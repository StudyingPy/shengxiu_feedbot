"""公共工具：日志、字节/时长格式化、字节流速率跟踪。

格式化与速率跟踪刻意放在这里（而非 channel/telegram/progress.py），
因为 Provider 层的 archive 流式下载也要复用它们；channel/telegram 反过来
依赖 Provider 层的 ProgressHook 类型别名，把这些工具留在 channel 会构成循环依赖。
"""

from __future__ import annotations

import shutil
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
    "MIN_FREE_DISK_BYTES",
    "disk_free_bytes",
    "check_disk_free",
    "format_disk_full_message",
]


# ---------------------------------------------------------------------------
# 磁盘剩余空间护栏
# ---------------------------------------------------------------------------
# 任何"重活"（下载图片、归档 zip、zip2tph 接收）在入队前都先调用
# check_disk_free() 看 cache_dir 所在挂载点是否还剩 >= MIN_FREE_DISK_BYTES。
# 不足则在 placeholder 消息上回 format_disk_full_message()，避免把磁盘顶满。

MIN_FREE_DISK_BYTES = 500 * 1024 * 1024  # 500 MB 安全余量


def disk_free_bytes(path: Path | str) -> int:
    """返回 path 所在挂载点的剩余字节数。失败时返回 0（保守地视为磁盘满）。"""
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return 0


def check_disk_free(path: Path | str, extra_required: int = 0) -> tuple[bool, int, int]:
    """检查 path 所在挂载点是否还有 MIN_FREE_DISK_BYTES + extra_required 字节。

    返回 (ok, free_bytes, required_bytes)。
    extra_required 用于已知体积的任务（如 zip2tph 已知 document.file_size）。
    """
    free = disk_free_bytes(path)
    required = MIN_FREE_DISK_BYTES + max(0, extra_required)
    return free >= required, free, required


def format_disk_full_message(free_bytes: int, extra_required: int = 0) -> str:
    """生成统一的中文用户提示。"""
    head = f"⚠️ 服务器磁盘剩余 {fmt_bytes(free_bytes)}，"
    if extra_required > 0:
        mid = (
            f"本次任务约需 {fmt_bytes(extra_required)} + "
            f"{fmt_bytes(MIN_FREE_DISK_BYTES)} 安全余量，"
        )
    else:
        mid = f"低于 {fmt_bytes(MIN_FREE_DISK_BYTES)} 安全余量阈值，"
    tail = "已拒绝任务以避免顶满磁盘。请稍后重试或联系管理员清理缓存。"
    return head + mid + tail
