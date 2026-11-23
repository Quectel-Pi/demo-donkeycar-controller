"""幻尔 TurboPi 扩展板串口协议。

帧格式与 CRC 表逐字节取自官方 ros_robot_controller_sdk.py：
  - buf_write()：buf = [0xAA, 0x55, func, len] + data + crc8(buf[2:])
  - recv_task()：状态机顺序为 帧头1 -> 帧头2 -> 功能码 -> 长度 -> 数据 -> 校验
注意：官方文件顶部那句 "0xAA 0x55 Length Function ID Data Checksum" 的注释是错的
（把长度写在了功能码前面），实际代码与接收状态机都是 **功能码在前、长度在后**，
本文件按实际代码实现。

本模块只用标准库，不依赖官方 SDK / pyserial / ROS2。
"""

import struct

# ---------------------------------------------------------------- 功能码
FUNC_SYS = 0
FUNC_LED = 1
FUNC_BUZZER = 2
FUNC_MOTOR = 3
FUNC_PWM_SERVO = 4
FUNC_BUS_SERVO = 5
FUNC_KEY = 6
FUNC_IMU = 7
FUNC_GAMEPAD = 8
FUNC_SBUS = 9
FUNC_OLED = 10
FUNC_RGB = 11
FUNC_NONE = 12

# 电机子命令
MOTOR_SUB_SPEED = 0x01  # 速度环
MOTOR_SUB_DUTY = 0x05   # 占空比环（ROS2/donkey 实际用的都是这个）

# 系统回报（扩展板 -> 主机）
SYS_BATTERY = 0x04

HEADER = b"\xaa\x55"

# 官方 crc8_table（256 项，由 tools/inject_crc.py 从官方 SDK 原文件注入）
CRC8_TABLE = []


def crc8(data):
    """与官方 checksum_crc8() 等价。"""
    if not CRC8_TABLE:
        raise RuntimeError(
            "CRC8_TABLE is empty: run 'python3 tools/verify.py' to inject the table "
            "from the official ros_robot_controller_sdk.py")
    check = 0
    for byte in bytes(data):
        check = CRC8_TABLE[check ^ byte]
    return check & 0xFF


def build(func, data=b""):
    """组一帧：AA 55 func len data crc"""
    payload = bytes([func & 0xFF, len(data) & 0xFF]) + bytes(data)
    return HEADER + payload + bytes([crc8(payload)])


class FrameParser:
    """与官方 recv_task() 等价的流式解析器，喂多少字节都行。"""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, chunk):
        if chunk:
            self._buf.extend(chunk)
        out = []
        while True:
            # 1) 同步帧头
            while len(self._buf) >= 2 and not (self._buf[0] == 0xAA and self._buf[1] == 0x55):
                del self._buf[0]
            if len(self._buf) < 4:
                return out
            func = self._buf[2]
            if func >= FUNC_NONE:          # 功能码越界 -> 丢一个字节重新同步
                del self._buf[0]
                continue
            length = self._buf[3]
            if len(self._buf) < 4 + length + 1:
                return out
            payload = bytes(self._buf[2:4 + length])
            crc = self._buf[4 + length]
            if crc8(payload) != crc:       # 校验失败 -> 丢一个字节重新同步
                del self._buf[0]
                continue
            out.append((func, bytes(self._buf[4:4 + length])))
            del self._buf[:4 + length + 1]


# ------------------------------------------------- 回报解析（照官方 get_* 实现）
def parse_battery(data):
    """SYS 帧：data[0]==0x04 时 data[1:3] 为电压 mV（官方注释写成 mAh 是笔误）。"""
    if len(data) >= 3 and data[0] == SYS_BATTERY:
        return struct.unpack("<H", data[1:3])[0]
    return None


def parse_button(data):
    """KEY 帧：返回 (按键号, 0=单击/1=按住)。"""
    if len(data) >= 2:
        return data[0], 0 if data[1] == 0x20 else (1 if data[1] == 0x01 else None)
    return None


def parse_imu(data):
    """IMU 帧：<6f = ax, ay, az, gx, gy, gz"""
    if len(data) >= 24:
        return struct.unpack("<6f", data[:24])
    return None


def parse_sbus(data):
    """SBUS 帧：<16hBBBB -> 16 通道 (-1..1)"""
    if len(data) < 36:
        return None
    ch = struct.unpack("<16h", data[:32])
    return [2.0 * (v - 192) / (1792 - 192) - 1.0 for v in ch]
