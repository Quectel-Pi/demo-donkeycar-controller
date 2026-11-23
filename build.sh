#!/usr/bin/env bash
# turbopi_app 构建脚本 —— 纯 Python 应用，不需要 ROS / Docker / 交叉工具链：
#   * 不 source 任何 Yocto SDK 环境脚本
#   * 不跑 cmake / make / colcon（没有交叉编译产物）
#   * 产物只落在本目录：output/（可交付目录树）和 dist/（可直接 push 的包）
#
# 用法：
#   ./build.sh                # = all：check -> output -> pack
#   ./build.sh check          # 语法检查（compileall），不产包
#   ./build.sh output         # 生成 output/turbopi_app/ 交付树
#   ./build.sh pack           # 生成 dist/turbopi_app_<VERSION>.{tgz,run}
#   ./build.sh clean
set -euo pipefail
cd "$(dirname "$0")"
HERE=$PWD

VER=$(cat VERSION 2>/dev/null | tr -d '[:space:]')
[ -n "${VER}" ] || VER=$(date +%Y%m%d_%H%M)
OUT_ROOT="$HERE/output"
OUT_DIR="$OUT_ROOT/turbopi_app"
DIST="$HERE/dist"
PY=${PY:-python3}

function check() {
    echo "== 1) 语法检查（不需要硬件）=="
    # 排除 vendor：那是给车机 py3.13 用的 wheel（里面有 3.12+ 的 type 语句），
    # 服务器上的 python3 可能更老，编它是假报错
    "$PY" -m compileall -q -x '/(vendor|output|dist)/' src donkey tools
    echo "   compileall OK"
    if "$PY" -c 'import donkeycar' >/dev/null 2>&1; then
        echo "   跑 donkey 单测..."
        ( cd donkey && "$PY" tests/test_vehicle.py 2>&1 | tail -3 )
    else
        echo "   (跳过 donkey 单测：本机没装 donkeycar)"
    fi
}

function build_output() {
    echo "== 2) 生成交付树 output/turbopi_app =="
    [ -d donkey/vendor/site-packages ] || {
        echo "!! 缺 donkey/vendor/site-packages（车机是 aarch64 + py3.13）"
        echo "   先跑： donkey/fetch_wheels.sh"
        exit 1; }

    rm -rf "$OUT_DIR"
    mkdir -p "$OUT_DIR"
    cp -a src donkey tools bin "$OUT_DIR"/
    cp -a systemd "$OUT_DIR"/
    cp -a systemd/turbopi.service "$OUT_DIR"/turbopi.service
    cp -a install.sh VERSION "$OUT_DIR"/
    find "$OUT_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
    find "$OUT_DIR" \( -name '*.pyc' -o -name '*.py.txt' \) -delete

    # 代码指纹：只算 src/tools/donkey 的 .py（不含 vendor）
    # 必须 LC_ALL=C sort：服务器是 zh_CN.UTF-8、车机是 C，排序不同会算出不同的哈希
    CODE_SHA=$( cd "$OUT_DIR" && find src tools donkey -name '*.py' -not -path '*/vendor/*' \
                | LC_ALL=C sort | xargs -r sha256sum | sha256sum | cut -d' ' -f1 )

    cat > "$OUT_DIR/MANIFEST.txt" <<EOF
app        : turbopi_app ${VER}
built at   : $(date '+%Y-%m-%d %H:%M:%S %z')
host       : $(hostname) ($(uname -m))
target     : RK3576 / QuecPi M2, aarch64, python 3.13
entrypoint : /opt/turbopi/donkey/manage.py drive --wait
service    : /etc/systemd/system/turbopi.service
vendor     : $(du -sh donkey/vendor/site-packages | cut -f1) ($(ls donkey/vendor/site-packages | wc -l) packages)
CODE_SHA   : ${CODE_SHA}
EOF
    du -sh "$OUT_DIR"
}

function pack() {
    build_output
    echo "== 3) 打包 =="
    mkdir -p "$DIST"
    local tgz="$DIST/turbopi_app_${VER}.tgz"
    # --mtime：把包内文件时间固定到过去。车机 RTC 慢 7 个月，若不固定，解包时 tar 会刷
    # 一屏 "time stamp ... is ... s in the future"（无害但吓人）。
    tar -C "$OUT_ROOT" --mtime='2020-01-01 00:00:00' --sort=name \
        --owner=0 --group=0 --numeric-owner -czf "$tgz" turbopi_app

    # 自解压安装包：板上只要 sh 它一次，不用手动解压
    local run="$DIST/turbopi_app_${VER}.run"
    cat > "$run" <<'HDR'
#!/bin/sh
# turbopi_app 自解压安装包：把代码装到 /opt/turbopi 并注册开机自启
set -eu
LINE=$(awk '/^__PAYLOAD_BELOW__$/{print NR+1; exit 0}' "$0")
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
tail -n +"$LINE" "$0" | tar xz -m -C "$TMP"
sh "$TMP/turbopi_app/install.sh"
exit 0
__PAYLOAD_BELOW__
HDR
    cat "$tgz" >> "$run"
    chmod +x "$run"

    ls -lh "$tgz" "$run" | awk '{print "   "$9}'
    awk -v s="$(stat -c %s "$run")" 'BEGIN{printf "   自解压包 %.1f MB\n", s/1048576}'
    sha256sum "$tgz" "$run" | awk '{print "   sha256 "$1"  "$2}'
    cat <<EOF

   推到车机（不用解压、不用 chmod）：
     adb push $(basename "$run") /tmp/
     adb shell sh /tmp/$(basename "$run")

   已有旧版本时，也可以直接丢进 inbox 自动升级：
     adb push $(basename "$run") /opt/turbopi/inbox/
EOF
}

function clean() {
    rm -rf "$OUT_ROOT" "$DIST"
    echo "已清理 output/ 和 dist/"
}

case "${1:-all}" in
    check)   check ;;
    output)  build_output ;;
    pack)    pack ;;
    all)     check; pack ;;
    clean)   clean ;;
    *) echo "usage: $0 [all|check|output|pack|clean]"; exit 2 ;;
esac
