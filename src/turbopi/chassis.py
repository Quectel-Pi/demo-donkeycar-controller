"""麦克纳姆底盘运动学 —— 照抄幻尔 controller/mecanum.py 的公式。

官方 set_velocity(vx, vy, wz)：
    k  = (wheelbase + track_width) / 2
    m1 = vx - vy - wz*k
    m2 = vx + vy - wz*k
    m3 = vx + vy + wz*k
    m4 = vx - vy + wz*k
    speeds = [speed_convert(-m1), speed_convert(m3), speed_convert(-m2), speed_convert(m4)]
其中 speed_convert(v) = v / max_linear_speed * 100，再夹到 ±100 后下发。

与 ROS2 版唯一的区别：这里没有 Twist 消息，直接收 (vx, vy, wz) 三个数。

物理含义（车体坐标系）：
    vx > 0  前进        vy > 0  右平移        wz > 0  左转（逆时针）
"""

import logging

log = logging.getLogger(__name__)

MOTOR_NAMES = ("m1", "m2", "m3", "m4")


def _clamp(value, limit):
    return max(-limit, min(limit, value))


class MecanumChassis:
    def __init__(self, board, config):
        self.board = board
        self.cfg = config
        self.k = (config.wheelbase + config.track_width) / 2.0
        self.last_duties = (0.0, 0.0, 0.0, 0.0)
        self.last_velocity = (0.0, 0.0, 0.0)

    # -------------------------------------------------- 运动学（纯函数，方便单测）
    def wheel_values(self, vx, vy, wz):
        """返回四个轮的线速度目标值 (m/s)，顺序 m1..m4（幻尔官方约定，含取反）。"""
        k = self.k
        m1 = vx - vy - wz * k
        m2 = vx + vy - wz * k
        m3 = vx + vy + wz * k
        m4 = vx - vy + wz * k
        return (-m1, m3, -m2, m4)

    def wheel_speeds(self, vx, vy, wz):
        """按"每个轮子自己的前进速度"给出 (m/s)，顺序 M1..M4 = 左前/右前/左后/右后。

        用于 I2C 板：那边的每路电机符号已经单独标定过（左侧两路同相、右侧两路反装），
        所以这里不能再套官方公式的取反，否则会双重取反。
        wz > 0 = 左转（逆时针），vy > 0 = 右平移。
        """
        k = self.k
        return (vx - vy - wz * k,     # M1 左前
                vx + vy + wz * k,     # M2 右前
                vx + vy - wz * k,     # M3 左后
                vx - vy + wz * k)     # M4 右后

    def _values(self, vx, vy, wz):
        if getattr(self.board, "wheel_frame", False):
            return self.wheel_speeds(vx, vy, wz)
        return self.wheel_values(vx, vy, wz)

    def to_duties(self, vx, vy, wz, max_linear=None, duty_cap=None):
        """四个轮的占空比（%），照官方 speed_convert 归一化。duty_cap 用于加速档。"""
        scale = max_linear if max_linear is not None else self.cfg.max_linear
        if not scale:
            raise ValueError("max_linear must be non-zero")
        cap = self.cfg.max_duty if duty_cap is None else duty_cap
        return tuple(_clamp(v / scale * 100.0, cap)
                     for v in self._values(vx, vy, wz))

    # ---------------------------------------------------------------- 下发
    def set_velocity(self, vx=0.0, vy=0.0, wz=0.0, duty_cap=None):
        vx = 0.0 if vx is None else float(vx)
        vy = 0.0 if vy is None else float(vy)
        wz = 0.0 if wz is None else float(wz)
        self.last_velocity = (vx, vy, wz)
        if self.cfg.motor_mode == "speed":
            speeds = self._values(vx, vy, wz)
            self.board.mark_mode("speed")
            self.board.set_motor_speed(speeds)
            self.last_duties = tuple(round(s, 3) for s in speeds)
        else:
            duties = self.to_duties(vx, vy, wz, duty_cap=duty_cap)
            self.board.mark_mode("duty")
            self.board.set_motor_duty(duties)
            self.last_duties = tuple(round(d, 1) for d in duties)
        return self.last_duties

    def stop(self):
        self.last_velocity = (0.0, 0.0, 0.0)
        self.last_duties = (0.0, 0.0, 0.0, 0.0)
        self.board.stop_motors()

    # ---------------------------------------------- 直接给百分比（调试/自检用）
    def set_duties_percent(self, values):
        values = [float(v) for v in values]
        if len(values) != 4:
            raise ValueError("need 4 values, got %d" % len(values))
        self.board.mark_mode("duty")
        self.board.set_motor_duty(values)
        self.last_duties = tuple(round(v, 1) for v in values)
        return self.last_duties

    def describe(self, vx, vy, wz):
        duties = self.to_duties(vx, vy, wz)
        parts = ["%s:%+.1f%%" % (name, duty) for name, duty in zip(MOTOR_NAMES, duties)]
        return "vx=%+.2f vy=%+.2f wz=%+.2f -> [%s]" % (vx, vy, wz, ", ".join(parts))
