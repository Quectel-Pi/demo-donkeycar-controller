#!/usr/bin/env python3
"""4B 扩展板控制示例（M2 版）—— 按 HiwonderSDK/Board.py 的语义重写。

官方 Board.py 依赖三样东西，M2 上都没有：
    import RPi.GPIO as GPIO          -> 没有 RPi.GPIO
    from smbus2 import SMBus         -> 没有 smbus2
    rpi_ws281x                       -> 没有

这里用纯标准库把它们换掉：
    I2C    -> /dev/i2c-N + ioctl(I2C_SLAVE)  + os.write / os.read
    GPIO   -> /sys/class/gpio 导出 + 写 value

对应关系（完全照官方 Board.py 的寄存器与取值）：

    电机   0x7A 寄存器 31..34      写 int8   -100..100（官方内部取负后再写）
    舵机   0x7A 寄存器 21..26      写 uint8  0..180
    电池   0x7A 寄存器 0           读 2 字节 小端 -> 毫伏
    蜂鸣器 40pin pin31 (BCM6)      电平，高=响（不经 I2C，不经串口）

用法：
    python3 tools/i2c_ctrl.py scan                 # 扫两组 40pin I2C，看有没有 0x7A
    python3 tools/i2c_ctrl.py battery              # 读电池（最安全的一次 I2C 访问）
    python3 tools/i2c_ctrl.py motor 1 20           # M1 以 20 前进（架高车轮！）
    python3 tools/i2c_ctrl.py motor 1 0            # 停
    python3 tools/i2c_ctrl.py servo 1 90           # 1 号 PWM 舵机转 90 度
    python3 tools/i2c_ctrl.py buzzer 2             # 蜂鸣器响 2 秒（GPIO 电平法）
    python3 tools/i2c_ctrl.py buzzer 2 --gpio 26   # 手动指定 gpio 号

每次操作都会打印「写进去的字节」和「读回来的字节」，不做任何隐藏。
"""

import argparse
import errno
import fcntl
import os
import struct
import sys
import time

I2C_SLAVE = 0x0703

BOARD_ADDR = 0x7A
REG_BATTERY = 0
REG_SERVO = 21            # 21..26
REG_MOTOR = 31            # 31..34
REG_SERVO_CMD = 40

# 40pin 两组 I2C 在 M2 上的总线号（组 A = pin3/5，组 B = pin27/28）
BUS_CANDIDATES = [
    (3, "组A pin3/5  SDA1/SCL1"),
    (7, "组B pin27/28 SDA0/SCL0"),
]

SERVO_PULSE_MIN = 500
SERVO_PULSE_MAX = 2500


# ------------------------------------------------------------------ I2C 底层
class I2C(object):
    def __init__(self, bus, addr=BOARD_ADDR):
        self.bus = bus
        self.addr = addr
        self.path = "/dev/i2c-%d" % bus
        self.fd = None

    def open(self):
        self.fd = os.open(self.path, os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, self.addr)
        return self

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def write_reg(self, reg, payload):
        buf = bytes([reg]) + bytes(payload)
        written = os.write(self.fd, buf)
        print("    写入 0x%02X -> %s" % (self.addr, buf.hex(" ").upper()))
        return written

    def read_reg(self, reg, count):
        os.write(self.fd, bytes([reg]))
        data = os.read(self.fd, count)
        print("    读取 0x%02X <- %s" % (self.addr, data.hex(" ").upper() or "(空)"))
        return data

    def ack(self):
        """靠一次 1 字节读判断器件在不在：NAK 时内核返回 EREMOTEIO(121)。"""
        try:
            os.read(self.fd, 1)
            return True
        except OSError as exc:
            if exc.errno == errno.EREMOTEIO:
                return False
            return True     # 其它错误（比如器件不支持纯读）也算"有人应答"


def probe(bus, addr):
    path = "/dev/i2c-%d" % bus
    if not os.path.exists(path):
        return None
    try:
        with I2C(bus, addr) as bus_obj:
            return bus_obj.ack()
    except OSError as exc:
        return "错误 %s" % exc.strerror


def find_board():
    """返回能应答 0x7A 的总线号；找不到返回 None。"""
    for bus, note in BUS_CANDIDATES:
        result = probe(bus, BOARD_ADDR)
        if result is None:
            print("  /dev/i2c-%d 不存在，跳过（%s）" % (bus, note))
            continue
        if result is True:
            print("  /dev/i2c-%d 上的 0x7A 有应答 ✓（%s）" % (bus, note))
            return bus
        if result is False:
            print("  /dev/i2c-%d 上的 0x7A 无应答（%s）" % (bus, note))
        else:
            print("  /dev/i2c-%d 访问失败：%s（%s）" % (bus, result, note))
    return None


# ------------------------------------------------------------------ 器件操作
def cmd_scan(args):
    print("== 扫 40pin 两组 I2C，找扩展板控制芯片 0x%02X ==" % BOARD_ADDR)
    found = find_board()
    print()
    if found is None:
        print("结论：两条 40pin I2C 上都没有 0x7A。")
        print("      -> 板上这块不是 4B 一类（I2C 控制）的扩展板；")
        print("         电机/蜂鸣器应该还是走 STM32 + UART（用 tools/hexsend.py 那条路）。")
        return 1
    print("结论：找到了，总线号 /dev/i2c-%d。电机可以走 I2C 寄存器 31~34。" % found)
    return 0


def cmd_battery(args):
    bus = args.bus if args.bus is not None else find_board()
    if bus is None:
        print("没找到 0x7A，先跑 scan")
        return 1
    print("== 读电池电压（寄存器 %d，2 字节小端）==" % REG_BATTERY)
    with I2C(bus, BOARD_ADDR) as dev:
        raw = dev.read_reg(REG_BATTERY, 2)
    if len(raw) < 2:
        print("    读回字节不足，器件没有响应")
        return 1
    millivolt = int.from_bytes(raw[:2], "little")
    print("    原始值 %d  ->  %.2f V" % (millivolt, millivolt / 1000.0))
    return 0


def cmd_motor(args):
    if args.index < 1 or args.index > 4:
        print("电机编号只能是 1~4")
        return 2
    speed = max(-100, min(100, args.speed))
    wire = -speed                      # 官方 Board.py 里就是 speed = -speed 后写下去
    value = struct.pack("<b", wire)[0]
    bus = args.bus if args.bus is not None else find_board()
    if bus is None:
        print("没找到 0x7A，先跑 scan")
        return 1
    reg = REG_MOTOR + (args.index - 1)
    print("== M%d 速度 %+d（线上写 %+d）-> 0x%02X 寄存器 %d =="
          % (args.index, speed, wire, BOARD_ADDR, reg))
    with I2C(bus, BOARD_ADDR) as dev:
        dev.write_reg(reg, [value])
    print("    完成。停车用：python3 %s motor %d 0" % (sys.argv[0], args.index))
    return 0


def cmd_servo(args):
    if args.index < 1 or args.index > 6:
        print("PWM 舵机编号只能是 1~6")
        return 2
    angle = max(0, min(180, args.angle))
    bus = args.bus if args.bus is not None else find_board()
    if bus is None:
        print("没找到 0x7A，先跑 scan")
        return 1
    reg = REG_SERVO + (args.index - 1)
    pulse = int(200 * angle / 9 + 500)
    print("== PWM 舵机 %d 转 %d 度（约 %d us）-> 寄存器 %d ==" % (args.index, angle, pulse, reg))
    with I2C(bus, BOARD_ADDR) as dev:
        dev.write_reg(reg, [angle])
    return 0


# ------------------------------------------------------------------ GPIO 蜂鸣器
def gpio_base_dirs():
    return "/sys/class/gpio"


def detect_buzzer_gpio():
    """尽力自动找 40pin pin31(BCM6) 在 M2 上的 gpio 号：查 debugfs 的标签。"""
    hints = []
    for path in ("/sys/kernel/debug/gpio",):
        try:
            with open(path) as fh:
                for line in fh:
                    low = line.lower()
                    if "40pin" in low and ("31" in low or "gpio6" in low or "io6" in low):
                        hints.append(line.strip())
        except (IOError, OSError):
            pass
    return hints


def gpio_set(number, value):
    base = os.path.join(gpio_base_dirs(), "gpio%d" % number)
    if not os.path.isdir(base):
        with open(os.path.join(gpio_base_dirs(), "export"), "w") as fh:
            fh.write(str(number))
        for _ in range(20):
            if os.path.isdir(base):
                break
            time.sleep(0.05)
    with open(os.path.join(base, "direction"), "w") as fh:
        fh.write("out")
    with open(os.path.join(base, "value"), "w") as fh:
        fh.write("1" if value else "0")


def cmd_buzzer(args):
    number = args.gpio
    if number is None:
        hints = detect_buzzer_gpio()
        if hints:
            print("debugfs 里找到这些和 40pin/pin31 相关的行（供你确认 gpio 号）：")
            for line in hints[:10]:
                print("    " + line)
        print("没有指定 --gpio。40pin pin31 = 树莓派 BCM6，在 M2 上的全局 gpio 号要看 pinmap；")
        print("先用 --gpio N 指定。可参考板上已有的命名：/sys/class/gpio/gpio23 名为 40pin-3v3-enable")
        return 2
    print("== 蜂鸣器：gpio%d 拉高 %s 秒（高电平=响，与官方 setBuzzer(1) 一致）=="
          % (number, args.seconds))
    try:
        gpio_set(number, 1)
        print("    已拉高")
        time.sleep(args.seconds)
    finally:
        try:
            gpio_set(number, 0)
            print("    已拉低（关闭）")
        except OSError as exc:
            print("    关闭失败：%s" % exc)
    return 0


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="4B 扩展板控制示例（M2 版，纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--bus", type=int, default=None, help="手动指定 I2C 总线号，默认自动探测")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("scan", help="扫描 40pin I2C 上的 0x7A")

    p = sub.add_parser("battery", help="读电池电压")

    p = sub.add_parser("motor", help="设置某个直流电机速度")
    p.add_argument("index", type=int, help="电机编号 1~4")
    p.add_argument("speed", type=int, help="速度 -100~100，正=前进")

    p = sub.add_parser("servo", help="设置某个 PWM 舵机角度")
    p.add_argument("index", type=int, help="舵机编号 1~6")
    p.add_argument("angle", type=int, help="角度 0~180")

    p = sub.add_parser("buzzer", help="用 GPIO 电平让蜂鸣器响")
    p.add_argument("seconds", type=float, nargs="?", default=1.0, help="响多久（秒）")
    p.add_argument("--gpio", type=int, default=None, help="40pin pin31 对应的全局 gpio 号")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    return {
        "scan": cmd_scan,
        "battery": cmd_battery,
        "motor": cmd_motor,
        "servo": cmd_servo,
        "buzzer": cmd_buzzer,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
