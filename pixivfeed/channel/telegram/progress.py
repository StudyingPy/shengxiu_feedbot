"""占位消息进度条工具。

通过周期性 edit_message_text 给用户实时反馈。Telegram 对同一消息的 edit
有隐性限流（约每秒 1 次，过频会 429），所以这里默认节流 3 秒一次；
状态级（status）更新会强制刷新一次。
"""

from __future__ import annotations

import asyncio
import time

from telegram.error import BadRequest, TelegramError

from ...utils import logger


def fmt_bytes(n: int | float) -> str:
    """把字节数格式化为 KB/MB/GB。"""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}GB"


class Progress:
    """节流的占位消息进度更新器。

    update()  : 受节流（默认 3s/次），适合高频回调（per-byte / per-image）
    status()  : 立即写入一次（除非与上次相同），适合阶段切换提示

    reply_markup 会被记住并在每次 edit 时带上，否则 Telegram 的 edit_text
    会清除按钮 —— 进度更新过程中按钮会消失。需要去掉按钮时调 set_markup(None)。
    """

    def __init__(self, message, prefix: str = "", min_interval: float = 1.0):
        self._msg = message
        self._prefix = prefix
        self._last_text = ""
        self._last_t = 0.0
        self._lock = asyncio.Lock()
        self._min_interval = min_interval
        self._markup = None

    def set_markup(self, markup) -> None:
        """设置/清除当前要保留的 reply_markup。后续 update/status/finish 都会带上。"""
        self._markup = markup

    def _full(self, text: str) -> str:
        if not self._prefix:
            return text
        return f"{self._prefix}\n{text}"

    async def _do_edit(self, full: str) -> None:
        try:
            await self._msg.edit_text(full, reply_markup=self._markup)
            self._last_text = full
            self._last_t = time.monotonic()
        except BadRequest as e:
            # message is not modified / message to edit not found 等都忽略
            if "not modified" in str(e).lower():
                self._last_text = full
                self._last_t = time.monotonic()
            else:
                logger.debug(f"progress edit BadRequest: {e}")
        except TelegramError as e:
            logger.debug(f"progress edit failed: {e}")

    async def update(self, text: str) -> None:
        """受节流的更新。"""
        full = self._full(text)
        now = time.monotonic()
        if (now - self._last_t) < self._min_interval:
            return
        if full == self._last_text:
            return
        async with self._lock:
            await self._do_edit(full)

    async def status(self, text: str) -> None:
        """阶段切换：立即刷新一次（除非内容相同）。"""
        full = self._full(text)
        if full == self._last_text:
            return
        async with self._lock:
            await self._do_edit(full)

    async def finish(self, text: str) -> None:
        """终态：强制刷新（重置节流计时）。"""
        full = self._full(text)
        async with self._lock:
            await self._do_edit(full)


class ImageCounter:
    """N 张图片的计数器，带节流式 progress 回调。

    用法：
        ctr = ImageCounter(total=42, progress=p, label="下载图片")
        # 每张完成时
        await ctr.tick()
    """

    def __init__(self, total: int, progress: Progress | None, label: str = "处理中"):
        self.total = total
        self.done = 0
        self._p = progress
        self._label = label
        self._t0 = time.monotonic()

    async def tick(self) -> None:
        self.done += 1
        if self._p is None:
            return
        eta_s = ""
        elapsed = time.monotonic() - self._t0
        if self.done > 0 and self.total > self.done and elapsed >= 2.0:
            rate = self.done / elapsed
            remain = (self.total - self.done) / rate if rate > 0 else 0
            eta_s = f" · ~{fmt_duration(remain)}剩余"
        await self._p.update(f"⏳ {self._label} {self.done}/{self.total}{eta_s}")


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


__all__ = ["Progress", "ImageCounter", "ByteRateTracker", "fmt_bytes", "fmt_duration"]
