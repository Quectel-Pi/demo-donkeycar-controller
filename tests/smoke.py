#!/usr/bin/env python3
"""无硬件的冒烟测试：用 pty 假装扩展板，跑一遍 CLI 的全部命令。

它验证的是"代码 -> 串口字节"这一段：
  - CLI 各子命令能正常启动/退出（退出码符合预期）
  - 真正写进串口的是合法的幻尔帧（帧头/长度/CRC 全部自洽）
  - 电机命令之后一定跟着一帧全 0（停车）
  - 参数（--duty / --ms）确实反映到字节里

运行（Linux，需要 pty）：
    PYTHONPATH=src python3 tests/smoke.py
"""

import os
import pty
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from turbopi import protocol as P          # noqa: E402

ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))


def decode(payload):
    parser = P.FrameParser()
    frames = parser.feed(payload)
    return frames


def run(argv, master_fd, stdin_text="\n", timeout=25):
    start = time.time()
    proc = subprocess.Popen([sys.executable, "-m", "turbopi"] + argv, env=ENV,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    if stdin_text is not None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass
    try:
        proc.stdin.close()          # 关掉 stdin：需要确认的 input() 立刻返回
    except (BrokenPipeError, ValueError):
        pass
    out = []
    os.set_blocking(master_fd, False)
    while True:
        try:
            chunk = os.read(master_fd, 4096)
            if chunk:
                out.append(chunk)
        except (BlockingIOError, OSError):
            pass
        if proc.poll() is not None:
            break
        if time.time() - start > timeout:
            proc.kill()
            break
        time.sleep(0.02)
    # 收尾，把剩余字节读完
    time.sleep(0.05)
    while True:
        try:
            chunk = os.read(master_fd, 4096)
            if not chunk:
                break
            out.append(chunk)
        except (BlockingIOError, OSError):
            break
    stdout = proc.stdout.read()
    return proc.returncode, stdout, b"".join(out)


def describe(frame):
    func, data = frame
    names = {P.FUNC_LED: "LED", P.FUNC_BUZZER: "BUZZER", P.FUNC_MOTOR: "MOTOR"}
    name = names.get(func, "FUNC_%d" % func)
    if func == P.FUNC_MOTOR and data:
        kind = {P.MOTOR_SUB_DUTY: "duty", P.MOTOR_SUB_SPEED: "speed"}.get(
            data[0], "sub%#x" % data[0])
        values = []
        offset = 2
        while offset + 5 <= len(data):
            _index, value = __import__("struct").unpack("<Bf", data[offset:offset + 5])
            values.append(round(value, 2))
            offset += 5
        return "%s/%s %s" % (name, kind, values)
    return "%s %s" % (name, data.hex(" "))


def main():
    failures = []

    def check(label, ok, detail=""):
        print("  [%s] %-46s %s" % ("PASS" if ok else "FAIL", label, detail))
        if not ok:
            failures.append(label)

    master, slave = pty.openpty()
    device = os.ttyname(slave)
    os.environ["TURBOPI_DEVICE"] = device
    ENV["TURBOPI_DEVICE"] = device
    print("fake expansion board on pty: %s\n" % device)

    print("== selftest (no motion)")
    code, out, raw = run(["selftest"], master)
    check("selftest exit 0", code == 0, "exit=%s" % code)
    check("selftest mentions kinematics", "kinematics" in out)

    def is_stop(frame):
        if frame[0] != P.FUNC_MOTOR:
            return False
        data = frame[1]
        return all(__import__("struct").unpack("<Bf", data[i:i + 5])[1] == 0
                   for i in range(2, len(data), 5))

    check("selftest writes no motion (only zero frames)",
          all(is_stop(f) for f in decode(raw)), "frames=%s" % [describe(f) for f in decode(raw)])

    print("== beep + led")
    code, out, raw = run(["beep", "--freq", "3000", "--repeat", "2"], master)
    frames = [f for f in decode(raw) if f[0] == P.FUNC_BUZZER]
    check("beep exit 0", code == 0, "exit=%s" % code)
    check("beep produced 1 BUZZER frame", len(frames) == 1, str([describe(f) for f in frames]))
    if frames:
        freq = int.from_bytes(frames[0][1][0:2], "little")
        check("beep freq encoded as 3000", freq == 3000, "freq=%d" % freq)

    code, out, raw = run(["led", "--id", "3"], master)
    frames = decode(raw)
    check("led exit 0", code == 0, "exit=%s" % code)
    check("led frame id == 3", bool(frames) and frames[0][1][0] == 3,
          str([describe(f) for f in frames]))

    print("== raw duty 20,20,20,20 for 300 ms")
    code, out, raw = run(["raw", "--duty", "20,20,20,20", "--ms", "300"], master)
    frames = decode(raw)
    check("raw exit 0", code == 0, "exit=%s" % code)
    check("raw wrote 2 frames (command + stop)", len(frames) == 2,
          str([describe(f) for f in frames]))
    if len(frames) == 2:
        first = frames[0][1]
        values = []
        offset = 2
        while offset + 5 <= len(first):
            _index, value = __import__("struct").unpack("<Bf", first[offset:offset + 5])
            values.append(round(value, 2))
            offset += 5
        check("raw duty values == [20,20,20,20]", values == [20.0, 20.0, 20.0, 20.0],
              str(values))
        stop = frames[1][1]
        stop_values = [round(__import__("struct").unpack("<Bf", stop[i:i + 5])[1], 2)
                       for i in range(2, len(stop), 5)]
        check("stop frame all zero", stop_values == [0.0, 0.0, 0.0, 0.0], str(stop_values))

    print("== bench (auto-enter, expect 6 moves + stops)")
    code, out, raw = run(["bench", "--duty", "12", "--ms", "200"], master, stdin_text="\n")
    frames = decode(raw)
    motors = [f for f in frames if f[0] == P.FUNC_MOTOR]
    check("bench exit 0", code == 0, "exit=%s" % code)
    check("bench wrote 12 motor frames (6 moves + 6 stops)", len(motors) == 12,
          "got %d: %s" % (len(motors), [describe(f) for f in motors[:4]]))
    peaks = []
    for frame in motors:
        data = frame[1]
        values = [abs(__import__("struct").unpack("<Bf", data[i:i + 5])[1])
                  for i in range(2, len(data), 5)]
        peaks.append(max(values) if values else 0)
    moves = peaks[0::2]
    stops = peaks[1::2]
    check("bench peak duty == 12 on every move", all(abs(p - 12) < 0.01 for p in moves),
          str([round(p, 1) for p in moves]))
    check("bench stops between moves", all(p == 0 for p in stops), str(stops))

    print("== sensors (1 s, expects no reply -> exit 3)")
    code, out, raw = run(["sensors", "--seconds", "1"], master, timeout=30)
    check("sensors exit 3 when board is silent", code == 3, "exit=%s" % code)

    print("== drive without gamepad")
    code, out, raw = run(["drive"], master, timeout=15)
    check("drive exit 1 without gamepad", code == 1, "exit=%s" % code)
    check("drive explains what is missing", "no gamepad" in out.lower(), out.strip()[:80])

    print("== parser robustness")
    parser = P.FrameParser()
    good = P.build(P.FUNC_BUZZER, b"\x01\x02\x03")
    stream = b"\x00\xff\xaa" + good + b"\xaa\x55" + b"\x99" + good
    frames = parser.feed(stream)
    check("parser resyncs through garbage", len(frames) >= 1,
          "recovered %d frame(s)" % len(frames))
    partial = P.FrameParser()
    frames = partial.feed(good[:5])
    check("parser waits for partial frame", frames == [])
    frames = partial.feed(good[5:])
    check("parser completes on remaining bytes", len(frames) == 1)

    print("\n%s (%d failures)" % ("SMOKE TEST PASS" if not failures else "FAILURES",
                                  len(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
