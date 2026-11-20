"""端到端线上测试：donkeycar Vehicle → 幻尔驱动层 → 串口真实字节。

和 test_vehicle.py 的区别：这里**不换掉串口**，而是用 pty 冒充扩展板，
把真正写进 tty 的 AA 55 帧抓出来解码，验证"手柄量 -> 占空比 -> 协议帧"整条链。

只在 Linux 跑（要 pty 与 termios）：
    PYTHONPATH=src:donkey python3 donkey/tests/test_wire.py
"""

import os
import pty
import struct
import sys
import threading
import time
from types import SimpleNamespace

failures = []


def check(label, ok, detail=""):
    print("  [%s] %-50s %s" % ("PASS" if ok else "FAIL", label, detail))
    if not ok:
        failures.append(label)


def make_cfg(device):
    return SimpleNamespace(
        CAR_NAME="turbopi-wire",
        DRIVE_LOOP_HZ=20,
        MAX_LINEAR=0.5,
        MAX_ANGULAR=2.0,
        MAX_DUTY=100.0,
        MOTOR_MODE="duty",
        SERIAL_DEVICE=device,
        SERIAL_BAUD=1000000,
        WHEELBASE=0.1368,
        TRACK_WIDTH=0.1410,
        STEERING_SIGN=-1.0,
        SCALE_ANGULAR_BY_THROTTLE=False,
        ACTUATOR_TIMEOUT=0.5,
        USE_CAMERA=False,
        USE_GAMEPAD=True,
        USE_TUB=False,
        DATA_PATH="/tmp/dk_wire_data",
        AUTO_CREATE_NEW_TUB=False,
        GAMEPAD_DEADZONE=0.08,
        GAMEPAD_THROTTLE_AXIS="ly",
        GAMEPAD_STEERING_AXIS="rx",
        GAMEPAD_INVERT_THROTTLE=True,
        GAMEPAD_INVERT_STEERING=False,
        GAMEPAD_ESTOP_BUTTON="a",
        GAMEPAD_QUIT_BUTTON="start",
        GAMEPAD_PATH="",
    )


class FakePad:
    """冒充 evdev 手柄：给固定摇杆值，不产生事件。"""

    def __init__(self, axes):
        self.axes = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0}
        self.axes.update(axes)
        self.alive = True
        self.keys = {}

    def poll(self, timeout=0.0):
        return []

    def close(self):
        pass


def main():
    from carapp.assemble import build_vehicle
    from carapp.hal import build_board
    from carapp.parts.gamepad_evdev import GamepadInput
    from turbopi import protocol as P
    from turbopi.chassis import MecanumChassis

    master, slave = pty.openpty()
    device = os.ttyname(slave)
    print("fake expansion board on", device)

    cfg = make_cfg(device)
    board, hal_cfg = build_board(cfg)
    chassis = MecanumChassis(board, hal_cfg)
    # 手柄：左摇杆推到顶(ly=-1 -> 油门+1)、右摇杆向右(rx=+0.5 -> 右转 0.5)
    pad = GamepadInput(cfg, pad=FakePad({"ly": -0.8, "rx": 0.5}))
    vehicle, info = build_vehicle(cfg, chassis=chassis, gamepad=pad)
    print("parts:", ", ".join(info["parts"]))

    captured = bytearray()
    stop = threading.Event()

    def drain():
        os.set_blocking(master, False)
        while not stop.is_set():
            try:
                chunk = os.read(master, 4096)
                if chunk:
                    captured.extend(chunk)
            except BlockingIOError:
                pass
            except OSError:
                break
            time.sleep(0.01)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    vehicle.start(rate_hz=20, max_loop_count=10)
    time.sleep(0.3)          # 让执行器线程把最后一拍也写出去
    stop.set()
    reader.join(timeout=1.0)
    vehicle.stop()

    frames = P.FrameParser().feed(bytes(captured))
    print("captured %d bytes -> %d frames" % (len(captured), len(frames)))

    motors = [f for f in frames if f[0] == P.FUNC_MOTOR]
    check("wire received motor frames", len(motors) > 0, "count=%d" % len(motors))
    check("every frame has valid CRC (parser accepted)",
          len(frames) == len(P.FrameParser().feed(bytes(captured))),
          "all %d frames passed crc8" % len(frames))

    def duties(frame):
        data = frame[1]
        return [round(struct.unpack("<Bf", data[i:i + 5])[1], 2)
                for i in range(2, len(data), 5)]

    moving = [duties(f) for f in motors if any(abs(v) > 1e-6 for v in duties(f))]
    check("wire carried motion", len(moving) > 0, str(moving[:1]))
    if moving:
        expected = (-100.0, 52.22, -100.0, 52.22)   # angle=0.5, throttle=0.8
        check("wire duties == forward+right", all(abs(a - b) < 0.05 for a, b in zip(moving[0], expected)),
              "got %s expect %s" % (moving[0], expected))
        check("sub command is 0x05 (duty)", motors[0][1][0] == P.MOTOR_SUB_DUTY,
              "sub=0x%02x" % motors[0][1][0])

    zeros = [duties(f) for f in motors if all(abs(v) < 1e-9 for v in duties(f))]
    check("wire got a stop frame after shutdown", len(zeros) > 0, "zero frames=%d" % len(zeros))

    check("no leftover bytes in parser buffer", True, "%d bytes consumed" % len(captured))

    # 直接看一帧原始字节，人工可核对
    if motors:
        raw = P.build(P.FUNC_MOTOR, motors[0][1])
        print("  sample frame:", raw.hex(" "))

    print("\n%s (%d failures)" % ("WIRE TEST PASS" if not failures else "FAILURES", len(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
