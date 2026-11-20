"""幻尔驱动层的装配点（唯一 import turbopi 的地方，方便在 Windows/CI 上脱离硬件测试）。"""

import logging

log = logging.getLogger(__name__)


def build_config(cfg):
    """把 donkey 侧的配置翻译成 turbopi 驱动层的配置。"""
    from turbopi.config import Config as HalConfig

    overrides = {
        "baud": int(getattr(cfg, "SERIAL_BAUD", 1000000)),
        "max_linear": float(getattr(cfg, "MAX_LINEAR", 0.5)),
        "max_angular": float(getattr(cfg, "MAX_ANGULAR", 2.0)),
        "max_duty": float(getattr(cfg, "MAX_DUTY", 100.0)),
        "motor_mode": getattr(cfg, "MOTOR_MODE", "duty"),
    }
    device = getattr(cfg, "SERIAL_DEVICE", "") or None
    if device:
        overrides["device"] = device
    for key in ("I2C_BUSES", "I2C_ADDR", "I2C_INVERT", "BOARD_BACKEND"):
        value = getattr(cfg, key, None)
        if value not in (None, ""):
            overrides[key.lower().replace("board_", "")] = value
    wheelbase = getattr(cfg, "WHEELBASE", None)
    track = getattr(cfg, "TRACK_WIDTH", None)
    if wheelbase:
        overrides["wheelbase"] = float(wheelbase)
    if track:
        overrides["track_width"] = float(track)
    return HalConfig(**overrides)


def build_board(cfg):
    """按 BOARD_BACKEND 选后端：i2c(0x7A) / serial(AA55) / auto(先 I2C 后串口)。"""
    from turbopi.config import resolve_device

    hal_cfg = build_config(cfg)
    backend = (getattr(cfg, "BOARD_BACKEND", "") or hal_cfg.backend or "auto").lower()
    buses = [int(b) for b in str(hal_cfg.i2c_buses).replace(";", ",").split(",") if b.strip()]
    signs = [int(s) for s in str(hal_cfg.i2c_signs).replace(";", ",").split(",") if s.strip()]

    if backend in ("i2c", "auto"):
        from turbopi.i2cboard import I2CBoard

        board = I2CBoard.probe_first(buses, hal_cfg.i2c_addr, hal_cfg.i2c_invert, signs)
        if board is None and backend == "i2c":
            board = I2CBoard(buses[0] if buses else 3, hal_cfg.i2c_addr, hal_cfg.i2c_invert, signs)
            try:
                board.read_battery()
            except (OSError, IOError) as exc:
                board.close()
                raise RuntimeError(
                    "I2C 扩展板没应答（%s 上的 0x%02X）：%s。检查电池开关是否打开、40pin 是否压实"
                    % (board.path, hal_cfg.i2c_addr, exc))
        if board is not None:
            log.info("board %s (I2C, 4B 扩展板)", board.device)
            return board, hal_cfg
        log.info("i2c %s 上没有 0x%02X，回退到串口", buses, hal_cfg.i2c_addr)

    from turbopi.board import Board

    device = resolve_device(hal_cfg.device)
    log.info("board on %s @ %d", device, hal_cfg.baud)
    return Board(device, hal_cfg.baud), hal_cfg


def build_chassis(cfg, board=None):
    """返回 MecanumChassis（幻尔麦轮运动学）。不传 board 就自己开串口。"""
    from turbopi.chassis import MecanumChassis

    hal_cfg = build_config(cfg)
    if board is None:
        board, hal_cfg = build_board(cfg)
    return MecanumChassis(board, hal_cfg), board
