#!/usr/bin/env bash
# 在能上网的 amd64 开发服务器上，为车机（aarch64 + Python 3.13）离线抓 donkeycar 及其依赖，
# 产出 vendor/site-packages/，再随镜像 COPY 进去 —— 车机不需要 pip、不需要外网。
#
# 用法（服务器上）：
#   ./fetch_wheels.sh                       # 默认 cp313 / manylinux2014_aarch64
#   PYVER=3.12 GLIBC=2.28 ./fetch_wheels.sh # 换版本/换 glibc 基线
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PYVER=${PYVER:-3.13}
GLIBC=${GLIBC:-2.28}
DEST="$HERE/vendor/site-packages"
PLAT="manylinux_${GLIBC}_aarch64"

echo "== target: python $PYVER / $PLAT -> $DEST"
python3 -m pip --version >/dev/null 2>&1 || {
    echo "pip 不在 PATH，用 uv 更省事："
    echo "  uv pip install --target $DEST --python-platform aarch64-unknown-linux-gnu \\"
    echo "      --python-version $PYVER --only-binary=:all: -r $HERE/requirements.txt"
    exit 1
}

rm -rf "$DEST" && mkdir -p "$DEST"
python3 -m pip download \
    --dest /tmp/donkey_wheels \
    --platform "$PLAT" \
    --python-version "$PYVER" \
    --implementation cp \
    --abi "cp${PYVER/./}" \
    --only-binary=:all: \
    --no-cache-dir \
    -r "$HERE/requirements.txt"

python3 -m pip install \
    --target "$DEST" \
    --no-index --find-links /tmp/donkey_wheels \
    --platform "$PLAT" \
    --python-version "$PYVER" \
    --implementation cp \
    --abi "cp${PYVER/./}" \
    --only-binary=:all: \
    -r "$HERE/requirements.txt"

# 纯 python 包（py3-none-any）没有 abi 限制，上面两步已经覆盖
echo "== installed $(find "$DEST" -maxdepth 1 -type d | wc -l) packages, $(du -sh "$DEST" | cut -f1)"
echo "== 关键包自检"
for mod in numpy pandas donkeycar; do
    if [ -d "$DEST/$mod" ] || ls "$DEST" | grep -qi "^${mod}-.*dist-info$"; then
        echo "   [ok] $mod"
    else
        echo "   [MISS] $mod"
    fi
done
echo
echo "next: 在 turbopi_app 下跑 ./build.sh --donkey 生成带 donkeycar 的镜像"
