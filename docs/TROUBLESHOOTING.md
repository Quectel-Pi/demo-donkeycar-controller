# 排障手册

下面每一条都是实车踩过的坑，按"现象 → 判断命令 → 处理"组织。

---

## 1. 车轮不动

先分层，不要一上来就怀疑手柄。

```bash
systemctl stop turbopi
car bench --duty 30 --ms 1500
```

| 结果 | 说明 |
|---|---|
| 四轮转 | 电机链路正常，问题在手柄/映射 → 看第 2 节 |
| 打印 `board i2c-3@0x7A` 但轮子不转 | 占空比太低（<35% 起步不动）或电池没开 |
| 打印"找不到板子" / 电池读数为 0 | 扩展板供电或 40pin 接触问题 → 看第 3 节 |

---

## 2. 手柄没反应

```bash
car buttons --raw
```

| 现象 | 原因 | 处理 |
|---|---|---|
| 完全无输出 | 手柄不在 XInput 模式 / 插在非 Host 口 | 手柄切 XInput（白灯），插 USB-A Host 口 |
| 有输出但按键名不对 | 面键布局不匹配 | 见 `docs/HARDWARE.md` 第 4 节，按 VID:PID 自动匹配或 `TURBOPI_PAD_LAYOUT=` 强制 |
| `car monitor` 没变化，但 `buttons` 有 | 该键没绑定功能 | `monitor` 只显示"算出来的"转向/油门，未绑定的键不会影响输出 |
| 按住加速键没反应 | 摇杆在中间（throttle=0），加速是"放大油门" | 推住摇杆再按加速/扳机 |

> `monitor` 是只读工具：它不会驱动电机，也不会打印未绑定按键。

---

## 3. 扩展板通信

```bash
python3 tools/i2c_ctrl.py battery      # 或 car bench --duty 0
```

正常输出 `battery: 7.6x V`。

| 现象 | 原因 | 处理 |
|---|---|---|
| `i2cdetect -y -r 3` 全 `--` | STC8H 不响应 SMBus quick probe | **忽略它**，用电池电压判断 |
| 读不到电池 | 40pin 排母没压实 / 电池开关没开 / 扩展板没上电 | 重插扩展板，开电池开关 |
| 电池读数偶尔是 `65.52 V` 这种乱值 | 写电机后立刻读电池的瞬态 | 驱动已做 3 次重试 + 范围校验；偶发可忽略 |

---

## 4. 服务相关

```bash
systemctl status turbopi
journalctl -u turbopi -b --no-pager | tail -30
```

| 现象 | 原因 | 处理 |
|---|---|---|
| `Unit docker.service not found` | 装进了旧的 docker 版 unit | 重装本包（内置 python3 版 unit） |
| 服务 `active` 但一直不接管 | 没插手柄 / 扩展板没上电 | 正常行为：`drive --wait` 每 3 秒重试，设备就位自动接管 |
| 服务反复重启 | 启动即异常退出，看日志首行 | 常见是 `ModuleNotFoundError`（wheel ABI 不对）或 I2C 权限 |
| 日志时间是几个月前 | 车机 RTC 慢，未同步 | 不影响功能，按内容判断顺序 |

---

## 5. 部署相关

| 现象 | 原因 | 处理 |
|---|---|---|
| `adb devices` 为空 | USB 掉线 / 车机重启 / 推车时扯线 | `adb kill-server && adb start-server`，等 25~55 秒 |
| `sh xxx.run` 报 `tar: ... in the future` | 旧包没固定 mtime | 只是告警；新包已解决 |
| 安装后 `CODE_SHA` 与编译机不一致 | 一边用了 `zh_CN` 排序、一边用 `C` | 统一走 `build.sh` / `install.sh` 里的算法（`LC_ALL=C`） |
| `inbox` 里的包没被处理 | `.path` 服务没起 | `systemctl status turbopi-inbox.path`；日志 `journalctl -u turbopi-inbox` |
| 包变成 `.failed` | 安装脚本报错 | 同上，看 inbox 日志里的具体行 |

---

## 6. 已知限制

- 离线 wheel 不含 `tflite_runtime` / `tensorflow`，本仓库只覆盖手动驾驶与数据采集。
- 仅适配 4B A 款（I2C 0x7A）；其他扩展板需改 `i2cboard.py` 或改用串口后端。
- 蜂鸣器 / LED / PWM 舵机未接成 donkeycar Part，只在 `tools/i2c_ctrl.py` 里有裸操作示例。
