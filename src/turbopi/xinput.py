"""XInput 手柄读取：绕过内核 HID 层，直接用 usbfs 读 XTYPE 中断端点。

为什么需要这个（M2 车机实测，2026-11）：
  1. 内核 `# CONFIG_INPUT_JOYSTICK is not set` —— xpad / joydev 都不存在，
     `/dev/input/js*` 和带 ABS 轴的 `/dev/input/event*` 永远不会出现；
  2. 手柄（飞智 APEX5，37d7:2501）在 XInput 模式下，interface 0 是 class=0xff
     subclass=0x5d protocol=0x01（标准 XTYPE 签名），本该由 xpad 绑定 —— 没有；
  3. 手动 `new_id` 让 usbhid 去接管，它 `can't add hid device: -22`（XTYPE 的
     描述符 hid-core 解不了，这正是不需要描述符的 xpad 存在的原因）。
  所以这里直接 USBDEVFS_SUBMITURB 读 interrupt IN 端点，按 XInput 20 字节报文解析。
  **不需要 root 改内核、不需要编 .ko、不需要 hidraw。**

报文格式（XInput over USB，固定 20 字节）：
    0     00          报文类型
    1     14          长度 20
    2     b2 低字节   上/下/左/右 + START/BACK + L3/R3
    3     b3 高字节   LB/RB/A/B/X/Y/GUIDE
    4     LT          0..255
    5     RT          0..255
    6-7   LX          有符号 16 位，-32768..32767
    8-9   LY
    10-11 RX
    12-13 RY
"""
import ctypes
import ctypes.util
import errno
import logging
import os
import time

log = logging.getLogger(__name__)

libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
libc.ioctl.restype = ctypes.c_int
libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]

USBDEVFS_CLAIMINTERFACE = 0x8004550F
USBDEVFS_RELEASEINTERFACE = 0x80045510
USBDEVFS_SUBMITURB = 0x8038550A
USBDEVFS_REAPURBNDELAY = 0x4008550D
USBDEVFS_DISCARDURB = 0x550B
USBDEVFS_URB_TYPE_INTERRUPT = 1

DEFAULT_VID = 0x37D7        # Flydigi
DEFAULT_PID = 0x2501        # APEX5 接收器

AXIS_MAX = 32768.0
DEADBAND = 0.02


class _Urb(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ubyte),
        ("endpoint", ctypes.c_ubyte),
        ("status", ctypes.c_int),
        ("flags", ctypes.c_uint),
        ("buffer", ctypes.c_void_p),
        ("buffer_length", ctypes.c_int),
        ("actual_length", ctypes.c_int),
        ("start_frame", ctypes.c_int),
        ("number_of_packets", ctypes.c_int),
        ("error_count", ctypes.c_int),
        ("signr", ctypes.c_uint),
        ("usercontext", ctypes.c_void_p),
    ]


def _ioctl(fd, request, arg, what):
    if libc.ioctl(fd, ctypes.c_ulong(request), ctypes.byref(arg)) < 0:
        err = ctypes.get_errno()
        raise OSError(err, "%s: %s" % (what, os.strerror(err)))


# --------------------------------------------------------------------- 设备发现
def find_interface(vid=DEFAULT_VID, pid=DEFAULT_PID):
    """在 sysfs 里找 XTYPE 接口，返回 dict 或 None。"""
    base = "/sys/bus/usb/devices"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    for name in entries:
        node = os.path.join(base, name)
        try:
            dev_vid = int(open(os.path.join(node, "idVendor")).read().strip(), 16)
            dev_pid = int(open(os.path.join(node, "idProduct")).read().strip(), 16)
        except (OSError, ValueError):
            continue
        if (dev_vid, dev_pid) != (vid, pid):
            continue
        try:
            bus = int(open(os.path.join(node, "busnum")).read())
            num = int(open(os.path.join(node, "devnum")).read())
        except (OSError, ValueError):
            continue
        for intf in entries:
            if not intf.startswith(name + ":"):
                continue
            ipath = os.path.join(base, intf)
            try:
                cls = int(open(os.path.join(ipath, "bInterfaceClass")).read(), 16)
                sub = int(open(os.path.join(ipath, "bInterfaceSubClass")).read(), 16)
            except (OSError, ValueError):
                continue
            if (cls, sub) != (0xFF, 0x5D):
                continue
            iface = int(intf.rsplit(".", 1)[1])
            for ep in sorted(os.listdir(ipath)):
                if not ep.startswith("ep_"):
                    continue
                epdir = os.path.join(ipath, ep)
                try:
                    etype = open(os.path.join(epdir, "type")).read().strip()
                    edir = open(os.path.join(epdir, "direction")).read().strip()
                except OSError:
                    continue
                if etype == "Interrupt" and edir == "in":
                    return {"bus": bus, "dev": num, "iface": iface,
                            "ep": int(ep.split("_")[1], 16),
                            "sysfs": node, "intf": intf}
    return None


def available(vid=DEFAULT_VID, pid=DEFAULT_PID):
    return find_interface(vid, pid) is not None


# ------------------------------------------------------------------------- 解析
def _s16(low, high):
    value = low | (high << 8)
    return value - 0x10000 if value & 0x8000 else value


def parse_report(data, layout="standard"):
    """20 字节 XInput 报文 -> dict；不是标准报文返回 None。"""
    if not data or len(data) < 14:
        return None
    if data[0] != 0x00 or data[1] not in (0x14, 0x01):
        return None
    low, high = data[2], data[3]
    bits = LAYOUTS.get(layout or "standard") or LAYOUTS["standard"]
    state = {
        "lx": _s16(data[6], data[7]),
        "ly": _s16(data[8], data[9]),
        "rx": _s16(data[10], data[11]),
        "ry": _s16(data[12], data[13]),
        "lt": data[4],
        "rt": data[5],
        "select": bool(low & 0x20),
        "start": bool(low & 0x10),
        "l3": bool(low & 0x40),
        "r3": bool(low & 0x80),
        "up": bool(low & 0x01),
        "down": bool(low & 0x02),
        "left": bool(low & 0x04),
        "right": bool(low & 0x08),
    }
    for name, mask in bits.items():
        state[name] = bool(high & mask)
    for name in BUTTONS:                 # 该 profile 没定义的键，一律当"没按"
        state.setdefault(name, False)
    return state


def _normalize(raw):
    value = max(-1.0, min(1.0, raw / AXIS_MAX))
    return 0.0 if abs(value) < DEADBAND else value


AXES = ("lx", "ly", "rx", "ry")
BUTTONS = ("a", "b", "x", "y", "l1", "r1", "select", "start",
           "guide", "l3", "r3", "up", "down", "left", "right")

# 面键/肩键在 data[3] 上的位定义。不同厂商、不同固件版本不一样，用 profile 区分。
LAYOUTS = {
    # 微软标准 XInput：Xbox 原装手柄、多数第三方
    "standard": {"l1": 0x01, "r1": 0x02, "a": 0x04, "b": 0x08,
                 "x": 0x10, "y": 0x20, "guide": 0x40},
    # 飞智 APEX5（37d7:2501）实测：面键整体上移两位，Y 在 0x80，没有 guide
    "flydigi": {"l1": 0x01, "r1": 0x02, "a": 0x10, "b": 0x20,
                "x": 0x40, "y": 0x80},
}


def auto_layout(vid, pid):
    """按 VID:PID 猜 profile；也可用 TURBOPI_PAD_LAYOUT=standard|flydigi 强制指定。"""
    forced = os.environ.get("TURBOPI_PAD_LAYOUT", "").strip().lower()
    if forced in LAYOUTS:
        return forced
    if (int(vid), int(pid)) == (0x37D7, 0x2501):
        return "flydigi"
    return "standard"


class XInputPad(object):
    """与 turbopi.gamepad.Gamepad 同接口，可直接替换。

        pad = XInputPad()
        while True:
            for kind, name, value in pad.poll(timeout=0.1):
                ...
            print(pad.axes)      # {'lx':..,'ly':..,'rx':..,'ry':..} -1..1
    """

    def __init__(self, vid=DEFAULT_VID, pid=DEFAULT_PID, packet=64, invert_y=True):
        """invert_y: XInput 的 LY 上推为正，而本项目内部（与 evdev ABS_Y 一致）
        约定上推为负，所以默认取反一次。实测飞智 APEX5 就需要这样。"""
        info = find_interface(vid, pid)
        if info is None:
            raise RuntimeError("no XTYPE interface for %04x:%04x" % (vid, pid))
        self.info = info
        self.path = "usb:%03d/%03d iface%d ep0x%02x" % (
            info["bus"], info["dev"], info["iface"], info["ep"])
        self.name = "XInput usbfs (%04x:%04x)" % (vid, pid)
        self.alive = True
        self.frames = 0
        self.last_error = None
        self.axes = {name: 0.0 for name in AXES}
        self.keys = {name: 0 for name in BUTTONS}
        self.triggers = {"lt": 0.0, "rt": 0.0}
        self.invert_y = bool(invert_y)
        self.layout = auto_layout(vid, pid)
        self.last_report = b""
        self._primed = False

        self.fd = os.open("/dev/bus/usb/%03d/%03d" % (info["bus"], info["dev"]), os.O_RDWR)
        self._iface = ctypes.c_uint(info["iface"])
        try:
            _ioctl(self.fd, USBDEVFS_CLAIMINTERFACE, self._iface, "claim interface")
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise

        self._buf = ctypes.create_string_buffer(packet)
        self._urb = _Urb(type=USBDEVFS_URB_TYPE_INTERRUPT, endpoint=info["ep"],
                         status=0, flags=0,
                         buffer=ctypes.cast(self._buf, ctypes.c_void_p),
                         buffer_length=packet, actual_length=0, start_frame=0,
                         number_of_packets=0, error_count=0, signr=0, usercontext=None)
        self._ptr = ctypes.c_void_p(0)
        self._submitted = False
        self._submit()

    # ---------------------------------------------------------------- 传输层
    def _submit(self):
        _ioctl(self.fd, USBDEVFS_SUBMITURB, self._urb, "submiturb")
        self._submitted = True

    def read_raw(self, timeout_ms=200):
        """取一帧原始报文；超时返回 None。"""
        if not self.alive:
            return None
        if not self._submitted:
            self._submit()
        deadline = time.time() + max(0, timeout_ms) / 1000.0
        first = True
        while first or time.time() < deadline:
            first = False
            self._ptr.value = 0
            try:
                _ioctl(self.fd, USBDEVFS_REAPURBNDELAY, self._ptr, "reapurb")
            except OSError as exc:
                # EAGAIN = 还没有完成事件（正常，URB 在飞）；只有真错误才算掉线
                if exc.errno in (errno.EAGAIN, errno.EBUSY, errno.EINTR):
                    time.sleep(0.001)
                    continue
                log.warning("reap urb failed: %s", exc)
                self.last_error = exc
                self.alive = False
                return None
            if not self._ptr.value:
                time.sleep(0.001)
                continue
            length, status = self._urb.actual_length, self._urb.status
            self._submitted = False
            try:
                self._submit()
            except OSError:
                self.alive = False
                return None
            if status != 0 or length <= 0:
                continue
            self.frames += 1
            return bytes(self._buf.raw[:length])
        return None

    # ---------------------------------------------------------------- 高层接口
    def poll(self, timeout=0.1):
        """读最新一帧（把积压的都丢掉，保证低延迟），返回变化事件。"""
        data = self.read_raw(timeout_ms=int(timeout * 1000) or 3)
        if data is None:
            return []
        while True:                                  # 追到最新
            newer = self.read_raw(timeout_ms=0)
            if newer is None:
                break
            data = newer
        state = parse_report(data, self.layout)
        if state is None:
            return []
        self.last_report = bytes(data)        # raw dump 用：最近一帧原始报文
        if not self._primed:
            # 第一帧只当基线：端点 FIFO 里可能残留上一次会话的报文（比如按过的 START），
            # 那不该被解释成"刚按下"。之后只对状态变化发事件。
            self._primed = True
            for axis in AXES:
                value = _normalize(state[axis])
                if axis == "ly" and self.invert_y:
                    value = -value if value else 0.0
                self.axes[axis] = value
            for button in BUTTONS:
                self.keys[button] = 1 if state.get(button) else 0
            for trigger in ("lt", "rt"):
                self.triggers[trigger] = state[trigger] / 255.0
            return []
        events = []
        for axis in AXES:
            value = _normalize(state[axis])
            if axis == "ly" and self.invert_y:
                value = -value if value else 0.0     # 上推为正 -> 上推为负（对齐 evdev）
            if value != self.axes[axis]:
                self.axes[axis] = value
                events.append(("axis", axis, value))
        for button in BUTTONS:
            value = 1 if state.get(button) else 0
            if value != self.keys[button]:
                self.keys[button] = value
                events.append(("button", button, value))
        for trigger in ("lt", "rt"):
            value = state[trigger] / 255.0
            if abs(value - self.triggers[trigger]) > 0.01:
                self.triggers[trigger] = value
                events.append(("axis", trigger, value))
        return events

    def close(self):
        fd = getattr(self, "fd", None)
        if fd is None:
            return
        if self._submitted:
            try:
                _ioctl(fd, USBDEVFS_DISCARDURB, self._urb, "discard urb")
            except OSError:
                pass
            self._submitted = False
        try:
            _ioctl(fd, USBDEVFS_RELEASEINTERFACE, self._iface, "release interface")
        except OSError:
            pass
        os.close(fd)
        self.fd = None
        self.alive = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __repr__(self):
        return "<XInputPad %s frames=%d>" % (self.path, self.frames)


def describe(vid=DEFAULT_VID, pid=DEFAULT_PID):
    """给 CLI 用：返回可打印的 dict 或 None。"""
    info = find_interface(vid, pid)
    if info is None:
        return None
    return {
        "path": "usb:%03d/%03d iface%d" % (info["bus"], info["dev"], info["iface"]),
        "name": "XInput usbfs (%04x:%04x)" % (vid, pid),
        "abs": {"lx": (-32768, 32767), "ly": (-32768, 32767),
                "rx": (-32768, 32767), "ry": (-32768, 32767)},
        "buttons": list(BUTTONS),
        "endpoint": info["ep"],
    }
