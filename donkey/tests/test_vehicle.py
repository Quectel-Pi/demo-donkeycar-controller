"""donkeycar 装配层测试：用**真的** donkeycar 5.4 Vehicle 跑我们的 Part。

不需要硬件、也不需要 Linux（turbopi.board 会 import termios，所以这里只用到
turbopi.chassis 的纯运动学 + DryRunBoard 替身）。

运行：
    PYTHONPATH=<含 turbopi/carapp 的路径或 zip> python tests/test_vehicle.py
"""

import os
import sys
import time
from types import SimpleNamespace

failures = []


def check(label, ok, detail=""):
    print("  [%s] %-52s %s" % ("PASS" if ok else "FAIL", label, detail))
    if not ok:
        failures.append(label)


def make_cfg(**overrides):
    cfg = SimpleNamespace(
        CAR_NAME="turbopi-test",
        DRIVE_LOOP_HZ=20,
        MAX_LINEAR=0.5,
        MAX_ANGULAR=2.0,
        MAX_DUTY=100.0,
        MOTOR_MODE="duty",
        SERIAL_DEVICE="",
        SERIAL_BAUD=1000000,
        WHEELBASE=0.1368,
        TRACK_WIDTH=0.1410,
        STEERING_SIGN=-1.0,
        SCALE_ANGULAR_BY_THROTTLE=False,
        ACTUATOR_TIMEOUT=0.5,
        USE_CAMERA=False,
        USE_GAMEPAD=False,
        USE_TUB=False,
        DATA_PATH=os.path.join(os.path.expanduser("~"), "mycar", "data"),
        AUTO_CREATE_NEW_TUB=True,
        GAMEPAD_DEADZONE=0.08,
        GAMEPAD_THROTTLE_AXIS="ly",
        GAMEPAD_STEERING_AXIS="rx",
        GAMEPAD_INVERT_THROTTLE=True,
        GAMEPAD_INVERT_STEERING=False,
        GAMEPAD_ESTOP_BUTTONS="l1,x",
        GAMEPAD_QUIT_BUTTON="start",
        GAMEPAD_BACKEND="evdev",
        GAMEPAD_PATH="",
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class FakeDriver:
    """顶部输入 Part：头几帧给固定 (angle, throttle)，之后归零。"""

    def __init__(self, angle=0.5, throttle=0.4, frames=5):
        self.angle = angle
        self.throttle = throttle
        self.frames = frames
        self.count = 0

    def run(self):
        self.count += 1
        if self.count <= self.frames:
            return self.angle, self.throttle
        return 0.0, 0.0

    def shutdown(self):
        pass


class FakePad:
    def __init__(self, axes=None):
        self.axes = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0}
        self.axes.update(axes or {})
        self.alive = True
        self.keys = {}

    def poll(self, timeout=0.0):
        return []

    def close(self):
        pass


class ScriptPad(FakePad):
    """按键脚本化：poll() 返回预先排好的事件，用来验证急停/退出逻辑。"""

    def __init__(self, axes=None, events=None):
        FakePad.__init__(self, axes)
        self.queue = list(events or [])

    def poll(self, timeout=0.0):
        out, self.queue = self.queue, []
        return out


def main():
    import donkeycar as dk
    from carapp.assemble import DryRunBoard, build_vehicle, describe
    from carapp.hal import build_config
    from carapp.parts.gamepad_evdev import GamepadInput
    from turbopi.chassis import MecanumChassis

    print("donkeycar", dk.__version__ if hasattr(dk, "__version__") else "?",
          "/ python", sys.version.split()[0])

    # ---------------------------------------------------- 1. 运动学映射
    print("== kinematics / steering sign")
    cfg = make_cfg()
    board = DryRunBoard()
    chassis = MecanumChassis(board, build_config(cfg))
    _vehicle, info = build_vehicle(cfg, chassis=chassis)
    actuator = info["actuator"]

    velocity = actuator.to_velocity(0.5, 0.4)
    check("angle=+0.5 throttle=+0.4 -> vx=+0.2 wz=-1.0",
          abs(velocity[0] - 0.2) < 1e-9 and abs(velocity[2] + 1.0) < 1e-9, str(velocity))

    chassis.set_velocity(*velocity)
    expected = (-67.78, 12.22, -67.78, 12.22)
    check("duties match HiWonder mecanum formula",
          all(abs(a - b) < 0.02 for a, b in zip(board.last_duty, expected)),
          "got %s expect %s" % (board.last_duty, expected))

    velocity = actuator.to_velocity(0.0, -0.4)
    check("reverse throttle -> vx negative", velocity[0] < 0, str(velocity))
    actuator.shutdown()

    # -------------------------------- 2. 真的 donkeycar Vehicle 循环
    print("== real donkeycar Vehicle loop (threaded parts)")
    cfg = make_cfg()
    board = DryRunBoard()
    chassis = MecanumChassis(board, build_config(cfg))
    vehicle, info = build_vehicle(cfg, chassis=chassis)
    vehicle.add(FakeDriver(angle=0.5, throttle=0.4, frames=4),
                outputs=["user/angle", "user/throttle"])
    print(describe(cfg, info).replace("\n", "\n   | "))

    vehicle.start(rate_hz=20, max_loop_count=8)
    time.sleep(0.25)          # 给执行器线程一拍时间
    frames = [f for f in board.frames if f[0] == "duty"]
    moving = [f for f in frames if any(abs(v) > 1e-6 for v in f[1])]
    check("vehicle loop produced duty frames", len(frames) >= 4, "frames=%d" % len(frames))
    check("vehicle loop produced motion", len(moving) >= 1,
          "first moving=%s" % (moving[0][1] if moving else None,))
    if moving:
        check("motion matches expected duties",
              all(abs(a - b) < 0.02 for a, b in zip(moving[0][1], expected)),
              str(moving[0][1]))

    # -------------------------------------------- 3. 看门狗（断指令停车）
    print("== actuator watchdog")
    import threading

    from carapp.parts.mecanum_actuator import MecanumActuator

    cfg = make_cfg()
    board = DryRunBoard()
    chassis = MecanumChassis(board, build_config(cfg))
    act = MecanumActuator(chassis, cfg)
    act.run_threaded(0.0, 0.6)                 # 主循环给过一条指令，之后"卡死"
    thread = threading.Thread(target=act.update, daemon=True)
    thread.start()
    time.sleep(0.15)
    active = [f for f in board.frames if any(abs(v) > 1e-9 for v in f[1])]
    time.sleep(float(cfg.ACTUATOR_TIMEOUT) + 0.4)
    zeros = [f for f in board.frames if all(abs(v) < 1e-9 for v in f[1])]
    check("actuator applies the command", len(active) > 0,
          "moving frames=%d" % len(active))
    check("watchdog zeroes motor after timeout", len(zeros) > 0,
          "zero frames=%d, updates=%d" % (len(zeros), act.updates))
    act.shutdown()

    # ------------------------------------------------- 4. 手柄段映射
    print("== gamepad mapping (injected evdev stub)")
    # evdev 约定：ABS_Y 向上推是负值、ABS_RX 向右推是正值
    # donkey 约定：throttle>0 前进、angle>0 右转
    cfg = make_cfg()
    pad = GamepadInput(cfg, pad=FakePad({"ly": -0.8, "rx": 0.5}))
    steering, throttle = pad.run()
    check("stick up (ly=-0.8) -> throttle +0.8", abs(throttle - 0.8) < 1e-9, "%.3f" % throttle)
    check("stick right (rx=+0.5) -> angle +0.5", abs(steering - 0.5) < 1e-9, "%.3f" % steering)

    pad_down = GamepadInput(cfg, pad=FakePad({"ly": 0.8, "rx": -0.5}))
    steering, throttle = pad_down.run()
    check("stick down/left -> throttle -, angle -",
          abs(throttle + 0.8) < 1e-9 and abs(steering + 0.5) < 1e-9,
          "%.3f/%.3f" % (throttle, steering))

    # 端到端：手柄量 -> 幻尔四轮占空比（向右打舵 = 右侧轮慢/左侧轮快）
    board = DryRunBoard()
    chassis = MecanumChassis(board, build_config(cfg))
    act = MecanumActuator(chassis, cfg)
    velocity = act.to_velocity(steering * -1, abs(throttle))   # 前进 + 向右打舵
    act.run(steering * -1, abs(throttle))
    check("forward-right gives asymmetric duties",
          board.last_duty[0] < board.last_duty[1] and abs(velocity[2]) > 0,
          "%s (wz=%+.2f)" % (board.last_duty, velocity[2]))
    act.shutdown()

    pad2 = GamepadInput(cfg, pad=FakePad({"ly": 0.03, "rx": 0.02}))
    steering, throttle = pad2.run()
    check("deadzone swallows small noise", steering == 0.0 and throttle == 0.0,
          "%s/%s" % (steering, throttle))

    pad3 = GamepadInput(make_cfg(), pad=ScriptPad({"ly": -0.9}))
    steering, throttle = pad3.run()
    check("hold forward -> throttle +0.9", abs(throttle - 0.9) < 1e-9, "%.3f" % throttle)

    pad3._pad.queue.append(("button", "l1", 1))
    steering, throttle = pad3.run()
    check("estop (l1) while stick held -> zero", steering == 0.0 and throttle == 0.0,
          "%.3f/%.3f" % (steering, throttle))

    pad3._pad.queue.append(("button", "l1", 0))
    steering, throttle = pad3.run()
    check("release estop -> throttle restored", abs(throttle - 0.9) < 1e-9, "%.3f" % throttle)

    pad3._pad.queue.append(("button", "start", 1))
    pad3.run()
    check("START sets quit_now", pad3.quit_now is True, str(pad3.quit_now))

    pad4 = GamepadInput(make_cfg(), pad=ScriptPad({"ly": -0.9}))
    pad4._pad.queue.append(("button", "a", 1))
    pad4.run()
    check("button not in estop list is ignored (a)", pad4.throttle == 0.9,
          "%.3f" % pad4.throttle)

    # ------------------------------------------- 6. XInput 报文位映射（纯逻辑）
    print("== xinput report bit mapping")
    from turbopi import xinput as xi
    rest = bytes.fromhex("0014000000000000000000000000000000000000")
    s = xi.parse_report(rest)
    check("rest frame -> nothing pressed and axes 0",
          s and not any(v for k, v in s.items() if isinstance(v, bool))
          and s["lx"] == 0 and s["ly"] == 0, str(s and s["ly"]))

    x_pressed = bytes.fromhex("0014001000000000c0ff000000000000000000")
    s = xi.parse_report(x_pressed)
    check("b3=0x10 -> x, ly=-64", s["x"] and not s["a"] and s["ly"] == -64, str(s["ly"]))

    l1_pressed = bytes.fromhex("0014000100000000000000000000000000000000")
    s = xi.parse_report(l1_pressed)
    check("b3=0x01 -> l1", s["l1"] and not s["a"] and not s["x"], str(s["l1"]))

    start_pressed = bytes.fromhex("0014100000000000000000000000000000000000")
    s = xi.parse_report(start_pressed)
    check("b2=0x10 -> start", s["start"] and not s["select"], str(s["start"]))

    s = xi.parse_report(b"\x00\x14\x00")
    check("short/invalid frame -> None (or parsed safely)", s is None or isinstance(s, dict), str(s))

    # --------------------------------------- 5. 关机时确实发了停车
    print("== shutdown stops the motors")
    cfg = make_cfg()
    board = DryRunBoard()
    chassis = MecanumChassis(board, build_config(cfg))
    vehicle, info = build_vehicle(cfg, chassis=chassis)
    vehicle.add(FakeDriver(angle=0.0, throttle=0.7, frames=3),
                outputs=["user/angle", "user/throttle"])
    vehicle.start(rate_hz=20, max_loop_count=3)
    stopped_before = board.stopped
    vehicle.stop()
    check("vehicle.stop() -> chassis stop_motors called",
          board.stopped > stopped_before, "stopped=%d" % board.stopped)

    print("\n%s (%d failures)" % ("VEHICLE TEST PASS" if not failures else "FAILURES",
                                  len(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
