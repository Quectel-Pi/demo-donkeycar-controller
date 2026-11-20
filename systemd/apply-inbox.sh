#!/bin/sh
# 由 turbopi-inbox.path 触发：把 /opt/turbopi/inbox/*.run 依次安装（自解压 + install.sh）
set -u
DIR=/opt/turbopi/inbox
mkdir -p "$DIR"

found=0
for f in "$DIR"/*.run; do
    [ -e "$f" ] || continue
    found=1
    echo "== 开始安装 $f =="
    if sh "$f"; then
        mv -f "$f" "$f.done"
        echo "== 安装成功，已改名为 $(basename "$f").done =="
    else
        mv -f "$f" "$f.failed"
        echo "!! 安装失败，已改名为 $(basename "$f").failed（journalctl -u turbopi-inbox）"
    fi
done

[ "$found" = "1" ] || echo "inbox 里没有 .run 文件"
