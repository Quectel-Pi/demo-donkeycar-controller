"""donkeycar 应用层配置（可选覆盖：环境变量 > 本文件）。

约定：
  - 硬件相关参数（串口/轮距/速度上限）全部下发给 turbopi 驱动层（幻尔协议实现）
  - 世界坐标约定与 donkeycar 一致：angle/throttle 都是 -1..1
  - 这里不放任何 ROS2 概念
"""

import os

# ---------------------------------------------------------------- 车辆身份
CAR_NAME = os.environ.get("DONKEY_CAR_NAME", "turbopi-m2")

# ---------------------------------------------------------------- 数据目录
# 采集/训练数据（donkey think 结构：data/tub_xxx）
DATA_PATH = os.environ.get("DONKEY_DATA_PATH", os.path.join(os.path.expanduser("~"), "mycar", "data"))
MODELS_PATH = os.environ.get("DONKEY_MODELS_PATH", os.path.join(os.path.expanduser("~"), "mycar", "models"))
AUTO_CREATE_NEW_TUB = True

# ---------------------------------------------------------------- 主循环
DRIVE_LOOP_HZ = int(os.environ.get("DONKEY_LOOP_HZ", "20"))
MAX_LOOPS = None

# ------------------------------------------------------------ 幻尔硬件层
# 交给 turbopi 驱动层：不填则自动 /dev/rrc -> /dev/ttyS1 -> /dev/ttyHS2 -> /dev/ttyACM0
SERIAL_DEVICE = os.environ.get("TURBOPI_DEVICE", "")
SERIAL_BAUD = int(os.environ.get("TURBOPI_BAUD", "1000000"))
WHEELBASE = float(os.environ.get("TURBOPI_WHEELBASE", "0.1368"))
TRACK_WIDTH = float(os.environ.get("TURBOPI_TRACK", "0.1410"))
MAX_LINEAR = float(os.environ.get("TURBOPI_MAX_LINEAR", "0.5"))     # m/s，满油门
MAX_ANGULAR = float(os.environ.get("TURBOPI_MAX_ANGULAR", "2.0"))   # rad/s，满舵
MAX_DUTY = float(os.environ.get("TURBOPI_MAX_DUTY", "30"))
MOTOR_MODE = os.environ.get("TURBOPI_MOTOR_MODE", "duty")           # duty(0x05) | speed(0x01)

# ---- 驱动后端 --------------------------------------------------------------
# auto  : 先找 I2C 0x7A（4B A款 DC 板），找不到再用 AA55 串口（Pi5/TurboPi 板）
# i2c   : 只用 I2C 0x7A
# serial: 只用 AA55 串口
BOARD_BACKEND = os.environ.get("TURBOPI_BACKEND", "auto")
I2C_BUSES = os.environ.get("TURBOPI_I2C_BUSES", "3,7")      # 40pin 组A=i2c-3，组B=i2c-7
I2C_ADDR = int(os.environ.get("TURBOPI_I2C_ADDR", "0x7A"), 0)
I2C_INVERT = os.environ.get("TURBOPI_I2C_INVERT", "1") == "1"
# 每路电机的"前进符号"，实车标定：M1 左前 (+1)，M2 与 M1 反向安装 (-1)
I2C_SIGNS = os.environ.get("TURBOPI_I2C_SIGNS", "1,-1,1,-1")

# donkey 的 steering 正方向是右转；幻尔麦轮 wz>0 是左转，所以这里取负
STEERING_SIGN = float(os.environ.get("DONKEY_STEERING_SIGN", "-1"))
# 有些车的角速度换算习惯用"以线速度为单位"，打开后 wz = angle * MAX_ANGULAR * |throttle|
SCALE_ANGULAR_BY_THROTTLE = os.environ.get("DONKEY_SCALE_ANGULAR", "0") == "1"

# 执行器看门狗：这么久没收到新指令就自动停车（防止主循环卡死导致飞车）
ACTUATOR_TIMEOUT = float(os.environ.get("DONKEY_ACTUATOR_TIMEOUT", "0.5"))

# ------------------------------------------------------------ 手柄（evdev）
USE_GAMEPAD = os.environ.get("DONKEY_USE_GAMEPAD", "1") == "1"
GAMEPAD_PATH = os.environ.get("DONKEY_GAMEPAD_PATH", "")   # 空 = 自动挑
GAMEPAD_DEADZONE = float(os.environ.get("DONKEY_DEADZONE", "0.08"))
GAMEPAD_THROTTLE_AXIS = os.environ.get("DONKEY_GAMEPAD_THROTTLE_AXIS", "ly")   # 左摇杆纵向
GAMEPAD_STEERING_AXIS = os.environ.get("DONKEY_GAMEPAD_STEERING_AXIS", "lx")   # 左摇杆横向（和油门同一根杆，游戏手柄式操作）
GAMEPAD_INVERT_THROTTLE = os.environ.get("DONKEY_GAMEPAD_INVERT_THROTTLE", "1") == "1"
# evdev 约定：ABS_Y 向上推是负值，ABS_RX 向右推是正值；
# donkey 约定：angle>0 = 右转、throttle>0 = 前进。所以油门要反相、转向不要反相。
GAMEPAD_INVERT_STEERING = os.environ.get("DONKEY_GAMEPAD_INVERT_STEERING", "0") == "1"
GAMEPAD_ESTOP_BUTTON = os.environ.get("DONKEY_GAMEPAD_ESTOP_BUTTON", "l1")
# 急停键：逗号分隔，任意一个按住即停车（现在绑 B；万一 B 在这只手柄上报不上来，改回 "l1" 立即可用）
GAMEPAD_ESTOP_BUTTONS = os.environ.get("DONKEY_GAMEPAD_ESTOP_BUTTONS", "b,l1")
GAMEPAD_QUIT_BUTTON = os.environ.get("DONKEY_GAMEPAD_QUIT_BUTTON", "start")

GAMEPAD_BOOST_TRIGGER = os.environ.get("DONKEY_GAMEPAD_BOOST_TRIGGER", "rt")   # 扳机加速：rt / lt / 留空

# 右摇杆：横移 + 原地旋转（麦轮特有）。留空字符串表示该轴不用。
GAMEPAD_STRAFE_AXIS = os.environ.get("DONKEY_GAMEPAD_STRAFE_AXIS", "rx")       # 右摇杆左右 -> 横移
GAMEPAD_ROTATE_AXIS = os.environ.get("DONKEY_GAMEPAD_ROTATE_AXIS", "ry")       # 右摇杆上下 -> 原地转
GAMEPAD_STRAFE_SIGN = float(os.environ.get("DONKEY_GAMEPAD_STRAFE_SIGN", "1"))
GAMEPAD_ROTATE_SIGN = float(os.environ.get("DONKEY_GAMEPAD_ROTATE_SIGN", "1"))

# 动作键（按住生效，优先于右摇杆）
GAMEPAD_TURN_LEFT_BUTTON = os.environ.get("DONKEY_GAMEPAD_TURN_LEFT_BUTTON", "x")    # 原地左转
GAMEPAD_TURN_RIGHT_BUTTON = os.environ.get("DONKEY_GAMEPAD_TURN_RIGHT_BUTTON", "y")  # 原地右转
GAMEPAD_STRAFE_BUTTON = os.environ.get("DONKEY_GAMEPAD_STRAFE_BUTTON", "r1")         # 横移模式：转向变平移
GAMEPAD_BOOST_BUTTONS = os.environ.get("DONKEY_GAMEPAD_BOOST_BUTTONS", "a")
GAMEPAD_BOOST_MULT = float(os.environ.get("DONKEY_GAMEPAD_BOOST_MULT", "1.4"))
GAMEPAD_BOOST_DUTY = float(os.environ.get("DONKEY_GAMEPAD_BOOST_DUTY", "100"))

# 后端：auto（先找 evdev，找不到再走 usbfs 直读 XInput）/ evdev / xinput
# M2 车机内核 CONFIG_INPUT_JOYSTICK=n，没有 xpad，所以只剩 xinput 这条路可用。
GAMEPAD_BACKEND = os.environ.get("DONKEY_GAMEPAD_BACKEND", "auto")
GAMEPAD_VID = int(os.environ.get("DONKEY_GAMEPAD_VID", "0x37d7"), 0)   # Flydigi
GAMEPAD_PID = int(os.environ.get("DONKEY_GAMEPAD_PID", "0x2501"), 0)   # APEX5 接收器

# ------------------------------------------------------------ 摄像头（可选）
USE_CAMERA = os.environ.get("DONKEY_USE_CAMERA", "0") == "1"
CAMERA_TYPE = os.environ.get("DONKEY_CAMERA_TYPE", "WEBCAM")   # WEBCAM | PICAM | CSICAMERA | IMAGE_LIST
CAMERA_INDEX = int(os.environ.get("DONKEY_CAMERA_INDEX", "0"))
IMAGE_W = int(os.environ.get("DONKEY_IMAGE_W", "160"))
IMAGE_H = int(os.environ.get("DONKEY_IMAGE_H", "120"))
CAMERA_FRAMERATE = int(os.environ.get("DONKEY_CAMERA_FPS", "20"))

# ------------------------------------------------------------------- 记录
RECORD_DURING_AI = False
HAVE_CONSOLE_LOGGING = True
LOGGING_LEVEL = "INFO"
LOGGING_FORMAT = "%(asctime)s %(levelname)s %(message)s"
