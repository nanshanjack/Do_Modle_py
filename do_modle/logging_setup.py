"""日志初始化。零业务依赖。"""

from __future__ import annotations

import logging
import sys

__all__ = ["setup_logging", "get_logger"]

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DEFAULT_LOGGER = "do_modle"


def setup_logging(level: int | str = logging.INFO, stream=None) -> logging.Logger:
    """配置并返回根 logger。重复调用不会叠加 handler。"""
    logger = logging.getLogger(_DEFAULT_LOGGER)
    logger.setLevel(level)
    if not any(getattr(h, "_do_modle_handler", False) for h in logger.handlers):
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler._do_modle_handler = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    """获取子 logger，统一挂在 ``do_modle`` 命名空间下。"""
    if name == _DEFAULT_LOGGER or name.startswith(f"{_DEFAULT_LOGGER}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_DEFAULT_LOGGER}.{name}")
