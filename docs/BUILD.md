# 编译说明

本应用是**纯 Python**，没有 C 扩展、没有交叉编译产物。所谓"编译"只做三件事：
语法检查、按白名单拼出可交付目录树、打成自解压安装包。

---

## 1. 编译机要求

| 项 | 要求 | 说明 |
|---|---|---|
| 系统 | 任意 Linux（x86_64 也可以） | 不要求与车机同架构 |
| `python3` | ≥ 3.10 | 只用来跑 `compileall` 做语法检查 |
| 工具 | `tar`、`gzip`、`sha256sum`、`awk`、`stat`、`find` | 发行版自带 |
| 网络 | **仅首次获取 wheel 时需要** | 之后完全离线 |

**不需要**：ROS 2、Docker、Yocto SDK、cmake/colcon、车机、adb。

---

## 2. 第一步：取车机依赖（vendor）

车机是 aarch64 + Python 3.13，且**没有 pip、没有外网**。所以依赖要在编译机上预先下载成 wheel，
放到 `donkey/vendor/site-packages/`，随包一起交付。

```bash
./donkey/fetch_wheels.sh
```

脚本做的事：用 `pip download` 指定 `--platform manylinux_2_28_aarch64 --python-version 3.13
--abi cp313 --only-binary=:all:` 下载 `donkey/requirements.txt` 里的全部依赖，再 `pip install --target`
装进 `donkey/vendor/site-packages/`。

可调环境变量：

| 变量 | 默认 | 含义 |
|---|---|---|
| `PYVER` | `3.13` | 目标 Python 版本，必须与车机一致（ABI 相关） |
| `GLIBC` | `2.28` | wheel 的 glibc 基线，必须 ≤ 车机 glibc（本车 2.41） |

`fetch_wheels.sh` 需要 `pip`；机器上没有 pip 时，脚本会打印等效的 `uv pip install --target ...` 命令。

> **`vendor/` 不入库**（几十 MB 二进制，见 `.gitignore`）。克隆仓库后必须先跑这一步，
> 否则 `./build.sh` 会直接报错 `!! 缺 donkey/vendor/site-packages`。

检查是否就绪：

```bash
ls donkey/vendor/site-packages | head        # 应能看到 donkeycar/ numpy/ pandas/ ...
```

---

## 3. 第二步：编译打包

```bash
./build.sh              # = check -> output -> pack
```

也可以分步：

| 命令 | 做什么 | 产物 |
|---|---|---|
| `./build.sh check` | `compileall` 检查 `src/ donkey/ tools/`（自动排除 vendor） | 无 |
| `./build.sh output` | 生成交付目录树 | `output/turbopi_app/` |
| `./build.sh pack` | 重建交付树并打包 | `dist/*.tgz`、`dist/*.run` |
| `./build.sh clean` | 清理 | 删除 `output/`、`dist/` |

### `check` 阶段

```
"$PY" -m compileall -q -x '/(vendor|output|dist)/' src donkey tools
```

- **必须排除 `vendor/`**：那是给车机 py3.13 用的 wheel，编译机的 python 可能更老，编它会假报错。
- 若编译机上装了 donkeycar（`python3 -c 'import donkeycar'` 通过），会顺带跑 `donkey/tests/test_vehicle.py`。

### `output` 阶段

按**白名单**拷贝，不整目录 `rsync`，避免把测试杂物、缓存、探针脚本带进交付物：

```
src/ tools/ donkey/ bin/ systemd/ install.sh VERSION
```

然后：

1. 删除 `__pycache__/`、`*.pyc`、`*.py.txt`
2. 计算 `CODE_SHA` 写入 `MANIFEST.txt`
3. `systemd/turbopi.service` 同时放到交付树根（`install.sh` 按相对路径取用）

`CODE_SHA` 的算法（**编译期与安装期必须完全一致**）：

```bash
find src tools donkey -name '*.py' -not -path '*/vendor/*' \
  | LC_ALL=C sort | xargs sha256sum | sha256sum
```

> `LC_ALL=C` 不是可选项：编译机若是 `zh_CN.UTF-8`、车机是 `C`，同一份代码会算出两个指纹。
> 该指纹用于「车上跑的是不是这一版」的核对。

### `pack` 阶段

```bash
tar -C output --mtime='2020-01-01 00:00:00' --sort=name \
    --owner=0 --group=0 --numeric-owner -czf dist/turbopi_app_<VER>.tgz turbopi_app
```

- **`--mtime` 固定**：车机 RTC 常年不准（实测慢几个月），不固定的话解包时会刷一屏
  `time stamp ... is ... s in the future`。
- `--sort=name --owner=0 --group=0`：让同样的输入产出同样的包，便于比对。

随后把 `tgz` 追加到一段 shell 头之后，生成**自解压安装包** `dist/turbopi_app_<VER>.run`：

```sh
#!/bin/sh
# ...（定位负载行号，解到临时目录，调用 install.sh）
LINE=$(awk '/^__PAYLOAD_BELOW__$/{print NR+1; exit 0}' "$0")
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
tail -n +"$LINE" "$0" | tar xz -m -C "$TMP"
sh "$TMP/turbopi_app/install.sh"
exit 0
__PAYLOAD_BELOW__
<tar.gz 二进制>
```

因为是 `sh` 在读自己，**不需要 `chmod +x`**，车机上也不需要 `unzip`。

---

## 4. 产物

| 文件 | 用途 |
|---|---|
| `dist/turbopi_app_<VER>.run` | 自解压安装包，`adb push` + `sh` 一次装完（约 36 MB） |
| `dist/turbopi_app_<VER>.tgz` | 等价 tar.gz，想手工解压/检查内容时用 |
| `output/turbopi_app/` | 交付目录树，含 `MANIFEST.txt`（版本、构建时间、`CODE_SHA`） |

`sha256` 每次编译都会变（`MANIFEST.txt` 里有构建时间），核对时用**同一次编译输出里的值**。

---

## 5. 部署到车机

```bash
adb push dist/turbopi_app_1.0.0.run /tmp/
adb shell sh /tmp/turbopi_app_1.0.0.run
```

安装脚本做的事：

1. 解包到 `/opt/turbopi`（先解 `.new` 再换名，避免半途失败留下残缺目录）
2. 写 `/etc/systemd/system/turbopi.service`
3. `systemctl daemon-reload` + `enable` + `restart`
4. 装 `turbopi-inbox.path` / `turbopi-inbox.service`（支持 push 即升级）
5. 软链 `/usr/local/bin/car` → `/opt/turbopi/bin/car`
6. 写 `/opt/turbopi/BUILD_INFO`（安装时间 + 板上实际 `CODE_SHA`）

之后升级不必再登录：把新 `.run` 丢进 `/opt/turbopi/inbox/`，1~2 秒自动安装并重启服务。

---

## 6. 验证

```bash
# 语法检查通过
./build.sh check

# 两边指纹一致 = 车上跑的就是这一版
adb shell "grep CODE_SHA /opt/turbopi/BUILD_INFO"
grep CODE_SHA output/turbopi_app/MANIFEST.txt
```

---

## 7. 常见错误

| 报错 | 原因 | 处理 |
|---|---|---|
| `!! 缺 donkey/vendor/site-packages` | 没跑 `fetch_wheels.sh` | 先补 vendor（注意 `PYVER` 必须等于车机版本） |
| `compileall` 在 vendor 里报语法错 | 编译机 python 版本低于 3.12 | 确认用的是本仓库的 `build.sh`（已排除 vendor） |
| 车机上 `ModuleNotFoundError: donkeycar` | wheel 的 ABI 不匹配（如按 cp312 下载、车机是 3.13） | 用 `PYVER=3.13` 重跑 `fetch_wheels.sh` |
| 车机上 `GLIBC_2.xx not found` | wheel 的 glibc 基线高于车机 | 用更低的 `GLIBC=` 重跑 |
| `dist/` 里的包没更新 | 手工改过 `output/` | `pack` 会强制重建交付树，直接 `./build.sh` 即可 |
| 装完服务起不来 | 见 README 的已知限制与排障 | `journalctl -u turbopi -b` 看原因 |
