#!/usr/bin/env python3
"""构建期校验：把 turbopi 的实现逐字节/逐数值对齐幻尔官方源码。

做四件事：
  1. 从官方 ros_robot_controller_sdk.py 抽出 crc8_table，注入 src/turbopi/protocol.py
  2. 用官方 checksum_crc8 对 256 个单字节输入比对我们的 crc8
  3. 对 7 类命令比对"官方 buf_write 生成的帧"与"我们生成的帧"，要求字节完全相同
  4. 对速度网格比对运动学占空比（参考公式抄自官方 controller/mecanum.py）

输出 docs/verify.md，并把结论打到 stdout（PASS/FAIL）。

用法：
    python3 tools/verify.py            # 就地注入 + 校验 + 生成文档
    python3 tools/verify.py --check    # 只校验，不改文件
"""

import argparse
import os
import re
import struct
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

DEFAULT_SDK = os.path.join(
    ROOT, "..", "ai_ros2", "ros2_ws", "src", "driver",
    "ros_robot_controller", "ros_robot_controller", "ros_robot_controller_sdk.py")

results = []


def record(name, ok, detail=""):
    results.append({"name": name, "ok": bool(ok), "detail": detail})
    print("  [%s] %-42s %s" % ("PASS" if ok else "FAIL", name, detail))
    return ok


# ------------------------------------------------------- 官方 SDK 加载
def install_serial_stub():
    """官方 SDK 顶层 import serial；服务器上没有 pyserial，注一个假模块。"""
    stub = types.ModuleType("serial")

    class FakeSerial:
        def __init__(self, *args, **kwargs):
            self.data = bytearray()
            self.rts = False
            self.dtr = False

        def write(self, payload):
            self.data.extend(payload)
            return len(payload)

        def read(self, size=1):
            return b""

        def open(self):
            pass

        def close(self):
            pass

        def isOpen(self):
            return True

        def setPort(self, _port):
            pass

        def reset_input_buffer(self):
            pass

    stub.Serial = FakeSerial
    stub.SerialException = OSError
    sys.modules["serial"] = stub
    return FakeSerial


def load_official(path, fake_serial):
    import importlib.util
    spec = importlib.util.spec_from_file_location("official_sdk", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    board = object.__new__(module.Board)          # 不跑 __init__，只要 buf_write
    board.port = fake_serial()
    return module, board


# --------------------------------------------------------- 注入 CRC 表
def extract_table(path):
    text = open(path, encoding="utf-8").read()
    match = re.search(r"crc8_table\s*=\s*\[(.*?)\]", text, re.S)
    if not match:
        raise SystemExit("crc8_table not found in %s" % path)
    values = [int(v) for v in re.findall(r"\d+", match.group(1))]
    if len(values) != 256:
        raise SystemExit("crc8_table has %d entries, expected 256" % len(values))
    return values


def inject(target, values):
    text = open(target, encoding="utf-8").read()
    block = "CRC8_TABLE = [" + ", ".join(str(v) for v in values) + "]"
    new_text, count = re.subn(r"CRC8_TABLE\s*=\s*\[[^\]]*\]", block, text, count=1, flags=re.S)
    if count == 0:
        raise SystemExit("CRC8_TABLE placeholder not found in %s" % target)
    if new_text != text:
        open(target, "w", encoding="utf-8").write(new_text)
    return block


# ------------------------------------------------------------- 校验项
def check_crc(official_module):
    from turbopi import protocol as own
    official_crc8 = official_module.checksum_crc8
    bad = []
    for value in range(256):
        if own.crc8(bytes([value])) != official_crc8(bytes([value])):
            bad.append(value)
    record("crc8 == official checksum_crc8 (256 inputs)", not bad,
           "" if not bad else "mismatch at %s" % bad[:8])
    multi = b"\x03\x16\x05\x04\x00\x00\x00\xc8\x41"
    record("crc8 on multi-byte payload", own.crc8(multi) == official_crc8(multi))
    return not bad


def check_frames(official_module, official_board, fake_serial):
    from turbopi import board as own_board
    from turbopi import protocol as own_proto

    class FakePort(fake_serial):
        pass

    saved = own_board.RawSerial
    own_board.RawSerial = FakePort
    try:
        ours = own_board.Board("/dev/fake", 1000000)
        cases = [
            ("led", lambda b: b.set_led(1.0, 0.5, 2, 1),
             lambda b: b.set_led(1.0, 0.5, 2, 1)),
            ("buzzer", lambda b: b.set_buzzer(2000, 0.25, 0.15, 3),
             lambda b: b.set_buzzer(2000, 0.25, 0.15, 3)),
            ("motor duty", lambda b: b.set_motor_duty([25, -25, 25, -25]),
             lambda b: b.set_motor_duty([[1, 25], [2, -25], [3, 25], [4, -25]])),
            ("motor speed", lambda b: b.set_motor_speed([0.1, -0.1, 0.1, -0.1]),
             lambda b: b.set_motor_speed([[1, 0.1], [2, -0.1], [3, 0.1], [4, -0.1]])),
            ("pwm servo position", lambda b: b.pwm_servo_set_position(0.5, [(1, 1500), (2, 500)]),
             lambda b: b.pwm_servo_set_position(0.5, [(1, 1500), (2, 500)])),
            ("pwm servo offset", lambda b: b.pwm_servo_set_offset(1, 12),
             lambda b: b.pwm_servo_set_offset(1, 12)),
            ("rgb", lambda b: b.set_rgb([(1, 10, 20, 30), (2, 40, 50, 60)]),
             lambda b: b.set_rgb([(1, 10, 20, 30), (2, 40, 50, 60)])),
        ]
        all_ok = True
        detail = []
        for label, mine_fn, official_fn in cases:
            ours.port.data = bytearray()
            official_board.port.data = bytearray()
            mine_fn(ours)
            official_fn(official_board)
            mine = bytes(ours.port.data)
            official = bytes(official_board.port.data)
            ok = mine == official and len(mine) > 0
            all_ok = all_ok and ok
            detail.append("%s %s" % (label, "identical" if ok else
                                     "MINE=%s OFFICIAL=%s" % (mine.hex(), official.hex())))
            if ok:
                record("frame %s" % label, True, "%d bytes %s" % (len(mine), mine.hex(" ")))
            else:
                record("frame %s" % label, False, detail[-1])
        return all_ok
    finally:
        own_board.RawSerial = saved


def official_duties(vx, vy, wz, max_linear, wheelbase=0.1368, track_width=0.1410):
    """抄自官方 controller/mecanum.py：set_velocity + speed_convert。"""
    k = (wheelbase + track_width) / 2.0
    m1 = vx - vy - wz * k
    m2 = vx + vy - wz * k
    m3 = vx + vy + wz * k
    m4 = vx - vy + wz * k

    def speed_convert(value):
        return max(-100.0, min(100.0, value / max_linear * 100.0))

    return (speed_convert(-m1), speed_convert(m3), speed_convert(-m2), speed_convert(m4))


def check_kinematics():
    from turbopi.chassis import MecanumChassis
    from turbopi.config import Config

    cfg = Config(device="/dev/fake")
    chassis = MecanumChassis(board=None, config=cfg)
    cases = []
    for vx in (-0.5, -0.2, 0.0, 0.15, 0.5):
        for vy in (-0.5, 0.0, 0.3):
            for wz in (-2.0, 0.0, 0.7):
                cases.append((vx, vy, wz))
    bad = []
    for vx, vy, wz in cases:
        mine = chassis.to_duties(vx, vy, wz)
        ref = official_duties(vx, vy, wz, cfg.max_linear, cfg.wheelbase, cfg.track_width)
        if max(abs(a - b) for a, b in zip(mine, ref)) > 1e-9:
            bad.append((vx, vy, wz, mine, ref))
    record("kinematics vs official formula (%d cases)" % len(cases), not bad,
           "" if not bad else "first mismatch %s" % (bad[0],))
    sample = [("forward 0.5", (0.5, 0.0, 0.0)), ("backward 0.5", (-0.5, 0.0, 0.0)),
              ("turn left 2.0", (0.0, 0.0, 2.0)), ("strafe right 0.5", (0.0, 0.5, 0.0))]
    rows = []
    for label, velocity in sample:
        duties = chassis.to_duties(*velocity)
        rows.append((label, velocity, duties))
    return not bad, rows


def write_doc(rows, sdk_path, table_bytes):
    path = os.path.join(ROOT, "docs", "verify.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [
        "# 与幻尔官方实现的逐字节校验",
        "",
        "由 `tools/verify.py` 自动生成，对照对象：",
        "",
        "```",
        os.path.relpath(sdk_path, ROOT),
        "```",
        "",
        "## 结果",
        "",
        "| 校验项 | 结果 | 说明 |",
        "|---|---|---|",
    ]
    for item in results:
        lines.append("| %s | %s | %s |" % (item["name"], "PASS" if item["ok"] else "FAIL",
                                           item["detail"].replace("|", "\\|")))
    lines += [
        "",
        "## 运动学样例",
        "",
        "| 动作 | (vx, vy, wz) | m1 / m2 / m3 / m4 占空比 |",
        "|---|---|---|",
    ]
    for label, velocity, duties in rows:
        lines.append("| %s | %s | %s |" % (
            label, velocity, " / ".join("%+.1f%%" % d for d in duties)))
    lines += [
        "",
        "参考公式（抄自 `controller/mecanum.py`）：",
        "",
        "```",
        "k  = (wheelbase + track_width) / 2",
        "m1 = vx - vy - wz*k",
        "m2 = vx + vy - wz*k",
        "m3 = vx + vy + wz*k",
        "m4 = vx - vy + wz*k",
        "duty[i] = clamp(v[i] / max_linear * 100, -100, 100)",
        "```",
        "",
    ]
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    print("\ndoc -> %s" % path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk", default=DEFAULT_SDK, help="official SDK path")
    parser.add_argument("--check", action="store_true", help="do not modify protocol.py")
    args = parser.parse_args()

    sdk_path = os.path.abspath(args.sdk)
    if not os.path.exists(sdk_path):
        raise SystemExit("official sdk not found: %s" % sdk_path)

    print("== 1. inject crc8_table from official sdk")
    values = extract_table(sdk_path)
    target = os.path.join(ROOT, "src", "turbopi", "protocol.py")
    if args.check:
        from turbopi import protocol as own
        record("crc8_table matches official (no write)", list(own.CRC8_TABLE) == values)
    else:
        inject(target, values)
        record("crc8_table injected (%d entries)" % len(values), True)

    print("== 2. load official sdk with a fake pyserial")
    fake_serial = install_serial_stub()
    official_module, official_board = load_official(sdk_path, fake_serial)

    print("== 3. crc8 equivalence")
    check_crc(official_module)

    print("== 4. frame-by-frame comparison")
    check_frames(official_module, official_board, fake_serial)

    print("== 5. kinematics")
    kin_ok, rows = check_kinematics()

    failed = [item for item in results if not item["ok"]]
    if not args.check:
        write_doc(rows, sdk_path, values)
    print("\n%s  (%d checks, %d failed)" % ("ALL PASS" if not failed else "FAILURES",
                                            len(results), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
