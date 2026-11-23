"""命令行入口。

    python3 -m turbopi list                     # 看串口 + 手柄设备
    python3 -m turbopi selftest [--pulse]       # 环境自检（不动机器人）
    python3 -m turbopi bench --duty 20 --ms 800 # 逐个动作试电机（先架高！）
    python3 -m turbopi raw --duty 20,20,20,20 --ms 800
    python3 -m turbopi sensors --seconds 5      # 读电池/IMU/按键
    python3 -m turbopi drive                    # 手柄驾驶（默认容器命令）
    python3 -m turbopi beep / led / servo

所有输出都是 ASCII，方便通过 adb / docker logs 查看。
母语注释、英文日志：跨终端不会乱码。
"""

import argparse
import logging
import os
import signal
import sys
import time

from . import __version__
from . import gamepad as gp
from .board import Board
from .chassis import MecanumChassis
from .config import Config, DEVICE_CANDIDATES, resolve_device

log = logging.getLogger("turbopi")


# --------------------------------------------------------------------- 工具
def add_common(parser):
    parser.add_argument("--device", help="serial device, e.g. /dev/rrc or /dev/ttyS1")
    parser.add_argument("--baud", type=int, help="default 1000000")
    parser.add_argument("--max-linear", dest="max_linear", type=float,
                        help="m/s, default 0.5 (Hiwonder joystick launch value)")
    parser.add_argument("--max-angular", dest="max_angular", type=float,
                        help="rad/s, default 2.0")
    parser.add_argument("--max-duty", dest="max_duty", type=float, help="clamp, default 100")
    parser.add_argument("--motor-mode", choices=("duty", "speed"), help="default duty")
    parser.add_argument("--log-level", default="info",
                        choices=("debug", "info", "warning", "error"))


def build_config(args):
    return Config(
        device=getattr(args, "device", None),
        baud=getattr(args, "baud", None),
        max_linear=getattr(args, "max_linear", None),
        max_angular=getattr(args, "max_angular", None),
        max_duty=getattr(args, "max_duty", None),
        motor_mode=getattr(args, "motor_mode", None),
    )


def setup_logging(level_name):
    logging.basicConfig(
        level=getattr(logging, str(level_name).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def open_board(cfg):
    device = resolve_device(cfg.device)
    cfg.device = device
    return Board(device, cfg.baud)


def print_kv(items):
    width = max(len(str(k)) for k, _ in items)
    for key, value in items:
        print("  %-*s : %s" % (width, key, value))


# ------------------------------------------------------------------ list
def cmd_list(args):
    print("== serial candidates ==")
    found = False
    for path in DEVICE_CANDIDATES:
        if os.path.exists(path):
            target = ""
            if os.path.islink(path):
                target = " -> " + os.readlink(path)
            print("  [ok]   %s%s" % (path, target))
            found = True
        else:
            print("  [--]   %s" % path)
    if not found:
        print("  no candidate exists; pass --device")

    print("== xinput gamepad (usbfs, no kernel driver needed) ==")
    try:
        from . import xinput as xi
    except ImportError:
        xi = None
    xinfo = xi.describe() if xi is not None else None
    if xinfo:
        print("  [ok]   %s  ep=0x%02x" % (xinfo["path"], xinfo["endpoint"]))
        print("         %r  axes=lx/ly/rx/ry  buttons=%d" % (xinfo["name"], len(xinfo["buttons"])))
        print("         走 usbfs 直读，所以内核没有 xpad/joydev 也能用")
    else:
        print("  [--]   no XTYPE interface (class ff/5d) for 37d7:2501")
        print("         手柄需在 Xinput 模式（白灯）并插在 USB-A(HOST) 口")

    print("== input devices (evdev) ==")
    devices = gp.scan()
    if not devices:
        print("  no /dev/input/event* readable in this namespace")
        print("  inside docker you must pass: -v /dev/input:/dev/input")
        return 0
    for info in devices:
        axes = ",".join("%s[%d..%d]" % (k, v[0], v[1]) for k, v in info["abs"].items())
        print("  %-22s %-28r axes=%-3d buttons=%d %s" % (
            info["path"], info["name"][:28], len(info["abs"]), len(info["buttons"]), axes))
        if len(info["abs"]) >= 2:
            print("      -> looks like a gamepad")
    return 0


# -------------------------------------------------------------- selftest
def cmd_selftest(args):
    cfg = build_config(args)
    print("turbopi %s / python %s" % (__version__, sys.version.split()[0]))
    print("== config ==")
    print_kv(sorted(cfg.as_dict().items()))
    try:
        board = open_board(cfg)
    except Exception as exc:
        print("!! cannot open serial device: %s" % exc)
        print("   check: ls -l /dev/rrc /dev/ttyS1 ; dmesg | grep ttyS")
        return 1
    try:
        print("== serial ==")
        from . import protocol as P
        print_kv([("device", board.device), ("baud", board.baud),
                  ("frame_duty_len", "%d bytes" % len(P.build(
                      P.FUNC_MOTOR,
                      bytes([P.MOTOR_SUB_DUTY, 4]) + b"\x00" * 20)))])

        chassis = MecanumChassis(board, cfg)
        print("== kinematics (no motor output) ==")
        for label, velocity in (("forward 0.1", (0.1, 0.0, 0.0)),
                                ("backward 0.1", (-0.1, 0.0, 0.0)),
                                ("turn left 0.5", (0.0, 0.0, 0.5)),
                                ("strafe right 0.1", (0.0, 0.1, 0.0))):
            print("  %-18s %s" % (label, chassis.describe(*velocity)))
        pads = gp.scan()
        print("== gamepad ==")
        if pads:
            for info in pads[:3]:
                print("  %s %r axes=%d" % (info["path"], info["name"][:24], len(info["abs"])))
        else:
            print("  none readable")
        if args.pulse:
            print("== pulse: beep + led (wheels stay still) ==")
            board.beep()
            board.set_led(0.2, 0.1, 2, 1)
            print("  tx_bytes=%d" % board.tx_bytes)
        print("OK")
        return 0
    finally:
        board.close()


# ----------------------------------------------------------------- bench
BENCH_ACTIONS = (
    ("forward", (0.15, 0.0, 0.0)),
    ("backward", (-0.15, 0.0, 0.0)),
    ("turn left", (0.0, 0.0, 0.3)),
    ("turn right", (0.0, 0.0, -0.3)),
    ("strafe left", (0.0, -0.15, 0.0)),
    ("strafe right", (0.0, 0.15, 0.0)),
)


def cmd_bench(args):
    cfg = build_config(args)
    board = open_board(cfg)
    chassis = MecanumChassis(board, cfg)
    duration = max(0.2, args.ms / 1000.0)
    # 每个动作先按标称速度算占空比，再等比缩放到 --duty，保证"最大轮 = duty%"
    plan = []
    for label, velocity in BENCH_ACTIONS:
        raw = chassis.to_duties(*velocity)
        peak = max(abs(value) for value in raw) or 1.0
        scaled = [value / peak * args.duty for value in raw]
        plan.append((label, velocity, [round(value, 1) for value in scaled]))
    try:
        print("device=%s @ %d, motor_mode=%s, peak duty=%s%%, %d ms per action" % (
            board.device, board.baud, cfg.motor_mode, args.duty, args.ms))
        print("(wheels must be off the ground)")
        for label, velocity, duties in plan:
            print("  %-13s %s  -> m1..m4 = %s" % (
                label, chassis.describe(*velocity),
                ", ".join("%+.1f%%" % value for value in duties)))
        input("  press ENTER to run (Ctrl-C aborts) ")
        for label, _velocity, duties in plan:
            print("  %-13s -> %s" % (label, chassis.set_duties_percent(duties)))
            time.sleep(duration)
            chassis.stop()
            time.sleep(0.3)
        print("  tx_bytes=%d" % board.tx_bytes)
        return 0

    except KeyboardInterrupt:
        print("\n  aborted")
        return 130
    finally:
        chassis.stop()
        board.close()


# ------------------------------------------------------------------- raw
def _parse_four(text, cast=float):
    parts = [chunk for chunk in text.replace(" ", ",").split(",") if chunk != ""]
    if len(parts) != 4:
        raise SystemExit("need 4 comma separated values, got %r" % text)
    return [cast(part) for part in parts]


def cmd_raw(args):
    cfg = build_config(args)
    board = open_board(cfg)
    chassis = MecanumChassis(board, cfg)
    duration = max(0.1, args.ms / 1000.0)
    try:
        if args.duty:
            values = _parse_four(args.duty)
            print("duty %s for %d ms" % (values, args.ms))
            chassis.set_duties_percent(values)
        else:
            values = _parse_four(args.speed)
            print("speed %s for %d ms" % (values, args.ms))
            board.mark_mode("speed")
            board.set_motor_speed(values)
        time.sleep(duration)
        return 0
    finally:
        chassis.stop()
        board.close()


# --------------------------------------------------------------- sensors
def cmd_sensors(args):
    cfg = build_config(args)
    board = open_board(cfg)
    board.start_reception()
    try:
        print("reading for %s s (battery/imu/key/sbus)" % args.seconds)
        deadline = time.time() + args.seconds
        while time.time() < deadline:
            time.sleep(0.5)
            print("  rx=%d frames=%d battery=%s imu=%s key=%s sbus=%s" % (
                board.rx_bytes, board.frames_in,
                ("%d mV" % board.battery) if board.battery else "None",
                ("(%.2f,%.2f,%.2f)" % board.imu[:3]) if board.imu else "None",
                board.button, "yes" if board.sbus else "None"))
        return 0 if board.frames_in else 3
    finally:
        board.close()


# ----------------------------------------------------------------- drive
def cmd_drive(args):
    cfg = build_config(args)
    board = open_board(cfg)
    chassis = MecanumChassis(board, cfg)
    pad_info = None
    if args.pad:
        pad_info = gp.describe_device(args.pad)
        if not pad_info:
            print("!! cannot read %s" % args.pad)
            board.close()
            return 1
    else:
        pad_info = gp.pick()
    if not pad_info:
        print("!! no gamepad found under /dev/input (need >=2 axes + buttons)")
        print("   python3 -m turbopi list   # to inspect")
        board.close()
        return 1

    print("gamepad: %s %r" % (pad_info["path"], pad_info["name"]))
    print_kv(sorted(cfg.as_dict().items()))
    print("left stick = move (x forward/back, y strafe) | right stick X = rotate")
    print("a = e-stop (hold) | b = beep | start = quit")

    stop_flag = {"value": False}

    def _on_signal(signum, _frame):
        print("\nsignal %d -> stopping" % signum)
        stop_flag["value"] = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    chassis.stop()
    board.set_led(0.3, 0.0, 1, 1)
    last = (None, None, None)
    last_send = 0.0
    try:
        with gp.Gamepad(pad_info["path"]) as pad:
            while not stop_flag["value"] and pad.alive:
                for kind, name, value in pad.poll(timeout=0.05):
                    if kind == "button":
                        if name == "start" and value:
                            stop_flag["value"] = True
                        elif name == "a" and value:
                            chassis.stop()
                            last = (0.0, 0.0, 0.0)
                        elif name == "b" and value:
                            board.beep()

                lx, ly = pad.axes["lx"], pad.axes["ly"]
                rx = pad.axes["rx"]
                if cfg.swap_sticks:
                    lx, rx = rx, lx
                dead = cfg.deadzone
                ly = 0.0 if abs(ly) < dead else ly
                lx = 0.0 if abs(lx) < dead else lx
                rx = 0.0 if abs(rx) < dead else rx
                if cfg.invert_throttle:
                    ly = -ly
                if cfg.invert_strafe:
                    lx = -lx
                if cfg.invert_steering:
                    rx = -rx

                if pad.keys.get("a"):
                    velocity = (0.0, 0.0, 0.0)
                else:
                    velocity = (ly * cfg.max_linear, lx * cfg.max_linear, rx * cfg.max_angular)

                # 值变了立刻发；值不变也按 --resend 周期重发，兼作心跳
                rounded = tuple(round(v, 3) for v in velocity)
                now = time.time()
                if rounded != last or now - last_send >= args.resend:
                    chassis.set_velocity(*velocity)
                    last, last_send = rounded, now
                else:
                    time.sleep(0.01)
        if not pad.alive:
            print("gamepad disconnected -> stop")
    finally:
        chassis.stop()
        board.set_led(0.1, 0.0, 1, 1)
        board.close()
        print("stopped")
    return 0


# -------------------------------------------------------- beep / led / servo
def cmd_beep(args):
    board = open_board(build_config(args))
    try:
        board.set_buzzer(args.freq, args.on, args.off, args.repeat)
        print("buzzer sent: %d Hz on=%.2fs off=%.2fs repeat=%d (tx=%d)" % (
            args.freq, args.on, args.off, args.repeat, board.tx_bytes))
        return 0
    finally:
        board.close()


def cmd_led(args):
    board = open_board(build_config(args))
    try:
        board.set_led(args.on, args.off, args.repeat, args.id)
        print("led %d sent: on=%.2fs off=%.2fs repeat=%d (tx=%d)" % (
            args.id, args.on, args.off, args.repeat, board.tx_bytes))
        return 0
    finally:
        board.close()


def cmd_servo(args):
    board = open_board(build_config(args))
    try:
        board.pwm_servo_set_position(args.duration, [(args.id, args.position)])
        print("servo %d -> %d us over %.2fs (tx=%d)" % (
            args.id, args.position, args.duration, board.tx_bytes))
        return 0
    finally:
        board.close()


# ------------------------------------------------------------------ main
def build_parser():
    parser = argparse.ArgumentParser(
        prog="turbopi", description="Hiwonder TurboPi standalone controller (no ROS2)")
    parser.add_argument("--version", action="version", version="turbopi %s" % __version__)
    subparsers = parser.add_subparsers(dest="command")

    p = subparsers.add_parser("list", help="list serial + input devices")
    p.set_defaults(func=cmd_list)

    p = subparsers.add_parser("selftest", help="check env, print kinematics, no motion")
    add_common(p)
    p.add_argument("--pulse", action="store_true", help="also beep + blink LED")
    p.set_defaults(func=cmd_selftest)

    p = subparsers.add_parser("bench", help="run each direction for --ms (lift the car!)")
    add_common(p)
    p.add_argument("--duty", type=float, default=20.0, help="not used, see chassis scaling")
    p.add_argument("--ms", type=int, default=800)
    p.set_defaults(func=cmd_bench)

    p = subparsers.add_parser("raw", help="send four duty or speed values directly")
    add_common(p)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--duty", help="four duty percentages, e.g. 20,20,20,20")
    group.add_argument("--speed", help="four speeds m/s, e.g. 0.1,0.1,0.1,0.1")
    p.add_argument("--ms", type=int, default=800)
    p.set_defaults(func=cmd_raw)

    p = subparsers.add_parser("sensors", help="read battery/imu/key for a while")
    add_common(p)
    p.add_argument("--seconds", type=float, default=5.0)
    p.set_defaults(func=cmd_sensors)

    p = subparsers.add_parser("drive", help="drive with a gamepad (evdev)")
    add_common(p)
    p.add_argument("--pad", help="explicit /dev/input/eventN")
    p.add_argument("--resend", type=float, default=0.5,
                   help="re-send the same command every N seconds (heartbeat), default 0.5")
    p.set_defaults(func=cmd_drive)

    p = subparsers.add_parser("beep", help="buzzer test")
    add_common(p)
    p.add_argument("--freq", type=int, default=2000)
    p.add_argument("--on", type=float, default=0.2)
    p.add_argument("--off", type=float, default=0.1)
    p.add_argument("--repeat", type=int, default=1)
    p.set_defaults(func=cmd_beep)

    p = subparsers.add_parser("led", help="led test")
    add_common(p)
    p.add_argument("--id", type=int, default=1)
    p.add_argument("--on", type=float, default=0.5)
    p.add_argument("--off", type=float, default=0.0)
    p.add_argument("--repeat", type=int, default=1)
    p.set_defaults(func=cmd_led)

    p = subparsers.add_parser("servo", help="pwm servo test")
    add_common(p)
    p.add_argument("--id", type=int, default=1)
    p.add_argument("--position", type=int, default=1500, help="pulse width in us")
    p.add_argument("--duration", type=float, default=0.5)
    p.set_defaults(func=cmd_servo)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(getattr(args, "log_level", "info"))
    if not getattr(args, "func", None):
        parser.print_help()
        print("\nhint: start with  python3 -m turbopi selftest")
        return 2
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print("!! %s" % exc)
        return 1
    except PermissionError as exc:
        print("!! permission denied: %s" % exc)
        print("   docker: add --device /dev/ttyS1   host: sudo or udev MODE=0666")
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
