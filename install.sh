#!/bin/sh
# 在开发板上运行（root）：把解包出来的代码装到 /opt/turbopi 并注册开机自启
set -eu

APP_DIR="${APP_DIR:-/opt/turbopi}"
HERE=$(cd "$(dirname "$0")" && pwd)

[ -f "${HERE}/donkey/manage.py" ] || {
    echo "!! install.sh 要在解包后的 turbopi_app 目录里运行（当前 ${HERE}）"; exit 1; }

mkdir -p "${APP_DIR}"

# 先拷到 .new 再换名，避免拷到一半断电留下半个目录
for d in src donkey tools; do
    rm -rf "${APP_DIR}/${d}.new"
    cp -a "${HERE}/${d}" "${APP_DIR}/${d}.new"
    rm -rf "${APP_DIR}/${d}"
    mv "${APP_DIR}/${d}.new" "${APP_DIR}/${d}"
done

install -m 0644 "${HERE}/turbopi.service" /etc/systemd/system/turbopi.service
[ -f "${HERE}/MANIFEST.txt" ] && install -m 0644 "${HERE}/MANIFEST.txt" "${APP_DIR}/MANIFEST.txt"
[ -f "${HERE}/VERSION" ] && install -m 0644 "${HERE}/VERSION" "${APP_DIR}/VERSION"
install -m 0644 "${HERE}/systemd/turbopi-inbox.path" /etc/systemd/system/turbopi-inbox.path
install -m 0644 "${HERE}/systemd/turbopi-inbox.service" /etc/systemd/system/turbopi-inbox.service

mkdir -p "${APP_DIR}/bin" "${APP_DIR}/inbox"
install -m 0755 "${HERE}/systemd/apply-inbox.sh" "${APP_DIR}/bin/apply-inbox.sh"
if [ -f "${HERE}/bin/car" ]; then
    install -m 0755 "${HERE}/bin/car" "${APP_DIR}/bin/car"
    ln -sf "${APP_DIR}/bin/car" /usr/local/bin/car
fi

systemctl daemon-reload
systemctl enable turbopi.service >/dev/null 2>&1 || true
if ! systemctl restart turbopi.service; then
    echo "!! turbopi.service 启动失败，最近日志："
    journalctl -u turbopi.service -b --no-pager 2>/dev/null | tail -15 || true
fi
systemctl enable turbopi-inbox.path >/dev/null 2>&1 || true
systemctl restart turbopi-inbox.path

sleep 6

# 把"板上到底装的是哪一版"记下来，方便和服务器上刚编出来的包比对
CODE_SHA=$( cd "${APP_DIR}" && find src tools donkey -name '*.py' -not -path '*/vendor/*' \
            | LC_ALL=C sort | xargs -r sha256sum | sha256sum | cut -d' ' -f1 )
{
    echo "installed at : $(date '+%Y-%m-%d %H:%M:%S %z')"
    echo "app dir      : ${APP_DIR}"
    echo "CODE_SHA     : ${CODE_SHA}"
    echo "service      : $(systemctl is-active turbopi.service)"
    echo "--- package MANIFEST ---"
    cat "${APP_DIR}/MANIFEST.txt" 2>/dev/null || echo "(no MANIFEST.txt)"
} > "${APP_DIR}/BUILD_INFO"

echo "== 安装完成 =="
echo "目录 : ${APP_DIR}"
echo "服务 : $(systemctl is-active turbopi.service) / $(systemctl is-enabled turbopi.service)"
echo "指纹 : CODE_SHA=${CODE_SHA}"
journalctl -u turbopi.service -b --no-pager 2>/dev/null | tail -6 || true
