"""把幻尔驱动层与 donkeycar 框架装配成一辆车。

装配出来的数据流（donkeycar 的标准 Part 图）：

    gamepad(evdev) --> user/angle, user/throttle
                            |
                            +--> mecanum actuator --> 幻尔扩展板 (AA55 帧)
                            |
    camera ---> cam/image_array
                            |
                            +--> tub writer (采集训练数据，可选)
"""

import logging
import os
import time

log = logging.getLogger(__name__)


class RecordToggle:
    """把恒定值发布成 'recording'，给 TubWriter 的 run_condition 用。"""

    def __init__(self, enabled=True):
        self.enabled = bool(enabled)

    def run(self):
        return self.enabled

    def shutdown(self):
        pass


class DryRunBoard:
    """不接硬件的扩展板替身：只记录下发的指令，供 manage.py check / CI 使用。

    它模仿 turbopi.board.Board 的写接口，所以可以直接喂给真正的 MecanumChassis
    （运动学是真的，只有串口被换掉）。
    """

    def __init__(self):
        self.frames = []
        self.last_duty = None
        self.last_speed = None
        self.stopped = 0

    def set_motor_duty(self, dutys):
        self.last_duty = tuple(round(float(v), 3) for v in dutys)
        self.frames.append(("duty", self.last_duty))
        return self.last_duty

    def set_motor_speed(self, speeds):
        self.last_speed = tuple(round(float(v), 3) for v in speeds)
        self.frames.append(("speed", self.last_speed))
        return self.last_speed

    def stop_motors(self):
        self.stopped += 1
        return None

    def mark_mode(self, _mode):
        pass

    def close(self):
        self.stopped += 1


# 手柄 -> 执行器的旁路通道：donkey 的 part 只有 (steering, throttle) 两路，
# 麦轮横移(vy)/原地旋转(wz) 走这里传。
SHARED = {}


def make_gamepad(cfg):
    from .parts.gamepad_evdev import GamepadInput
    return GamepadInput(cfg, shared=SHARED)


def make_actuator(cfg, chassis=None):
    from .parts.mecanum_actuator import MecanumActuator
    if chassis is None:
        from .hal import build_chassis
        chassis, board = build_chassis(cfg)
        return MecanumActuator(chassis, cfg, shared=SHARED), chassis, board
    return MecanumActuator(chassis, cfg, shared=SHARED), chassis, None


def make_camera(cfg):
    from donkeycar.parts.camera import Camera
    return Camera(
        image_w=int(getattr(cfg, "IMAGE_W", 160)),
        image_h=int(getattr(cfg, "IMAGE_H", 120)),
        image_d=3,
        framerate=int(getattr(cfg, "CAMERA_FRAMERATE", 20)),
    )


def make_tub_writer(cfg, use_camera):
    from donkeycar.parts.tub_v2 import TubWriter

    inputs, types = [], []
    if use_camera:
        inputs.append("cam/image_array")
        types.append("image_array")
    inputs += ["user/angle", "user/throttle"]
    types += ["float", "float"]

    data_path = getattr(cfg, "DATA_PATH", os.path.expanduser("~/mycar/data"))
    if getattr(cfg, "AUTO_CREATE_NEW_TUB", True):
        tub_path = os.path.join(data_path, "tub_%s" % time.strftime("%Y%m%d_%H%M%S"))
    else:
        tub_path = data_path
    os.makedirs(os.path.dirname(tub_path) or ".", exist_ok=True)
    writer = TubWriter(tub_path, inputs=inputs, types=types,
                       metadata=["car_name:%s" % getattr(cfg, "CAR_NAME", "turbopi-m2")])
    return writer, tub_path, inputs, types


def build_vehicle(cfg, chassis=None, camera=None, gamepad=None):
    """返回 (Vehicle, info dict)。chassis/camera/gamepad 可注入，便于无硬件测试。"""
    import donkeycar as dk

    V = dk.vehicle.Vehicle()
    info = {"parts": [], "tub_path": None}

    use_camera = bool(getattr(cfg, "USE_CAMERA", False))
    if use_camera:
        cam = camera or make_camera(cfg)
        V.add(cam, outputs=["cam/image_array"], threaded=True)
        info["parts"].append("camera(%s)" % type(cam).__name__)

    use_gamepad = bool(getattr(cfg, "USE_GAMEPAD", True))
    if use_gamepad:
        pad = gamepad or make_gamepad(cfg)
        V.add(pad, outputs=["user/angle", "user/throttle"], threaded=True)
        info["parts"].append("gamepad(%s)" % (pad.pad_info or {}).get("path", "injected"))

    actuator, chassis_obj, board = make_actuator(cfg, chassis=chassis)
    V.add(actuator, inputs=["user/angle", "user/throttle"], threaded=True)
    info["parts"].append("actuator(%s)" % type(chassis_obj).__name__)

    use_tub = bool(getattr(cfg, "USE_TUB", False))
    if use_tub:
        writer, tub_path, _inputs, _types = make_tub_writer(cfg, use_camera)
        V.add(RecordToggle(True), outputs=["recording"])
        V.add(writer, inputs=(["cam/image_array"] if use_camera else [])
              + ["user/angle", "user/throttle"],
              outputs=["tub/num_records"], run_condition="recording")
        info["parts"].append("tub_writer")
        info["tub_path"] = tub_path

    info["actuator"] = actuator
    info["chassis"] = chassis_obj
    info["board"] = board
    info["loop_hz"] = int(getattr(cfg, "DRIVE_LOOP_HZ", 20))
    return V, info


def describe(cfg, info):
    lines = [
        "car            : %s" % getattr(cfg, "CAR_NAME", "?"),
        "loop           : %d Hz" % info["loop_hz"],
        "parts          : %s" % ", ".join(info["parts"]),
        "serial         : %s @ %s" % (getattr(cfg, "SERIAL_DEVICE", "") or "(auto)",
                                      getattr(cfg, "SERIAL_BAUD", 1000000)),
        "limits         : %.2f m/s, %.2f rad/s, duty cap %s, mode %s" % (
            getattr(cfg, "MAX_LINEAR", 0.5), getattr(cfg, "MAX_ANGULAR", 2.0),
            getattr(cfg, "MAX_DUTY", 100), getattr(cfg, "MOTOR_MODE", "duty")),
    ]
    if info.get("tub_path"):
        lines.append("tub            : %s" % info["tub_path"])
    return "\n".join(lines)
