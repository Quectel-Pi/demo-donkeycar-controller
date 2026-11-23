"""turbopi —— 幻尔 TurboPi 小车的完全独立控制程序。

不依赖 ROS2、不依赖 donkeycar、不依赖 pyserial：
    协议层 protocol.py      幻尔 AA55 帧 + CRC8（字节取自官方 SDK）
    串口层 serialport.py    os + termios，1000000 8N1 raw
    设备层 board.py         电机 / LED / 蜂鸣器 / 舵机 / 电池 / 按键 / IMU
    底盘层 chassis.py       麦克纳姆运动学（照抄官方 mecanum.py）
    输入层 gamepad.py       evdev 手柄
    命令行 cli.py            self-test / bench / raw / drive / sensors
"""

__version__ = "1.0.0"
__all__ = ["protocol", "serialport", "config", "board", "chassis", "gamepad", "cli"]
