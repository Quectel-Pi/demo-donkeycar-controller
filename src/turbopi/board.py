"""Board：把幻尔协议封装成"电机 / LED / 蜂鸣器 / 舵机 / 电池 / 按键 / IMU"操作。

对应官方 ros_robot_controller_sdk.Board 的功能子集，但：
  - 不依赖 pyserial（用 RawSerial）
  - 接收不是队列，而是"最新值"快照 + 计数器，更适合做成服务
  - 所有写操作都会累加 tx_bytes，方便排查"到底发出去没有"

官方语义对照（逐字段抄的）：
  set_led       -> AA 55 01 07 <B led_id><H on*1000><H off*1000><H repeat>
  set_buzzer    -> AA 55 02 08 <H freq><H on*1000><H off*1000><H repeat>
  set_motor     -> AA 55 03 16 <B sub><B n> [<B id-1><f value>] * n
  pwm_servo_pos -> AA 55 04 03... <B 0x01><H dur*1000><B n> [<B id><H pos>] * n
  pwm_servo_off -> AA 55 04 03   <B 0x07><B id><b offset>
  set_rgb       -> AA 55 0B ...  <B 0x01><B n> [<B idx-1><B r><B g><B b>] * n
"""

import logging
import struct
import threading
import time

from . import protocol as P
from .serialport import RawSerial

log = logging.getLogger(__name__)

MOTOR_IDS = (1, 2, 3, 4)


class Board:
    def __init__(self, device, baud=1000000):
        self.port = RawSerial(device, baud)
        self.device = device
        self.baud = baud
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.frames_in = 0
        self._motors_active = False
        self._last_mode = "duty"
        self.battery = None      # mV
        self.imu = None          # (ax, ay, az, gx, gy, gz)
        self.sbus = None         # 16 通道
        self.button = None       # (id, state)
        self._parser = P.FrameParser()
        self._lock = threading.Lock()
        self._rx_thread = None
        self._rx_stop = threading.Event()
        log.info("board opened: %s", self.port)

    # ------------------------------------------------------------ 底层
    def _send(self, func, data=b""):
        packet = P.build(func, data)
        with self._lock:
            self.port.write(packet)
            self.tx_bytes += len(packet)
        log.debug("tx %s", packet.hex(" "))
        return packet

    # ------------------------------------------------------------ 电机
    def set_motor_duty(self, dutys):
        """四个占空比，单位 %，范围 -100..100（与幻尔 set_motor_duty 同字节）。"""
        dutys = [float(v) for v in dutys]
        data = bytearray([P.MOTOR_SUB_DUTY, len(dutys)])
        for index, duty in enumerate(dutys):
            data += struct.pack("<Bf", index, duty)
        self._motors_active = any(abs(value) > 1e-9 for value in dutys)
        self._last_mode = "duty"
        return self._send(P.FUNC_MOTOR, bytes(data))

    def set_motor_speed(self, speeds):
        """四个速度，单位 m/s（走扩展板速度环 0x01）。"""
        speeds = [float(v) for v in speeds]
        data = bytearray([P.MOTOR_SUB_SPEED, len(speeds)])
        for index, speed in enumerate(speeds):
            data += struct.pack("<Bf", index, speed)
        self._motors_active = any(abs(value) > 1e-9 for value in speeds)
        self._last_mode = "speed"
        return self._send(P.FUNC_MOTOR, bytes(data))

    def stop_motors(self):
        """停车。已经在停的状态就什么都不发，避免一串重复的全 0 帧。"""
        if not self._motors_active:
            return None
        if self._last_mode == "speed":
            packet = self.set_motor_speed([0.0] * 4)
        else:
            packet = self.set_motor_duty([0.0] * 4)
        self._motors_active = False
        return packet

    # 兼容写法：actuator 用哪种模式，stop 就跟哪种
    def mark_mode(self, mode):
        self._last_mode = mode

    # ------------------------------------------------------- 灯 / 蜂鸣器
    def set_led(self, on_time=1.0, off_time=0.0, repeat=1, led_id=1):
        """on_time/off_time 单位秒。"""
        return self._send(P.FUNC_LED, struct.pack(
            "<BHHH", int(led_id), int(on_time * 1000), int(off_time * 1000), int(repeat)))

    def set_buzzer(self, freq=2000, on_time=0.25, off_time=0.15, repeat=3):
        """freq Hz，times 单位秒。"""
        return self._send(P.FUNC_BUZZER, struct.pack(
            "<HHHH", int(freq), int(on_time * 1000), int(off_time * 1000), int(repeat)))

    def beep(self):
        return self.set_buzzer(2000, 0.15, 0.1, 1)

    # ------------------------------------------------------------ 舵机
    def pwm_servo_set_position(self, duration, positions):
        """duration 秒；positions = [(servo_id, 脉宽ms), ...]"""
        duration = int(duration * 1000)
        data = bytearray([0x01, duration & 0xFF, 0xFF & (duration >> 8), len(positions)])
        for servo_id, position in positions:
            data += struct.pack("<BH", int(servo_id), int(position))
        return self._send(P.FUNC_PWM_SERVO, bytes(data))

    def pwm_servo_set_offset(self, servo_id, offset):
        return self._send(P.FUNC_PWM_SERVO, struct.pack("<BBb", 0x07, int(servo_id), int(offset)))

    # ------------------------------------------------------------- RGB
    def set_rgb(self, pixels):
        """pixels = [(index 1.., r, g, b), ...]"""
        data = bytearray([0x01, len(pixels)])
        for index, r, g, b in pixels:
            data += struct.pack("<BBBB", int(index) - 1, int(r), int(g), int(b))
        return self._send(P.FUNC_RGB, bytes(data))

    # ------------------------------------------------------------ 接收
    def start_reception(self):
        if self._rx_thread is not None:
            return
        self._rx_stop.clear()
        self._rx_thread = threading.Thread(target=self._rx_loop, name="board-rx", daemon=True)
        self._rx_thread.start()

    def stop_reception(self):
        self._rx_stop.set()
        thread, self._rx_thread = self._rx_thread, None
        if thread is not None:
            thread.join(timeout=1.0)

    def _rx_loop(self):
        while not self._rx_stop.is_set():
            try:
                chunk = self.port.read(4096, timeout=0.1)
            except OSError as exc:
                log.warning("serial read error: %s", exc)
                time.sleep(0.2)
                continue
            if not chunk:
                continue
            self.rx_bytes += len(chunk)
            for func, data in self._parser.feed(chunk):
                self.frames_in += 1
                self._dispatch(func, data)

    def _dispatch(self, func, data):
        if func == P.FUNC_SYS:
            voltage = P.parse_battery(data)
            if voltage is not None:
                self.battery = voltage
        elif func == P.FUNC_IMU:
            value = P.parse_imu(data)
            if value is not None:
                self.imu = value
        elif func == P.FUNC_KEY:
            value = P.parse_button(data)
            if value is not None:
                self.button = value
        elif func == P.FUNC_SBUS:
            value = P.parse_sbus(data)
            if value is not None:
                self.sbus = value

    # ------------------------------------------------------------ 收尾
    def close(self):
        try:
            self.stop_motors()
        except Exception as exc:                      # 停机尽力而为，绝不因为抛错卡住退出
            log.warning("stop motors failed: %s", exc)
        self.stop_reception()
        self.port.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
