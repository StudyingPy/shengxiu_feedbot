"""占位消息进度条工具。

通过周期性 edit_message_text 给用户实时反馈。Telegram 对同一消息的 edit
有隐性限流（约每秒 1 次，过频会 429），所以这里默认节流 1 秒一次；
状态级（status）更新会强制刷新一次。

`fmt_bytes` / `fmt_duration` / `ByteRateTracker` 这些与 telegram 解耦的工具
住在 [pixivfeed/utils.py](../../utils.py)（Provider 层的 archive 下载也要复用），
本文件保留同名 re-export 以兼容现有 import。
"""

from __future__ import annotations

import asyncio
import time

from telegram.error import BadRequest, TelegramError

from ...provider import ProgressHook
from ...utils import ByteRateTracker, fmt_bytes, fmt_duration, logger


class Progress:
    """节流的占位消息进度更新器。

    update()  : 受节流（默认 1s/次），适合高频回调（per-byte / per-image）
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


def make_item_hook(progress: Progress | None, label: str) -> ProgressHook:
    """构造一个 item-style ProgressHook：把 (done, total) 渲染为 "⏳ {label} N/M · ~ETA剩余"。

    done/total 由 downloader 解读为"项数"（图片张数、telegraph 页数等）。
    `progress` 为 None 时返回 None，downloader 会跳过调用，无开销。

    ETA 在已经过 2 秒之后才开始显示，避免初期速率估计抖动。
    """
    if progress is None:
        return None

    t0 = time.monotonic()

    async def _hook(done: int, total: int) -> None:
        if total <= 0:
            return
        eta_s = ""
        elapsed = time.monotonic() - t0
        if done > 0 and total > done and elapsed >= 2.0:
            rate = done / elapsed
            if rate > 0:
                eta_s = f" · ~{fmt_duration((total - done) / rate)}剩余"
        await progress.update(f"⏳ {label} {done}/{total}{eta_s}")

    return _hook


def make_bytes_hook(progress: Progress | None, label: str) -> ProgressHook:
    """构造一个 bytes-style ProgressHook：把 (downloaded, total_bytes) 渲染为
    "⏳ {label} 12.3MB/45.6MB (27.0%) · 1.2MB/s · ~28s剩余"。

    `progress` 为 None 时返回 None，downloader 会跳过调用。

    内部复用 `ByteRateTracker` 的 format 逻辑：每次调用把 done/total 推入
    tracker 后调 format。Tracker 的速率基于其内部 t0（首次构造时），所以
    rate 是真实的累计速率，不会被节流跳过影响。
    """
    if progress is None:
        return None

    tracker = ByteRateTracker(total=0)

    async def _hook(done: int, total: int) -> None:
        tracker.total = total
        tracker.done = done
        await progress.update(tracker.format(label))

    return _hook


__all__ = [
    "Progress",
    "ImageCounter",
    "ByteRateTracker",
    "fmt_bytes",
    "fmt_duration",
    "make_item_hook",
    "make_bytes_hook",
]
