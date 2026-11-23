"""手柄读取：直接读 evdev，不用 pygame / donkeycar / joydev。

为什么不用 ROS2 那套 joy_node：它要求 /dev/input/js0（joydev），这块板的内核没有
joydev，js0 永远不会出现 —— 这也是幻尔 ROS2 版在这块板上本来就跑不起来的原因之一。

这里走 /dev/input/event*（evdev）：
  - EVIOCGBIT 认出"真正有摇杆轴"的设备，避免把开发板自己的按键（abs=[] 那种）当成手柄；
  - EVIOCGABS 取每个轴的 min/max，做到与手柄型号无关的归一化。

ioctl 编码按内核 evdev.h：
  EVIOCGBIT(ev,len) = _IOC(_IOC_READ, 'E', 0x20+ev, len)
  EVIOCGABS(abs)    = _IOC(_IOC_READ, 'E', 0x40+abs, sizeof(struct input_absinfo)=24)
  EVIOCGNAME(len)   = _IOC(_IOC_READ, 'E', 0x06, len)
"""

import fcntl
import glob
import logging
import os
import select
import struct

log = logging.getLogger(__name__)

_IOC_READ = 2
_EVDEV_TYPE = 0x45  # 'E'


def _ioc_read(number, size):
    return (_IOC_READ << 30) | (size << 16) | (_EVDEV_TYPE << 8) | number


def EVIOCGBIT(ev_type, length):
    return _ioc_read(0x20 + ev_type, length)


def EVIOCGABS(axis):
    return _ioc_read(0x40 + axis, 24)


def EVIOCGNAME(length):
    return _ioc_read(0x06, length)


def _ioctl_buffer(fd, request, size):
    buf = bytearray(size)
    try:
        fcntl.ioctl(fd, request, buf, True)
    except OSError:
        return None
    return bytes(buf)


EV_KEY, EV_ABS = 0x01, 0x03
EVENT_FMT = "@llHHi"          # struct input_event（64 位：8+8+2+2+4 = 24 字节）
EVENT_SIZE = struct.calcsize(EVENT_FMT)

ABS_X, ABS_Y, ABS_RX, ABS_RY = 0x00, 0x01, 0x03, 0x04
BTN_SOUTH, BTN_EAST, BTN_WEST, BTN_NORTH = 0x130, 0x131, 0x132, 0x133
BTN_TL, BTN_TR, BTN_SELECT, BTN_START = 0x136, 0x137, 0x13A, 0x13B

AXIS_NAMES = {ABS_X: "lx", ABS_Y: "ly", ABS_RX: "rx", ABS_RY: "ry"}
BUTTON_NAMES = {
    BTN_SOUTH: "a", BTN_EAST: "b", BTN_WEST: "x", BTN_NORTH: "y",
    BTN_TL: "l1", BTN_TR: "r1", BTN_SELECT: "select", BTN_START: "start",
}


def _ioctl_bits(fd, ev_type, nbytes):
    raw = _ioctl_buffer(fd, EVIOCGBIT(ev_type, nbytes), nbytes)
    if raw is None:
        return set()
    bits = set()
    for index, byte in enumerate(raw):
        for bit in range(8):
            if byte >> bit & 1:
                bits.add(index * 8 + bit)
    return bits


def _axis_range(fd, axis):
    raw = _ioctl_buffer(fd, EVIOCGABS(axis), 24)
    if raw is None:
        return None
    _value, minimum, maximum, _fuzz, _flat, _res = struct.unpack("@6i", raw)
    if maximum <= minimum:
        return None
    return minimum, maximum


def _device_name(fd):
    raw = _ioctl_buffer(fd, EVIOCGNAME(256), 256)
    if raw is None:
        return "?"
    return raw.split(b"\x00")[0].decode("utf-8", "replace")


def describe_device(path):
    """返回 dict（path/name/abs/buttons）或 None（打不开就跳过）。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        axes = {}
        abs_bits = _ioctl_bits(fd, EV_ABS, 8)
        for axis in (ABS_X, ABS_Y, ABS_RX, ABS_RY):
            if axis in abs_bits:
                span = _axis_range(fd, axis)
                if span:
                    axes[AXIS_NAMES[axis]] = span
        key_bits = _ioctl_bits(fd, EV_KEY, 96)
        return {
            "path": path,
            "name": _device_name(fd),
            "abs": axes,
            "buttons": sorted(BUTTON_NAMES[b] for b in key_bits if b in BUTTON_NAMES),
        }
    finally:
        os.close(fd)


def scan(paths=None):
    """扫描所有 evdev 设备，按"像不像手柄"排序（轴多、按键多的排前面）。"""
    results = []
    for path in sorted(paths or glob.glob("/dev/input/event*")):
        info = describe_device(path)
        if info:
            info["score"] = len(info["abs"]) * 10 + len(info["buttons"])
            results.append(info)
    results.sort(key=lambda item: (item["score"], item["path"]), reverse=True)
    return results


def pick(paths=None, min_axes=2):
    """挑一个至少 min_axes 个摇杆轴、并且带按键的设备；没有就返回 None。"""
    for info in scan(paths):
        if len(info["abs"]) >= min_axes and info["buttons"]:
            return info
    return None


class Gamepad:
    """事件式手柄。

        pad = Gamepad(path)
        while True:
            for kind, code, value in pad.poll(timeout=0.1):
                ...
            print(pad.axes)      # {'lx':..,'ly':..,'rx':..,'ry':..}  范围 -1..1
    """

    def __init__(self, path, axes=None):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.name = _device_name(self.fd)
        self.alive = True
        self.ranges = {}
        for axis in (axes or (ABS_X, ABS_Y, ABS_RX, ABS_RY)):
            span = _axis_range(self.fd, axis)
            if span:
                self.ranges[axis] = span
        self.axes = {name: 0.0 for name in AXIS_NAMES.values()}
        self.keys = {name: 0 for name in BUTTON_NAMES.values()}

    # -------------------------------------------------------------- 归一化
    def normalize(self, code, raw_value):
        span = self.ranges.get(code)
        if not span:
            return 0.0
        minimum, maximum = span
        center = (maximum + minimum) / 2.0
        half = (maximum - minimum) / 2.0
        value = max(-1.0, min(1.0, (raw_value - center) / half))
        return 0.0 if abs(value) < 0.02 else value

    # ---------------------------------------------------------------- 读取
    def poll(self, timeout=0.1):
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            self.alive = False
            return []
        if not ready:
            return []
        try:
            data = os.read(self.fd, EVENT_SIZE * 64)
        except OSError:
            # ENODEV：设备被拔掉；别的错误也当作掉线处理，让上层立刻停车
            self.alive = False
            return []
        if not data:
            self.alive = False
            return []
        events = []
        for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _s, _us, ev_type, code, value = struct.unpack(
                EVENT_FMT, data[offset:offset + EVENT_SIZE])
            if ev_type == EV_ABS and code in AXIS_NAMES:
                axis = AXIS_NAMES[code]
                self.axes[axis] = self.normalize(code, value)
                events.append(("axis", axis, self.axes[axis]))
            elif ev_type == EV_KEY and code in BUTTON_NAMES:
                button = BUTTON_NAMES[code]
                self.keys[button] = value
                events.append(("button", button, value))
        return events

    def close(self):
        if self.fd >= 0:
            try:
                os.close(self.fd)
            finally:
                self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __repr__(self):
        return "<Gamepad %s %r axes=%d>" % (self.path, self.name, len(self.ranges))
