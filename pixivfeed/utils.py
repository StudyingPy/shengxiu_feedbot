"""公共工具：日志、字节/时长格式化、字节流速率跟踪。

格式化与速率跟踪刻意放在这里（而非 channel/telegram/progress.py），
因为 Provider 层的 archive 流式下载也要复用它们；channel/telegram 反过来
依赖 Provider 层的 ProgressHook 类型别名，把这些工具留在 channel 会构成循环依赖。
"""

from __future__ import annotations

import sys
import time
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


def fmt_bytes(n: int | float) -> str:
    """把字节数格式化为 KB/MB/GB。"""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}GB"


def fmt_duration(seconds: float) -> str:
    """把秒数格式化为 1m23s / 45s / 2h3m。"""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


class ByteRateTracker:
    """字节速率与 ETA 跟踪器。

    用法：
        tr = ByteRateTracker(total)        # total 可为 0 表示未知
        tr.add(chunk_len)
        text = tr.format("下载 zip")        # "下载 zip 12.3MB/45.6MB (27.0%) · 1.2MB/s · ~28s剩余"
    """

    def __init__(self, total: int = 0):
        self.total = total
        self.done = 0
        self._t0 = time.monotonic()

    def add(self, n: int) -> None:
        self.done += n

    @property
    def elapsed(self) -> float:
        return max(0.001, time.monotonic() - self._t0)

    @property
    def rate(self) -> float:
        return self.done / self.elapsed

    def format(self, label: str, suffix: str = "") -> str:
        rate = self.rate
        rate_s = f"{fmt_bytes(rate)}/s"
        if self.total > 0:
            pct = self.done * 100 / self.total
            eta_s = ""
            if rate > 0 and self.done < self.total and self.elapsed >= 2.0:
                eta_s = f" · ~{fmt_duration((self.total - self.done) / rate)}剩余"
            base = (
                f"⏳ {label} {fmt_bytes(self.done)}/{fmt_bytes(self.total)} "
                f"({pct:.1f}%) · {rate_s}{eta_s}"
            )
        else:
            base = f"⏳ {label} {fmt_bytes(self.done)} · {rate_s}"
        if suffix:
            base += f" {suffix}"
        return base


__all__ = [
    "logger",
    "setup_logging",
    "fmt_bytes",
    "fmt_duration",
    "ByteRateTracker",
]
