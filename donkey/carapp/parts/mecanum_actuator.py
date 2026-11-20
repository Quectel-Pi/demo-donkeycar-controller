"""donkeycar 执行器 Part：把 (steering, throttle) 变成幻尔扩展板上的四轮占空比。

这是"donkeycar 应用层 ↔ 幻尔驱动层"的唯一接口：

    donkeycar Vehicle 主循环
        └─ run_threaded(steering, throttle)   # 只记录最新指令，立刻返回
    ActuatorThread（本文件里的后台线程，固定频率）
        └─ chassis.set_velocity(vx, vy, wz)   # 幻尔麦轮运动学
            └─ board.set_motor_duty(...)      # AA 55 03 16 05 04 ... 帧

为什么要独立线程 + 看门狗：
  donkeycar 的 part 是"主循环调用一次跑一次"。若主循环被模型推理/录像卡住，
  电机就会一直保持最后一次占空比。线程 + 0.5s 无更新自动归零，能兜住这种情况。
"""

import logging
import threading
import time

log = logging.getLogger(__name__)


class MecanumActuator:
    """donkeycar Part。用法：

        act = MecanumActuator(chassis, cfg)
        V.add(act, inputs=['user/angle', 'user/throttle'], threaded=True)
    """

    def __init__(self, chassis, cfg, name="mecanum", shared=None):
        self.chassis = chassis
        self.cfg = cfg
        self.name = name
        self.shared = shared if shared is not None else {}
        self.max_linear = float(getattr(cfg, "MAX_LINEAR", 0.5))
        self.max_angular = float(getattr(cfg, "MAX_ANGULAR", 2.0))
        self.steering_sign = float(getattr(cfg, "STEERING_SIGN", -1.0))
        self.scale_angular = bool(getattr(cfg, "SCALE_ANGULAR_BY_THROTTLE", False))
        self.timeout = float(getattr(cfg, "ACTUATOR_TIMEOUT", 0.5))
        self.rate_hz = float(getattr(cfg, "DRIVE_LOOP_HZ", 20))
        # 加速档：|throttle|>1 时用 BOOST_DUTY，否则用 MAX_DUTY
        self.base_duty = float(getattr(cfg, "MAX_DUTY", 60))
        self.boost_duty = max(float(getattr(cfg, "GAMEPAD_BOOST_DUTY", 85)), self.base_duty)

        self._lock = threading.Lock()
        self._target = (0.0, 0.0, 0.0)     # vx, vy, wz
        self._stamp = 0.0
        self._running = True
        self.last_duties = (0.0, 0.0, 0.0, 0.0)
        self.updates = 0

    # ---------------------------------------------------------- 运动学映射
    def to_velocity(self, steering, throttle):
        """donkey 的 (angle, throttle) -> (vx, vy, wz, duty_cap)。

        手柄按住加速键时会把 throttle 放大到 1 以上（例如 1.4），这里用它表示
        "加速档"：基础速度仍按 1.0 算，占空比上限抬到 GAMEPAD_BOOST_DUTY。
        """
        steering = 0.0 if steering is None else float(steering)
        throttle = 0.0 if throttle is None else float(throttle)
        steering = max(-1.0, min(1.0, steering))
        mag = abs(throttle)
        # |throttle| > 1 表示带加速：1.0 → 不加速，1.5 → 加速到满
        frac = 0.0
        if mag > 1.0001:
            frac = max(0.0, min(1.0, (mag - 1.0) / 0.5))
        throttle = max(-1.0, min(1.0, throttle))
        cap = self.base_duty + frac * (self.boost_duty - self.base_duty)
        vx = throttle * self.max_linear
        wz = self.steering_sign * steering * self.max_angular
        # 右摇杆带来的横移 / 原地旋转（麦轮特有）：-1..1 直接换算成物理量
        vy = float(self.shared.get("vy", 0.0) or 0.0) * self.max_linear
        wz += float(self.shared.get("wz", 0.0) or 0.0) * self.max_angular
        if self.scale_angular:
            wz *= abs(throttle)
        return vx, vy, wz, cap

    # ------------------------------------------------- donkey Part 接口
    def run(self, steering, throttle):
        """非 threaded 模式：直接下发（同步阻塞一个串口写）。"""
        velocity = self.to_velocity(steering, throttle)
        self._commit(velocity)
        return self.last_duties

    def run_threaded(self, steering, throttle):
        """threaded 模式（主循环调用）：只更新目标值，不等串口。"""
        with self._lock:
            self._target = self.to_velocity(steering, throttle)
            self._stamp = time.time()
        return None

    def update(self):
        """threaded 模式（donkeycar 自己开的后台线程调用）：阻塞循环，直到 shutdown。

        Vehicle.add(part, threaded=True) 会把 update 挂到一个 Thread 上只 start 一次，
        所以这里必须是 while 循环，而不是"跑一次"。
        """
        period = 1.0 / max(1.0, self.rate_hz)
        while self._running:
            time.sleep(period)
            with self._lock:
                target = self._target
                stale = (time.time() - self._stamp) > self.timeout
            if stale:
                target = (0.0, 0.0, 0.0, self.base_duty)
            self._commit(target)
            self.updates += 1

    def shutdown(self):
        """Vehicle.stop() 会调用它：停线程 + 停车。"""
        self._running = False
        time.sleep(2.0 / max(1.0, self.rate_hz))       # 让 update 循环有机会退出
        try:
            self.chassis.stop()
        except Exception as exc:                       # 停车尽力而为
            log.warning("chassis.stop() failed: %s", exc)

    # -------------------------------------------------------------- 内部
    def _commit(self, velocity):
        try:
            self.last_duties = self.chassis.set_velocity(*velocity)
        except Exception as exc:
            log.error("motor write failed: %s", exc)
