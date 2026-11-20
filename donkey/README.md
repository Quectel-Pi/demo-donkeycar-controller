# donkeycar 层（跑在幻尔驱动层之上）

```
                    ┌──────────────────────────────────────────┐
   手柄(evdev) ─────┤ GamepadInput  → user/angle, user/throttle │
                    └───────────────────┬──────────────────────┘
                                        │  donkeycar Vehicle 主循环 (20 Hz)
                    ┌───────────────────▼──────────────────────┐
                    │ MecanumActuator（后台线程 + 0.5s 看门狗） │
                    └───────────────────┬──────────────────────┘
                                        │ set_velocity(vx, vy, wz)
                    ┌───────────────────▼──────────────────────┐
                    │ turbopi 驱动层（幻尔协议，字节级已验证）  │
                    │   chassis.py 麦轮运动学                   │
                    │   board.py   AA 55 .. CRC8 帧             │
                    │   serialport.py termios 1000000 8N1       │
                    └───────────────────┬──────────────────────┘
                                        │ /dev/ttyS1 → 40pin → 74HC126 → STM32
   摄像头(可选) ──→ cam/image_array ──→ TubWriter（采集训练数据）
```

## 为什么这样分层

| 幻尔的东西 | 在 donkeycar 里的位置 | 处理方式 |
|---|---|---|
| `ros_robot_controller_sdk.py`（Board） | `turbopi/board.py` | 已重写为纯标准库，帧字节与官方逐字节一致（`tools/verify.py` 11 项 PASS） |
| `controller/mecanum.py`（运动学） | `turbopi/chassis.py` | 公式照抄，45 组速度校验一致 |
| `peripherals/joystick_control.py` | `carapp/parts/gamepad_evdev.py` | 改走 evdev（原版要 `/dev/input/js0`，本板无 joydev） |
| ROS2 节点（avoidance/line_following/tracking/llm） | **没有对应物** | 这些是 rclpy 节点，不能直接搬。要哪个就单独写成 donkeycar Part（一般 50~100 行/个） |
| `ros2 launch bringup` | `manage.py drive` | 一个进程一个线程池，没有 DDS、没有 node、没有 colcon |

一句话：**能搬的是"硬件层"，不能搬的是"业务层"**。硬件层已经搬完并验证；业务层要按 Part 重写。

## 运行

```bash
# 不碰硬件，只看装配结果和运动学
python3 manage.py check

# 手柄驾驶
python3 manage.py drive

# 手柄驾驶 + 录制数据（到 DATA_PATH/tub_YYYYmmdd_HHMMSS）
python3 manage.py drive --tub

# 训练（需要 tensorflow，一般在 PC 上跑，车机不装）
python3 manage.py train
```

容器里（记得挂串口、evdev 目录、数据目录）：

```bash
docker run --rm -it \
  --device /dev/ttyS1 \
  -v /dev/input:/dev/input:ro \
  -v /root/mycar/data:/root/mycar/data \
  -e DONKEY_DATA_PATH=/root/mycar/data \
  turbopi-donkey:1.0.0 drive --tub
```

## 车机离线安装 donkeycar（没有 pip、没有外网）

donkeycar 5.4 要求 `python>=3.12,<3.14`，车机是 **3.13.5**，刚好在范围内。
但它需要 numpy / pandas / pillow / tornado / paho-mqtt 等 17 个运行时依赖，所以：

```bash
# 1) 在能上网的 amd64 服务器上抓 aarch64 + cp313 的 whl，装成 vendor/site-packages
cd ai_car_project/turbopi_app/donkey && ./fetch_wheels.sh

# 2) 打镜像（COPY vendor/，仍然没有 RUN，不用 qemu）
cd .. && ./build.sh donkey        # -> dist/turbopi-donkey_1.0.0.tar.gz

# 3) 推车机
adb push dist/turbopi-donkey_1.0.0.tar.gz /mnt/car_test/
adb shell "docker load -i /mnt/car_test/turbopi-donkey_1.0.0.tar.gz"
```

## 配置（`myconfig.py`，全部可用环境变量覆盖）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TURBOPI_DEVICE` | 自动 | `/dev/rrc` → `/dev/ttyS1` → … |
| `TURBOPI_MAX_LINEAR` | 0.5 | 满油门线速度 m/s |
| `TURBOPI_MAX_ANGULAR` | 2.0 | 满舵角速度 rad/s |
| `DONKEY_LOOP_HZ` | 20 | 主循环频率 |
| `DONKEY_DATA_PATH` | `~/mycar/data` | tub 存放目录 |
| `DONKEY_USE_CAMERA` | 0 | 1 = 加摄像头 Part（需要 opencv） |
| `DONKEY_ACTUATOR_TIMEOUT` | 0.5 | 断指令多久自动停车 |
| `DONKEY_GAMEPAD_INVERT_THROTTLE` | 1 | evdev 里摇杆向上是负值，所以要反 |
| `DONKEY_GAMEPAD_INVERT_STEERING` | 0 | evdev 向右是正值，与 donkey 的 `angle>0=右转` 一致 |

## 已知边界

- **本车机的内核没有 joydev / xpad**（`# CONFIG_INPUT_JOYSTICK is not set`），
  `/dev/input/js*` 和带摇杆轴的 `/dev/input/event*` 永远不会出现。所以手柄走
  **usbfs 直读 XTYPE 中断端点**（`turbopi.xinput`），不需要内核驱动，也不需要 hidraw。
  要求：手柄在 **Xinput 模式**、插 **USB-A(HOST) 口**、进程有 root（`/dev/bus/usb` 权限）。
- 飞智 APEX5 实测：X/Y/LB/RB/BACK/START 都在标准 XInput 报文里，
  **A/B 两个键不上报**（走厂商私有通道），所以急停默认绑 `l1`（LB），另加 `x` 兜底。
- XInput 的 `LY` 上推为正，与本项目内部约定（同 evdev，上推为负）相反，
  `XInputPad(invert_y=True)` 默认取反一次；实测修正后"上推=前进"。
- 打开设备后的**第一帧只作基线**，不发事件（端点 FIFO 会残留上次会话的报文，
  否则会误判成"刚按下 START"而立刻退出）。

- **训练**不在车机上做：tensorflow 装不进 3.13 + 无外网的环境，数据拷到 PC 训练。
- **摄像头**默认关：车机是否有可用 V4L2 设备还没实测；打开前先 `ls /dev/video*`。
- **业务节点**（避障/巡线/追踪）尚未移植，需要单独写 Part。
- `TubWriter` 在"一条记录都没写就退出"时会打一条 `Unexpected closing manifest` 日志，无害。
