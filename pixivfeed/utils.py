"""公共工具：日志、路径辅助等。"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(level: str = "INFO", to_file: bool = False, file_path: str | None = None) -> None:
    """配置 loguru。可重复调用，每次先清空已有 handler。"""
    logger.remove()
    logger.add(
        sys.stdout,
        level=level.upper(),
        backtrace=True,
        diagnose=True,
        enqueue=True,
    )
    if to_file and file_path:
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            file_path,
            level=level.upper(),
            rotation="10 MB",
            retention="7 days",
            backtrace=True,
            diagnose=True,
            enqueue=True,
        )


__all__ = ["logger", "setup_logging"]
