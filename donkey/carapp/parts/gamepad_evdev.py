"""donkeycar 输入 Part：evdev 手柄 -> (steering, throttle)。

为什么不用 donkeycar 自带的 JoystickController：
  它依赖 pygame + joydev（/dev/input/js0）。M2 板的内核没编 joydev，js0 永远不存在；
  pygame 又要 pip 装。这里用纯标准库读 evdev（/dev/input/event*），零依赖。

按键：A = 急停（按住即 0/0）、B = 鸣笛（如果接了 board）、START = 退出程序。

注意 donkeycar 的 threaded 约定（vehicle.py:102 与 :206）：
  Vehicle.add(part, threaded=True) 只会 `Thread(target=part.update)` 并 start 一次，
  主循环每拍调 `run_threaded()` 取最新值。所以 update() 必须是常驻循环。
"""

import logging
import time

log = logging.getLogger(__name__)


class GamepadInput:
    """donkeycar Part。用法：

        pad = GamepadInput(cfg)
        V.add(pad, outputs=['user/angle', 'user/throttle'], threaded=True)
    """

    def __init__(self, cfg, pad=None, gamepad_module=None, shared=None):
        self.cfg = cfg
        self.deadzone = float(getattr(cfg, "GAMEPAD_DEADZONE", 0.08))
        # 右摇杆：横移 + 原地旋转。donkey 的 part 只有 (steering, throttle) 两路，
        # 麦轮多出来的 vy/wz 走 shared 这条旁路，由执行器读走。
        self.shared = shared if shared is not None else {}
        self.strafe_axis = getattr(cfg, "GAMEPAD_STRAFE_AXIS", "rx")
        self.rotate_axis = getattr(cfg, "GAMEPAD_ROTATE_AXIS", "ry")
        self.strafe_sign = float(getattr(cfg, "GAMEPAD_STRAFE_SIGN", 1.0))
        self.rotate_sign = float(getattr(cfg, "GAMEPAD_ROTATE_SIGN", 1.0))
        # 动作键（按住生效，优先于右摇杆）：X=原地左转，Y=原地右转，RB=横移模式
        self.turn_left_button = str(getattr(cfg, "GAMEPAD_TURN_LEFT_BUTTON", "x")).lower()
        self.turn_right_button = str(getattr(cfg, "GAMEPAD_TURN_RIGHT_BUTTON", "y")).lower()
        self.strafe_button = str(getattr(cfg, "GAMEPAD_STRAFE_BUTTON", "r1")).lower()
        self._act_state = {}
        self.throttle_axis = getattr(cfg, "GAMEPAD_THROTTLE_AXIS", "ly")
        self.steering_axis = getattr(cfg, "GAMEPAD_STEERING_AXIS", "rx")
        self.invert_throttle = bool(getattr(cfg, "GAMEPAD_INVERT_THROTTLE", True))
        self.invert_steering = bool(getattr(cfg, "GAMEPAD_INVERT_STEERING", False))
        raw_estop = getattr(cfg, "GAMEPAD_ESTOP_BUTTONS", None)
        if raw_estop is None:
            raw_estop = getattr(cfg, "GAMEPAD_ESTOP_BUTTON", "l1,x,a")
        if isinstance(raw_estop, str):
            self.estop_buttons = tuple(b.strip().lower() for b in raw_estop.split(",") if b.strip())
        else:
            self.estop_buttons = tuple(str(b).lower() for b in raw_estop)
        self.quit_button = getattr(cfg, "GAMEPAD_QUIT_BUTTON", "start")
        raw_boost = getattr(cfg, "GAMEPAD_BOOST_BUTTONS", "a")
        if isinstance(raw_boost, str):
            self.boost_buttons = tuple(b.strip().lower() for b in raw_boost.split(",") if b.strip())
        else:
            self.boost_buttons = tuple(str(b).lower() for b in raw_boost)
        self.boost_mult = float(getattr(cfg, "GAMEPAD_BOOST_MULT", 1.4))
        self.boost_trigger = getattr(cfg, "GAMEPAD_BOOST_TRIGGER", "rt")

        self.steering = 0.0
        self.throttle = 0.0
        self.estop = False
        self.boost = False
        self._boost_held = set()
        self._estop_held = set()
        self.quit_now = False
        self.pad_info = None
        self._running = True
        self._pad = pad
        self._module = gamepad_module

        if self._pad is None:
            self._open()

    # ---------------------------------------------------------------- 打开
    def _open(self):
        backend = str(getattr(self.cfg, "GAMEPAD_BACKEND", "auto")).lower()
        errors = []
        if backend in ("auto", "evdev"):
            if self._open_evdev():
                return
            errors.append("evdev: no /dev/input device with joystick axes")
        if backend in ("auto", "xinput"):
            if self._open_xinput():
                return
            errors.append("xinput: no XTYPE (class ff/5d) USB interface present")
        raise RuntimeError(
            "no gamepad found (%s). 这块车机的内核 CONFIG_INPUT_JOYSTICK=n，"
            "没有 xpad/joydev，所以 XInput 手柄是通过 usbfs 直读的："
            "请确认手柄在 Xinput 模式（白灯）且插在 USB-A(host) 口。"
            % "; ".join(errors))

    def _open_evdev(self):
        if self._module is None:
            try:
                from turbopi import gamepad as module   # 延迟导入：非 Linux 上也不需要
            except ImportError as exc:
                log.info("evdev backend unavailable: %s", exc)
                return None
            self._module = module
        path = getattr(self.cfg, "GAMEPAD_PATH", "") or None
        info = self._module.describe_device(path) if path else self._module.pick()
        if not info:
            return None
        self.pad_info = info
        self._pad = self._module.Gamepad(info["path"])
        log.info("gamepad %s %r", info["path"], info["name"])
        return info

    def _open_xinput(self):
        try:
            from turbopi import xinput as module
        except ImportError as exc:
            log.info("xinput backend unavailable: %s", exc)
            return None
        vid = int(getattr(self.cfg, "GAMEPAD_VID", module.DEFAULT_VID))
        pid = int(getattr(self.cfg, "GAMEPAD_PID", module.DEFAULT_PID))
        info = module.describe(vid, pid)
        if not info:
            return None
        self._pad = module.XInputPad(vid, pid)
        self.pad_info = info
        log.info("gamepad %s %r (usbfs backend)", info["path"], info["name"])
        return info

    # ------------------------------------------------------------ 读取逻辑
    def _apply_deadzone(self, value):
        return 0.0 if abs(value) < self.deadzone else value

    def poll(self):
        """把设备事件变成 (steering, throttle)，返回是否有变化。"""
        if self._pad is None:
            return False
        changed = False
        for kind, name, value in self._pad.poll(timeout=0.0):
            if kind == "axis":
                changed = True
            elif kind == "button":
                if name in self.estop_buttons:
                    # 多个急停键：任意一个按住即停车，全部松开才解除
                    if value:
                        self._estop_held.add(name)
                    else:
                        self._estop_held.discard(name)
                    self.estop = bool(self._estop_held)
                    changed = True
                elif name in self.boost_buttons:
                    if value:
                        self._boost_held.add(name)
                    else:
                        self._boost_held.discard(name)
                    self.boost = bool(self._boost_held)
                    changed = True
                elif name in (self.turn_left_button, self.turn_right_button, self.strafe_button):
                    if value:
                        self._act_state[name] = True
                    else:
                        self._act_state.pop(name, None)
                    changed = True
                elif name == self.quit_button and value:
                    self.quit_now = True
        axes = getattr(self._pad, "axes", {})
        throttle = self._apply_deadzone(float(axes.get(self.throttle_axis, 0.0)))
        steering = self._apply_deadzone(float(axes.get(self.steering_axis, 0.0)))
        if self.invert_throttle:
            throttle = -throttle
        if self.invert_steering:
            steering = -steering
        if self.estop:
            throttle, steering = 0.0, 0.0
        else:
            # 加速：优先用扳机 RT（0~1 模拟量），其次用加速键（按满=1.0）。
            # 编码方式：|throttle| 超过 1 的部分表示"抬多少占空比上限"，
            # 1.0 = 不加速，1.5 = 加速到满（执行器负责换算成 cap）。
            frac = 0.0
            triggers = getattr(self._pad, "triggers", None) or {}
            boost_trigger = getattr(self, "boost_trigger", "rt")
            if boost_trigger:
                try:
                    rt = float(triggers.get(boost_trigger, 0.0))
                except (TypeError, ValueError):
                    rt = 0.0
                if rt > 0.05:
                    frac = min(1.0, rt)
            if frac <= 0 and self.boost:
                frac = 1.0
            if frac > 0 and throttle:
                throttle = (1.0 if throttle > 0 else -1.0) * abs(throttle) * (1.0 + 0.5 * frac)
                throttle = max(-1.5, min(1.5, throttle))
        throttle = 0.0 if throttle == 0 else throttle      # 修掉 -0.0 的显示
        steering = 0.0 if steering == 0 else steering
        # 麦轮横移(vy) / 原地旋转(wz)：动作键优先，其次右摇杆；急停时全部归零
        if self.estop or self.quit_now:
            self.shared["vy"] = 0.0
            self.shared["wz"] = 0.0
        else:
            vy_cmd = 0.0
            wz_cmd = 0.0
            if self._act_state.get(self.turn_left_button):        # X：原地左转
                wz_cmd = 1.0
            elif self._act_state.get(self.turn_right_button):     # Y：原地右转
                wz_cmd = -1.0
            elif self._act_state.get(self.strafe_button):         # RB：转向变平移
                vy_cmd = steering
                steering = 0.0
            else:                                                 # 右摇杆
                if self.strafe_axis:
                    vy_cmd = self.strafe_sign * self._apply_deadzone(
                        float(axes.get(self.strafe_axis, 0.0)))
                if self.rotate_axis:
                    wz_cmd = self.rotate_sign * self._apply_deadzone(
                        float(axes.get(self.rotate_axis, 0.0)))
            self.shared["vy"] = vy_cmd
            self.shared["wz"] = wz_cmd
        if (throttle, steering) != (self.throttle, self.steering):
            changed = True
        self.throttle, self.steering = throttle, steering
        return changed

    # ------------------------------------------------- donkey Part 接口
    def run(self):
        self.poll()
        return self.steering, self.throttle

    def update(self):
        """threaded 模式的后台循环（donkeycar 只 start 一次，所以必须 while）。"""
        while self._running:
            try:
                self.poll()
            except Exception as exc:                 # 设备被拔掉之类：别让线程炸掉
                log.warning("gamepad poll failed: %s", exc)
            time.sleep(0.02)

    def run_threaded(self):
        return self.steering, self.throttle

    def shutdown(self):
        self._running = False
        if self._pad is not None:
            self._pad.close()
            self._pad = None
