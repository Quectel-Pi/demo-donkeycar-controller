"""配置：全部可用环境变量覆盖，方便 docker run -e 调参而不用改镜像。"""

import os

# 串口候选（按优先级）。/dev/rrc 是 udev 软链，重启后可能丢，所以留 ttyS1 兜底。
DEVICE_CANDIDATES = ("/dev/rrc", "/dev/ttyS1", "/dev/ttyHS2", "/dev/ttyACM0")

# 4B A款(DC接口)扩展板的 I2C 从机地址（STC8H1K08）
BOARD_I2C_ADDR = 0x7A


def _env_str(name, default):
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_float(name, default):
    try:
        return float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


def _env_int(name, default):
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "y")


class Config:
    """所有可调参数。默认值来自幻尔官方代码：

    - 轮距/轴距：controller/mecanum.py 的 wheelbase=0.1368, track=0.1410
    - 速度上限：peripherals/launch/joystick_control.launch.py 的 max_linear=0.5,
      max_angular=2.0（手柄节点就是按这个把摇杆映射成 Twist 的）
    - 摇杆死区：同文件 min_value=0.1
    """

    def __init__(self, **overrides):
        self.device = overrides.get("device") or _env_str("TURBOPI_DEVICE", "")
        self.baud = int(overrides.get("baud") or _env_int("TURBOPI_BAUD", 1000000))
        self.wheelbase = _env_float("TURBOPI_WHEELBASE", 0.1368)
        self.track_width = _env_float("TURBOPI_TRACK", 0.1410)
        self.max_linear = _env_float("TURBOPI_MAX_LINEAR", 0.5)
        self.max_angular = _env_float("TURBOPI_MAX_ANGULAR", 2.0)
        self.max_duty = _env_float("TURBOPI_MAX_DUTY", 100.0)
        self.deadzone = _env_float("TURBOPI_DEADZONE", 0.1)
        self.timeout = _env_float("TURBOPI_TIMEOUT", 0.6)
        self.motor_mode = _env_str("TURBOPI_MOTOR_MODE", "duty").lower()
        # 驱动后端：serial（AA55 串口，Pi5/TurboPi 板）| i2c（0x7A，4B A款 DC 板）| auto
        self.backend = _env_str("TURBOPI_BACKEND", "auto").lower()
        self.i2c_buses = _env_str("TURBOPI_I2C_BUSES", "3,7")
        self.i2c_addr = _env_int("TURBOPI_I2C_ADDR", BOARD_I2C_ADDR)
        self.i2c_invert = _env_bool("TURBOPI_I2C_INVERT", True)
        self.i2c_signs = _env_str("TURBOPI_I2C_SIGNS", "1,-1,1,-1")
        self.invert_steering = _env_bool("TURBOPI_INVERT_STEERING", True)
        self.invert_throttle = _env_bool("TURBOPI_INVERT_THROTTLE", True)
        self.invert_strafe = _env_bool("TURBOPI_INVERT_STRAFE", False)
        self.swap_sticks = _env_bool("TURBOPI_SWAP_STICKS", False)
        for key, value in overrides.items():
            if value is not None and hasattr(self, key):
                setattr(self, key, value)

    def as_dict(self):
        return {
            "device": self.device or "(auto)",
            "baud": self.baud,
            "wheelbase": self.wheelbase,
            "track_width": self.track_width,
            "max_linear": self.max_linear,
            "max_angular": self.max_angular,
            "max_duty": self.max_duty,
            "deadzone": self.deadzone,
            "motor_mode": self.motor_mode,
            "backend": self.backend + ("(%s)" % self.i2c_buses if self.backend != "serial" else ""),
            "invert_steering": self.invert_steering,
            "invert_throttle": self.invert_throttle,
            "invert_strafe": self.invert_strafe,
        }


def resolve_device(explicit=None):
    """按 显式参数 -> 环境变量 -> 候选列表 的顺序找串口。"""
    if explicit:
        return explicit
    env = os.environ.get("TURBOPI_DEVICE", "").strip()
    if env:
        return env
    for path in DEVICE_CANDIDATES:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "no serial device found (tried %s). Set TURBOPI_DEVICE, or check that the "
        "udev rule created /dev/rrc." % ", ".join(DEVICE_CANDIDATES)
    )
