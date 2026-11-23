"""I2C 后端的 Board —— 对应「树莓派4B扩展板 A款(DC接口)」，主控 STC8H1K08。

为什么需要它：
    之前那套 Board（board.py）走的是 Pi5/TurboPi 那代板的 AA55 串口协议。
    而这块 4B 板完全不用串口控制电机 —— 串口只接总线舵机。它的电机、舵机角度、
    电池全在 I2C 从机 0x7A 上（幻尔 HiwonderSDK/Board.py 就是这个模型）。

寄存器映射（逐字节照抄官方 Board.py）：
    setMotor(index, speed)      -> 寄存器 31+index-1，写 int8(-speed)，范围 -100..100
    setPWMServoAngle(id, angle) -> 寄存器 21+id-1，写 uint8 0..180
    getBattery()                -> 寄存器 0，读 2 字节小端，单位 mV

两个坑（实测）：
    1. 官方 setMotor 内部先 speed = -speed 再写。所以"正数=前进"对应线上写负值。
       本模块用 invert=True 复现这个行为；实车验证过 M1 写 -35 → 左前轮前进。
    2. i2cdetect 探测不到 0x7A！这颗 MCU 的 I2C 从机不响应 SMBus 的快速探测命令，
       但正常的"写寄存器 / 写寄存器再读回"是通的。别用 i2cdetect 判断板子在不在，
       用 probe()（读电池寄存器）。

蜂鸣器不在这条总线上：它由 40pin pin31 直接驱动三极管，属于 GPIO，和 MCU 无关。
"""

import errno
import fcntl
import logging
import os
import struct
import threading
import time

log = logging.getLogger(__name__)

I2C_SLAVE = 0x0703

BOARD_ADDR = 0x7A
REG_BATTERY = 0
REG_SERVO = 21          # 21..26
REG_MOTOR = 31          # 31..34

MOTOR_IDS = (1, 2, 3, 4)

# 40pin 两组 I2C 在 M2 上的总线号：组A=pin3/5 -> i2c-3，组B=pin27/28 -> i2c-7
DEFAULT_BUSES = (3, 7)


class I2CBoard(object):
    """和 board.Board 同一套写接口，所以 MecanumChassis / donkey 执行器不用改。"""

    def __init__(self, bus=3, addr=BOARD_ADDR, invert=True, signs=None):
        self.bus = int(bus)
        self.addr = int(addr)
        self.invert = bool(invert)
        # 每路电机的"前进符号"：正装 +1，反装 -1。实车标定得到，默认 1,-1,1,1
        self.signs = list(signs) if signs else [1, 1, 1, 1]
        if len(self.signs) != 4:
            raise ValueError("signs 需要 4 个值")
        self.path = "/dev/i2c-%d" % self.bus
        self.device = "i2c-%d@0x%02X" % (self.bus, self.addr)
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.frames_in = 0
        self.battery = None          # mV
        self.imu = None              # 这块板没有 IMU
        self.button = None
        self._motors = [0.0] * 4
        self._motors_active = False
        self._last_mode = "duty"
        self._lock = threading.Lock()
        self._fd = None
        # 运动学要用"每轮自己的前进速度"，不要官方那套取反（符号已在 signs 里）
        self.wheel_frame = True
        self._open()

    # ------------------------------------------------------------ 底层
    def _open(self):
        self._fd = os.open(self.path, os.O_RDWR)
        fcntl.ioctl(self._fd, I2C_SLAVE, self.addr)

    def _write_reg(self, reg, payload):
        buf = bytes([reg]) + bytes(payload)
        with self._lock:
            written = os.write(self._fd, buf)
        self.tx_bytes += len(buf)
        log.debug("i2c tx %02X %s", reg, buf[1:].hex(" "))
        return written

    def _read_reg(self, reg, count):
        with self._lock:
            os.write(self._fd, bytes([reg]))
            data = os.read(self._fd, count)
        self.rx_bytes += len(data)
        self.frames_in += 1
        return data

    # ------------------------------------------------------------ 探测
    @classmethod
    def probe(cls, bus, addr=BOARD_ADDR):
        """读寄存器 0（电池）来判断板子在不在。返回 I2CBoard 或 None。"""
        path = "/dev/i2c-%d" % bus
        import os
        if not os.path.exists(path):
            return None
        try:
            board = cls(bus, addr)
            board.read_battery()
            return board
        except (OSError, IOError) as exc:
            if isinstance(exc, OSError) and exc.errno not in (errno.EREMOTEIO, errno.EIO):
                log.debug("i2c-%d 打开失败: %s", bus, exc)
            else:
                log.debug("i2c-%d 上 0x%02X 无应答: %s", bus, addr, exc)
            return None

    @classmethod
    def probe_first(cls, buses=None, addr=BOARD_ADDR, invert=True, signs=None):
        for bus in buses or DEFAULT_BUSES:
            board = cls(bus, addr, invert, signs) if os.path.exists("/dev/i2c-%d" % bus) else None
            if board is None:
                continue
            try:
                board.read_battery()
                return board
            except (OSError, IOError) as exc:
                log.debug("i2c-%d 上 0x%02X 无应答: %s", bus, addr, exc)
                board.close()
        return None

    # ------------------------------------------------------------ 电机
    def set_motor_duty(self, dutys):
        """四个占空比，单位 %，正数=前进。内部按官方语义取负后写 int8。"""
        dutys = [float(v) for v in dutys]
        if len(dutys) != 4:
            raise ValueError("需要 4 个占空比，收到 %d 个" % len(dutys))
        for index, duty in enumerate(dutys):
            duty = max(-100.0, min(100.0, duty)) * self.signs[index]
            wire = -duty if self.invert else duty
            value = int(round(wire))
            value = max(-128, min(127, value))
            self._write_reg(REG_MOTOR + index, struct.pack("<b", value))
        self._motors = dutys
        self._motors_active = any(abs(v) > 1e-9 for v in dutys)
        self._last_mode = "duty"
        return dutys

    def set_motor_speed(self, speeds):
        """这块板没有速度环，速度按"最大线速度线性映射到占空比"处理。"""
        speeds = [float(v) for v in speeds]
        peak = max([abs(v) for v in speeds] + [1e-6])
        scale = 100.0 / peak if peak > 0 else 0.0
        duties = [v * scale for v in speeds]
        if peak <= 1e-6:
            duties = [0.0] * 4
        self._last_mode = "speed"
        return self.set_motor_duty(duties)

    def stop_motors(self):
        if not self._motors_active:
            return None
        for index in range(4):
            self._write_reg(REG_MOTOR + index, b"\x00")
        self._motors = [0.0] * 4
        self._motors_active = False
        return [0.0] * 4

    def mark_mode(self, mode):
        self._last_mode = mode

    # ------------------------------------------------------------ 电池
    def read_battery(self):
        """读电池 mV。这颗 MCU 在刚写过电机之后偶尔会回无效值，所以做合理性重试。"""
        last = None
        for _ in range(3):
            raw = self._read_reg(REG_BATTERY, 2)
            if len(raw) < 2:
                continue
            last = int.from_bytes(raw[:2], "little")
            if 3000 <= last <= 30000:
                self.battery = last
                return last
        if last is not None and not (3000 <= last <= 30000):
            raise OSError(errno.EIO, "电池读数不合理: %d mV" % last)
        raise OSError(errno.EIO, "电池寄存器读回字节不足")

    def get_battery(self):
        return self.read_battery()

    # ------------------------------------------------------------ 舵机
    def pwm_servo_set_position(self, duration=0.0, positions=None):
        """positions: [(id, angle), ...]，id 1..6，angle 0..180（这块板按角度走 I2C）。"""
        for servo_id, angle in (positions or []):
            if not 1 <= int(servo_id) <= 6:
                raise ValueError("PWM 舵机编号只能是 1~6")
            value = max(0, min(180, int(angle)))
            self._write_reg(REG_SERVO + int(servo_id) - 1, bytes([value]))
        return positions

    def pwm_servo_set_offset(self, servo_id, offset):
        log.info("这块板的舵机偏差只能通过命令块设置，I2C 后端暂不支持")

    # ------------------------------------------- 这块板没有的功能（保持接口）
    def set_led(self, on_time=1.0, off_time=0.0, repeat=1, led_id=1):
        log.debug("I2C 板没有这个 LED 接口，忽略")

    def set_buzzer(self, freq=2000, on_time=0.25, off_time=0.15, repeat=3):
        log.info("蜂鸣器不在 I2C 上：由 40pin pin31 直接驱动，请用 GPIO 控制")

    def beep(self):
        self.set_buzzer()

    def set_rgb(self, pixels):
        log.debug("I2C 板没有 RGB 接口，忽略")

    def start_reception(self):
        return None

    def stop_reception(self):
        return None

    # ------------------------------------------------------------ 生命周期
    def close(self):
        if self._fd is not None:
            try:
                self.stop_motors()
            except (OSError, IOError):
                pass
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        return "<I2CBoard %s>" % self.device
