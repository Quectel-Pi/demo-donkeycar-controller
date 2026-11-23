#!/usr/bin/env python3
"""donkeycar 应用入口（基于幻尔驱动层）。

    python3 manage.py check                 # 装配并打印配置，不接硬件
    python3 manage.py drive                 # 手柄驾驶（默认）
    python3 manage.py drive --tub           # 手柄驾驶 + 录制训练数据
    python3 manage.py drive --no-gamepad    # 只装配执行器（外部给 angle/throttle 的场合）
    python3 manage.py train                 # 用 donkeycar 自己的训练脚本
"""

import argparse
import importlib
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# turbopi 驱动层（同仓库的 ../src）
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_config():
    """把 myconfig.py 变成 cfg 对象，再套一层环境变量覆盖。"""
    module = importlib.import_module("myconfig")
    from types import SimpleNamespace

    cfg = SimpleNamespace(**{k: getattr(module, k) for k in dir(module) if k.isupper()})
    cfg.USE_TUB = bool(getattr(module, "USE_TUB", False))
    return cfg


def cmd_check(args):
    """装配 + 打印运动学，全程不碰硬件（运动学用真的 MecanumChassis，只是板子换成替身）。"""
    from carapp.assemble import DryRunBoard, build_vehicle, describe
    from carapp.hal import build_config
    from turbopi.chassis import MecanumChassis

    cfg = load_config()
    if args.tub:
        cfg.USE_TUB = True
    # check 默认不碰手柄（Windows/CI 上没有 evdev），要测手柄加 --gamepad
    cfg.USE_GAMEPAD = bool(args.gamepad)

    board = DryRunBoard()
    chassis = MecanumChassis(board, build_config(cfg))
    try:
        _vehicle, info = build_vehicle(cfg, chassis=chassis)
    except RuntimeError as exc:
        print("!! %s" % exc)
        return 1
    print(describe(cfg, info))

    print("\nkinematics preview (angle, throttle -> vx, vy, wz -> m1..m4 duty):")
    for steering, throttle in ((0.0, 0.3), (0.0, -0.3), (0.5, 0.0), (-0.5, 0.0)):
        velocity = info["actuator"].to_velocity(steering, throttle)
        chassis.set_velocity(*velocity)
        print("  angle=%+.2f throttle=%+.2f -> vx=%+.3f vy=%+.3f wz=%+.3f -> %s" % (
            steering, throttle, velocity[0], velocity[1], velocity[2],
            " / ".join("%+.1f%%" % v for v in board.last_duty)))
    info["actuator"].shutdown()
    print("\nCHECK OK (no serial device opened)")
    return 0


def cmd_monitor(args):
    """只读手柄：实时打印 (angle, throttle) 与换算出来的四轮占空比，**不动电机**。

    用途：确认"手柄能不能被 donkeycar 读到 / 方向对不对"而不让车上路。
    """
    from carapp.assemble import DryRunBoard, build_vehicle
    from carapp.hal import build_config
    from turbopi.chassis import MecanumChassis

    cfg = load_config()
    cfg.USE_GAMEPAD = True
    board = DryRunBoard()
    chassis = MecanumChassis(board, build_config(cfg))
    try:
        _vehicle, info = build_vehicle(cfg, chassis=chassis)
    except RuntimeError as exc:
        print("!! %s" % exc)
        return 1

    pad = None
    for entry in _vehicle.parts:
        if entry["part"].__class__.__name__ == "GamepadInput":
            pad = entry["part"]
            break
    if pad is None:
        print("!! gamepad part missing")
        return 1

    print("gamepad: %s %r  (no motor output; Ctrl-C to stop)" % (
        (pad.pad_info or {}).get("path", "?"), (pad.pad_info or {}).get("name", "?")))
    print("%-8s %-9s %-8s %-8s %s" % ("angle", "throttle", "vx", "wz", "duties"))
    actuator = info["actuator"]
    last = None
    last_t = 0.0
    try:
        while not getattr(pad, "quit_now", False):
            pad.poll()
            steering, throttle = pad.steering, pad.throttle
            velocity = actuator.to_velocity(steering, throttle)
            duties = chassis.to_duties(velocity[0], velocity[1], velocity[2], duty_cap=velocity[3])
            # 当前按下的键 + 右摇杆（诊断用：monitor 本身不把右摇杆映射到任何输出）
            keys = getattr(pad._pad, "keys", None) or {}
            pressed = tuple(sorted(k for k, v in keys.items() if v))
            axes = getattr(pad._pad, "axes", None) or {}
            rx = round(float(axes.get("rx", 0.0)), 2)
            ry = round(float(axes.get("ry", 0.0)), 2)
            # 加速态：throttle 被放大到 1 以上（扳机），或数字键按住
            boosting = abs(throttle) > 1.0001 or bool(getattr(pad, "boost", False))
            snapshot = (round(steering, 2), round(throttle, 2), bool(getattr(pad, "estop", False)),
                        boosting, pressed, rx, ry)
            now = time.time()
            # 变化立刻打印；状态不变但车在动/在加速时每 0.5 秒重打一行 ——
            # 否则"按住不放"只亮一行，看起来像掉回普通档了
            if snapshot != last or (now - last_t > 0.5 and
                                    (throttle or steering or boosting or snapshot[2] or any(duties)
                                     or pressed or rx or ry)):
                flags = ""
                if snapshot[2]:
                    flags += "   <ESTOP>"
                if boosting:
                    flags += "   <BOOST cap=%.0f>" % velocity[3]
                if pressed:
                    flags += "   keys=" + ",".join(pressed)
                flags += "   rx=%+.2f ry=%+.2f" % (rx, ry)
                print("%-8.2f %-9.2f %-8.3f %-8.3f %s%s" % (
                    steering, throttle, velocity[0], velocity[2],
                    " ".join("%+6.1f" % d for d in duties), flags), flush=True)
                last = snapshot
                last_t = now
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        pad.shutdown()
        print("monitor stopped (no motor command was sent)")
    return 0


def _hexdump(data):
    return " ".join("%02x" % b for b in data)


def cmd_buttons(args):
    """打印手柄真正上报的事件（按键/轴/扳机），--raw 时连原始报文一起打。"""
    import time as _time

    from carapp.parts.gamepad_evdev import GamepadInput

    cfg = load_config()
    pad = GamepadInput(cfg)
    print("board: %s" % (pad.pad_info or {}).get("name", "?"))
    print("现在按手柄上的键（A/B/X/Y/LB/RB/十字键/扳机/摇杆），事件会实时打印。Ctrl-C 退出。")
    print("加了 --raw 时，每一帧变化的原始报文都会打出来（含变化的字节下标）\n")
    seen = {}
    prev_raw = None
    try:
        while True:
            if pad._pad is None:
                break
            for kind, name, value in pad._pad.poll(timeout=0.0):
                if kind == "button" and not value:
                    print("button  %-8s 松开" % name, flush=True)
                    seen[name] = 1
                    continue
                shown = round(float(value), 2) if kind == "axis" else value
                print("%-7s %-8s %s" % (kind, name, shown), flush=True)
                seen[name] = 1
            raw = getattr(pad._pad, "last_report", b"")
            if args.raw and raw and raw != prev_raw:
                if prev_raw is None:
                    print("RAW  %s" % _hexdump(raw), flush=True)
                else:
                    diff = " ".join(
                        "[%d]%02x->%02x" % (i, prev_raw[i], raw[i])
                        for i in range(len(raw))
                        if i >= len(prev_raw) or raw[i] != prev_raw[i])
                    print("RAW  %s   %s" % (_hexdump(raw), diff), flush=True)
                prev_raw = raw
            _time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        pad.shutdown()
    print("\n本次会话上报过的名字：%s" % (", ".join(sorted(seen)) or "(无)"))
    return 0


def cmd_bench(args):
    """真机点轮子：走和 donkey 完全相同的执行器链路（I2C 0x7A 或 AA55 串口）。"""
    import time as _time

    from carapp.hal import build_chassis

    cfg = load_config()
    chassis, board = build_chassis(cfg)
    duty = max(-100.0, min(float(args.duty), 100.0))
    hold = max(0.1, args.ms / 1000.0)
    print("board  : %r" % (board,))
    try:
        battery = getattr(board, "read_battery", None)
        if battery:
            try:
                print("battery: %.2f V" % (battery() / 1000.0))
            except Exception as exc:            # noqa: BLE001
                print("battery: 读不到 (%s)" % exc)
    except Exception:                           # noqa: BLE001
        pass

    targets = [args.motor] if args.motor else ([1, 2, 3, 4] if args.sequence else [0])
    try:
        for target in targets:
            if target == 0:
                print(">>> 四轮 +%.0f%%  %.0f ms" % (duty, args.ms))
                board.set_motor_duty([duty, duty, duty, duty])
            else:
                duties = [0.0, 0.0, 0.0, 0.0]
                duties[target - 1] = duty
                print(">>> M%d +%.0f%%  %.0f ms" % (target, duty, args.ms))
                board.set_motor_duty(duties)
            _time.sleep(hold)
            board.set_motor_duty([0.0, 0.0, 0.0, 0.0])
            _time.sleep(0.35)
    finally:
        board.stop_motors()
        print("stopped")
    return 0


def cmd_jog(args):
    """按 (vx, vy, wz) 走一段，直接验证运动学方向。"""
    import time as _time

    from carapp.hal import build_chassis

    cfg = load_config()
    chassis, board = build_chassis(cfg)
    print("board: %r" % (board,))
    print(chassis.describe(args.vx, args.vy, args.wz))
    try:
        chassis.set_velocity(args.vx, args.vy, args.wz)
        _time.sleep(max(0.1, args.ms / 1000.0))
    finally:
        chassis.stop()
    print("stopped")


def cmd_drive(args):
    from carapp.assemble import build_vehicle, describe

    import time as _time

    cfg = load_config()
    if args.tub:
        cfg.USE_TUB = True
    if args.no_gamepad:
        cfg.USE_GAMEPAD = False
    if not cfg.USE_GAMEPAD:
        print("!! no input source configured (use --no-gamepad only with an external driver)")
        return 2

    estop = getattr(cfg, "GAMEPAD_ESTOP_BUTTONS", "l1") or "l1"
    if not isinstance(estop, str):
        estop = "/".join(str(b) for b in estop)

    while True:
        try:
            vehicle, info = build_vehicle(cfg)
        except RuntimeError as exc:
            if not args.wait:
                print("!! %s" % exc)
                return 1
            print("!! %s" % exc, flush=True)
            print("!! 等手柄 / 扩展板就绪，3 秒后重试（Ctrl-C 结束）", flush=True)
            _time.sleep(3)
            continue

        print(describe(cfg, info), flush=True)
        print("drive with the pad: %s = throttle, %s = steering, "
              "%s = e-stop (hold), %s = quit"
              % (getattr(cfg, "GAMEPAD_THROTTLE_AXIS", "ly"),
                 getattr(cfg, "GAMEPAD_STEERING_AXIS", "rx"),
                 estop.replace(",", " / "), getattr(cfg, "GAMEPAD_QUIT_BUTTON", "start")),
              flush=True)
        print("  右摇杆: %s = 横移（左/右平移）, %s = 原地旋转（左/右转）"
              % (getattr(cfg, "GAMEPAD_STRAFE_AXIS", "rx") or "关",
                 getattr(cfg, "GAMEPAD_ROTATE_AXIS", "ry") or "关"), flush=True)
        print("  动作键: %s = 原地左转, %s = 原地右转, %s = 横移模式（按住时左摇杆左右变平移）"
              % (getattr(cfg, "GAMEPAD_TURN_LEFT_BUTTON", "x"),
                 getattr(cfg, "GAMEPAD_TURN_RIGHT_BUTTON", "y"),
                 getattr(cfg, "GAMEPAD_STRAFE_BUTTON", "r1")), flush=True)
        print("  加速: 按住 %s 或扳机 %s（上限 %s%% → %s%%）"
              % (getattr(cfg, "GAMEPAD_BOOST_BUTTONS", "a"),
                 getattr(cfg, "GAMEPAD_BOOST_TRIGGER", "rt") or "关",
                 int(float(getattr(cfg, "MAX_DUTY", 30))),
                 int(float(getattr(cfg, "GAMEPAD_BOOST_DUTY", 100)))), flush=True)
        try:
            vehicle.start(rate_hz=info["loop_hz"], max_loop_count=args.loops)
        finally:
            vehicle.stop()
            print("stopped", flush=True)
        if not args.wait:
            return 0
        print("== 已停车，等待重新开始（插回手柄 / 按 START）==", flush=True)
        _time.sleep(2)


def cmd_train(args):
    """转发给 donkeycar 官方训练脚本（需要 tensorflow，车机上不装，一般在 PC 上跑）。"""
    cfg = load_config()
    try:
        from donkeycar.parts.keras import KerasLinear  # noqa: F401
    except Exception as exc:
        print("!! training needs tensorflow, which is not installed here: %s" % exc)
        print("   run training on your PC against the tub copied from %s" % cfg.DATA_PATH)
        return 2
    from donkeycar.templates.train import train
    train(cfg)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="TurboPi car on donkeycar (Hiwonder HAL)")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("check", help="assemble without hardware and print the plan")
    p.add_argument("--tub", action="store_true")
    p.add_argument("--gamepad", action="store_true", help="also probe the gamepad")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("monitor", help="read the gamepad only, print mapping, no motor output")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("buttons", help="dump raw gamepad events (which buttons really report)")
    p.add_argument("--raw", action="store_true", help="同时打印原始报文字节（含变化的下标）")
    p.set_defaults(func=cmd_buttons)

    p = sub.add_parser("jog", help="run (vx,vy,wz) for a while to check kinematics")
    p.add_argument("--vx", type=float, default=0.0, help="前进 m/s")
    p.add_argument("--vy", type=float, default=0.0, help="右平移 m/s")
    p.add_argument("--wz", type=float, default=0.0, help="左转 rad/s")
    p.add_argument("--ms", type=int, default=1500, help="持续毫秒")
    p.set_defaults(func=cmd_jog)

    p = sub.add_parser("bench", help="jog motors through the real board (no vehicle loop)")
    p.add_argument("--duty", type=float, default=30.0, help="占空比 %%（默认 30）")
    p.add_argument("--ms", type=int, default=1000, help="每次转多久（毫秒）")
    p.add_argument("--motor", type=int, default=0, help="1~4 只点某个电机，0 = 四轮一起（默认）")
    p.add_argument("--sequence", action="store_true", help="依次点 M1→M4")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("drive", help="drive with a gamepad (default)")
    p.add_argument("--tub", action="store_true", help="record training data")
    p.add_argument("--no-gamepad", action="store_true")
    p.add_argument("--loops", type=int, default=None, help="无人值守自测：跑 N 拍就退出")
    p.add_argument("--wait", action="store_true",
                   help="常驻模式：没手柄 / 没扩展板时每 3 秒重试，不退出（开机自启用）")
    p.set_defaults(func=cmd_drive)

    p = sub.add_parser("train", help="run donkeycar training (needs tensorflow)")
    p.set_defaults(func=cmd_train)

    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    if not getattr(args, "func", None):
        args.func = cmd_drive
        args.tub = False
        args.no_gamepad = False
        args.loops = None
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
