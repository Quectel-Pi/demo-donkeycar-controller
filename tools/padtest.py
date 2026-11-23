#!/usr/bin/env python3
"""在车机上用 uinput 造一个虚拟手柄，验证 evdev 读取 + donkeycar 映射这一段。

不涉及真手柄、不涉及串口：只是证明"我们的代码能读出摇杆并算出正确占空比"。
真手柄插上后是同一条代码路径。

用法（车机，root）：
    cd /mnt/car_test/app
    PYTHONPATH=/mnt/car_test/app/src:/mnt/car_test/app/donkey/vendor/site-packages \
        python3 padtest.py
"""
import ctypes
import os
import fcntl
import struct
import subprocess
import sys
import time

ABS_CNT = 64
ABS_X, ABS_Y, ABS_RX, ABS_RY = 0, 1, 3, 4
BTN_A, BTN_START = 0x130, 0x13B
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045567
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502

libc = ctypes.CDLL("libc.so.6", use_errno=True)


class InputEvent(ctypes.Structure):
    _fields_ = [("sec", ctypes.c_long), ("usec", ctypes.c_long),
                ("type", ctypes.c_ushort), ("code", ctypes.c_ushort),
                ("value", ctypes.c_int32)]


def make_pad():
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    for bit in (EV_SYN, EV_KEY, EV_ABS):
        fcntl.ioctl(fd, UI_SET_EVBIT, bit)
    for code in (BTN_A, BTN_START):
        fcntl.ioctl(fd, UI_SET_KEYBIT, code)
    for code in (ABS_X, ABS_Y, ABS_RX, ABS_RY):
        fcntl.ioctl(fd, UI_SET_ABSBIT, code)

    # 老式 uinput_user_dev：name[80] + input_id(4*u16) + ff_effects_max + 4*abs[64]
    zeros = [0] * ABS_CNT
    absmax = list(zeros); absmin = list(zeros); absfuzz = list(zeros); absflat = list(zeros)
    for code in (ABS_X, ABS_Y, ABS_RX, ABS_RY):
        absmin[code], absmax[code] = -32768, 32767
    blob = struct.pack("80sHHHHI", b"Microsoft X-Box 360 pad", 0x03, 0x045E, 0x028E, 0x0110, 0)
    blob += struct.pack("%di" % ABS_CNT, *absmax)
    blob += struct.pack("%di" % ABS_CNT, *absmin)
    blob += struct.pack("%di" % ABS_CNT, *absfuzz)
    blob += struct.pack("%di" % ABS_CNT, *absflat)
    os.write(fd, blob)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def emit(fd, etype, code, value):
    ev = InputEvent(0, 0, etype, code, value)
    os.write(fd, bytes(ev))


def sync(fd):
    emit(fd, EV_SYN, 0, 0)


def axes(fd, x=0, y=0, rx=0, ry=0):
    emit(fd, EV_ABS, ABS_X, x)
    emit(fd, EV_ABS, ABS_Y, y)
    emit(fd, EV_ABS, ABS_RX, rx)
    emit(fd, EV_ABS, ABS_RY, ry)
    sync(fd)


def button(fd, code, value):
    emit(fd, EV_KEY, code, value)
    sync(fd)


def find_node():
    for name in sorted(os.listdir("/dev/input")):
        if not name.startswith("event"):
            continue
        path = "/dev/input/" + name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            buf = bytearray(96)
            fcntl.ioctl(fd, 0x80FF4506, buf)          # EVIOCGNAME
            if b"X-Box" in bytes(buf):
                return path
        finally:
            os.close(fd)
    return None


def main():
    fd = make_pad()
    node = None
    for _ in range(50):
        time.sleep(0.1)
        node = find_node()
        if node:
            break
    if not node:
        print("!! virtual pad did not appear in /dev/input")
        return 1
    print("virtual pad ->", node)

    env = dict(os.environ)
    env.setdefault("PYTHONPATH", "/mnt/car_test/app/src:/mnt/car_test/app/donkey/vendor/site-packages")
    proc = subprocess.Popen(
        [sys.executable, "donkey/manage.py", "monitor"],
        cwd="/mnt/car_test/app", env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.5)                                    # 等它打开手柄

    seq = [
        (0.30, lambda: axes(fd)),
        (0.30, lambda: axes(fd, y=-30000)),            # 摇杆上推 = 前进
        (0.30, lambda: axes(fd, rx=20000)),            # 右扳 = 右转
        (0.30, lambda: axes(fd, y=-30000, rx=20000)),  # 前进 + 右转
        (0.30, lambda: axes(fd, y=30000)),             # 后退
        (0.30, lambda: axes(fd, y=-30000)),
        (0.30, lambda: button(fd, BTN_A, 1)),          # 急停
        (0.30, lambda: button(fd, BTN_A, 0)),
        (0.40, lambda: button(fd, BTN_START, 1)),      # START = 退出
    ]
    for delay, action in seq:
        time.sleep(delay)
        action()

    try:
        out, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        print("!! monitor did not exit on START")
        print(out[-800:])
        return 1
    finally:
        fcntl.ioctl(fd, UI_DEV_DESTROY)
        os.close(fd)

    lines = out.splitlines()
    print("--- monitor output ---")
    for ln in lines:
        print(ln)

    # 解析数据行：[angle, throttle, vx, wz, duty1..4]
    rows = []
    for ln in lines:
        parts = ln.split()
        if len(parts) == 8:
            try:
                rows.append([float(v) for v in parts])
            except ValueError:
                pass
    duties = [r[4:] for r in rows]

    def all_neg(d):
        return all(v <= -20 for v in d)

    def all_pos(d):
        return all(v >= 20 for v in d)

    def mixed(d):
        return any(v <= -50 for v in d) and any(v >= 20 for v in d)

    def uniform(d):
        return max(d) - min(d) <= 0.2 * max(abs(v) for v in d)

    def zeros(d):
        return all(abs(v) < 0.5 for v in d)

    def row_zeroed(rows):
        """动过之后又出现全零的行 —— 对应按了 A 急停。"""
        seen_motion = False
        for r in rows:
            if any(abs(v) > 20 for v in r[4:]):
                seen_motion = True
            elif seen_motion and zeros(r[4:]):
                return True
        return False

    checks = [
        ("pad detected by donkeycar", "gamepad:" in out),
        ("mapping lines printed (>=4 distinct)", len(rows) >= 4),
        ("forward -> all wheels same sign", any(all_neg(d) or all_pos(d) for d in duties)),
        ("turn only -> four wheels equal magnitude",
         any(uniform(d) and not zeros(d) for d in duties)),
        ("forward+right -> asymmetric duties", any(mixed(d) for d in duties)),
        ("e-stop -> all duties zero", row_zeroed(rows)),
        ("START -> clean exit", "monitor stopped" in out and proc.returncode == 0),
    ]
    failed = [n for n, ok in checks if not ok]
    for name, ok in checks:
        print("[%s] %s" % ("PASS" if ok else "FAIL", name))
    print("PAD TEST", "PASS" if not failed else "FAIL (%d)" % len(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
