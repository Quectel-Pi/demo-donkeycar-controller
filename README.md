# turbopi_app

幻尔 TurboPi 四驱麦轮车的 **donkeycar** 控制程序：用一支 USB 手柄开车，支持麦轮横移、原地旋转、斜行等特技动作。

跑在嵌入式 Linux 车机（aarch64）上，**纯 Python 标准库优先**：不需要 ROS、不需要 Docker、不需要 pip、不需要外网。

```
        USB 手柄（usbfs 直读，≈800 Hz）
                 │
        GamepadInput（donkeycar Part）
                 │  user/angle, user/throttle
        MecanumActuator（20 Hz 独立线程）
                 │  vx, vy, wz
        MecanumChassis（麦轮运动学）
                 │  四轮占空比 -100..100
        I2C ── 扩展板 STC8H1K08 @ 0x7A ── 4× YX-4055AM ── 电机
```

## 特性

- **手柄即开即用**：插上接管、拔掉停车，开机自动进入等待态
- **麦轮特技**：整车横移（车头朝向不变）、原地左/右转、斜行
- **加速档 + 急停**：占空比上限可切换，急停优先级最高
- **零内核依赖**：手柄走 usbfs 直读 XInput，不需要 `joydev` / `xpad` / `/dev/input/js*`
- **一个自解压包部署**：`adb push` + `sh` 一条命令，无需 chmod / 解压
- **可验证版本**：编译期与安装期生成同名 `CODE_SHA`，一眼比对「车上跑的是不是这一版」

## 硬件要求

| 部件 | 型号 / 说明 |
|---|---|
| 主控 | aarch64 Linux，Python ≥ 3.12（实测 Debian / glibc 2.41 / Python 3.13.5） |
| 扩展板 | 树莓派 4B 扩展板 A 款（DC 接口），板载 `STC8H1K08`，I2C 从机 **0x7A**，4× `YX-4055AM` |
| 底盘 | 幻尔 TurboPi 麦轮四驱（4 路直流减速电机） |
| 电池 | 2S 锂电池（7.4 V 标称 / 8.4 V 满电），电压可由扩展板回读 |
| 手柄 | XInput 手柄，实测 **飞智 Flydigi APEX5**（USB `37d7:2501`） |

> 扩展板是 **4B A 款（I2C 0x7A）**，不是 Pi5 版的 `AA 55` 串口协议。
> 项目里保留的串口后端（`src/turbopi/protocol.py` 等）是给另一代扩展板用的，本车不用。

## 快速开始

```bash
# 1) 取车机依赖（在有网、有 pip/uv 的 x86 机器上；产物是 aarch64 + py3.13 的 wheel）
./donkey/fetch_wheels.sh                      # -> donkey/vendor/site-packages/

# 2) 编译打包（不需要车机、不需要 ROS/Docker）
./build.sh                                    # -> dist/turbopi_app_1.0.0.run

# 3) 推到车机并安装（一条命令，不需要 chmod / 解压）
adb push dist/turbopi_app_1.0.0.run /tmp/
adb shell sh /tmp/turbopi_app_1.0.0.run
```

装完即开机自启，插上手柄就能开。详细编译说明见 [docs/BUILD.md](docs/BUILD.md)。

## 手柄操作

| 按键 / 摇杆 | 行为 |
|---|---|
| 左摇杆 上 / 下 | 前进 / 后退 |
| 左摇杆 左 / 右 | 左转 / 右转（可斜推，边走边拐） |
| **X**（按住） | 原地左转（左轮后退、右轮前进） |
| **Y**（按住） | 原地右转 |
| **RB**（按住）+ 左摇杆左右 | 整车左右平移（车头朝向不变） |
| 右摇杆 左右 / 上下 | 平移 / 原地旋转（模拟量） |
| **A** / **RT** | 加速：占空比上限 30% → 100% |
| **B** / **LB** | 急停 |
| START | 退出本次驾驶，回到等待态 |

优先级：**急停 > 动作键 X/Y/RB > 右摇杆**。

> APEX5 的面键位与标准 XInput 不同（A=`0x10`、B=`0x20`、X=`0x40`、Y=`0x80`）。
> 程序按 `VID:PID` 自动套用 `flydigi` 布局，标准手柄自动回落到 `standard`，也可用
> `TURBOPI_PAD_LAYOUT=standard|flydigi` 强制指定。详见 [docs/HARDWARE.md](docs/HARDWARE.md)。

## 目录结构

```
src/turbopi/          驱动层（可单独使用，不依赖 donkeycar）
  i2cboard.py         I2C 0x7A 后端：四路电机 + 电池电压
  chassis.py          麦轮运动学：(vx, vy, wz) → 四轮占空比
  xinput.py           usbfs 直读 XInput 手柄（含 APEX5 布局）
  board.py            另一代扩展板的 AA55 串口后端（可选）
  gamepad.py          evdev 手柄后端（可选）
donkey/               donkeycar 应用层
  myconfig.py         全部可调参数
  manage.py           命令行：drive / monitor / buttons / bench / jog
  carapp/             donkeycar Part 与组装
systemd/              开机自启与 inbox 自动升级
bin/car               板上统一入口
tools/                标定与诊断脚本
tests/                冒烟测试
```

## 板端命令

```bash
car monitor                 # 只读手柄，不动电机
car buttons --raw           # 打印按键事件 + 原始报文（排查手柄用）
car bench --duty 30 --ms 1500   # 四轮占空比直测
car jog --vx 0.2 --ms 1500      # 按运动学直测
car drive [--tub]           # 跑 donkeycar 车辆循环
```

## 参数

集中在 `donkey/myconfig.py`，全部可用环境变量覆盖，改完 `systemctl restart turbopi` 生效。

| 变量 | 默认 | 含义 |
|---|---|---|
| `MAX_DUTY` | 30 | 普通档占空比上限（%） |
| `GAMEPAD_BOOST_DUTY` | 100 | 加速档上限（%） |
| `GAMEPAD_THROTTLE_AXIS` / `GAMEPAD_STEERING_AXIS` | `ly` / `lx` | 油门轴 / 转向轴 |
| `GAMEPAD_STRAFE_AXIS` / `GAMEPAD_ROTATE_AXIS` | `rx` / `ry` | 平移轴 / 原地旋转轴 |
| `GAMEPAD_ESTOP_BUTTONS` | `b,l1` | 急停键 |
| `I2C_SIGNS` | `1,-1,1,-1` | 四轮符号标定 |

完整列表见 `donkey/myconfig.py`。

## 测试

```bash
./build.sh check                  # 全量语法检查（不含 vendor）
cd donkey && python3 tests/test_vehicle.py   # 需要装 donkeycar
python3 tests/smoke.py            # 驱动层冒烟
```

## 已知限制

- 离线 wheel 不含推理引擎（`tflite_runtime` / `tensorflow`），本仓库只做数据采集与手动驾驶；要做自动驾驶需自行补装对应 aarch64 版本。
- 仅适配 4B A 款扩展板（I2C 0x7A）。其他扩展板请改 `src/turbopi/i2cboard.py` 或换用串口后端。
- 蜂鸣器、LED、PWM 舵机等外设未接入 donkeycar Part，仅在 `tools/` 里有裸操作示例。

## License

MIT，见 [LICENSE](LICENSE)。
