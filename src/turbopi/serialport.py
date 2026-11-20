"""裸串口：1000000 8N1 raw 模式，只用 os + termios。

为什么要自己写而不装 pyserial：
  1) 板端没有 pip / 没有外网，镜像里也不想塞 pip 安装步骤；
  2) 我们只需要"写命令 + 可选读回报"，termios 足够；
  3) Dockerfile 因此可以是零 RUN 指令，交叉构建不需要 qemu。

镜像/内核要求：Linux（termios、/dev/tty*）。
"""

import errno
import os
import select
import termios
import time


class SerialTimeout(Exception):
    pass


class RawSerial:
    """极简串口：open / write / read / close。"""

    def __init__(self, path, baud=1000000):
        self.path = path
        self.baud = baud
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self._configure()
        except Exception:
            os.close(self.fd)
            self.fd = -1
            raise

    def _configure(self):
        speed = getattr(termios, "B%d" % self.baud, None)
        if speed is None:
            raise ValueError("baud %d not supported by termios on this kernel" % self.baud)
        iflag, oflag, cflag, lflag, _, _, cc = termios.tcgetattr(self.fd)
        # cfmakeraw
        iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
                   | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON
                   | termios.IXOFF | termios.IXANY | termios.INPCK)
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON
                   | termios.ISIG | termios.IEXTEN)
        cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def write(self, data, timeout=1.0):
        total = bytes(data)
        view = memoryview(total)
        deadline = time.time() + timeout
        while view:
            try:
                view = view[os.write(self.fd, view):]
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.time() > deadline:
                    raise SerialTimeout("serial write timeout (%d bytes left)" % len(view))
                time.sleep(0.001)
        return len(total)

    def read(self, size=4096, timeout=0.0):
        if timeout:
            try:
                ready, _, _ = select.select([self.fd], [], [], timeout)
            except (OSError, ValueError):
                return b""
            if not ready:
                return b""
        try:
            return os.read(self.fd, size)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return b""
            raise

    def flush(self):
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except termios.error:
            pass

    def close(self):
        if self.fd >= 0:
            try:
                os.close(self.fd)
            finally:
                self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __repr__(self):
        return "<RawSerial %s @ %d 8N1>" % (self.path, self.baud)
