"""最终消息交付的 Telegram 限频重试。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TypeVar

from telegram.error import RetryAfter

from ...utils import logger

FINAL_MESSAGE_MAX_RETRIES = 10
_T = TypeVar("_T")


async def retry_on_rate_limit(operation: Callable[..., Awaitable[_T]], *args, **kwargs) -> _T:
    """原样重试同一次交付，最多重试 10 次，每次等待 retry_after + 1 秒。

    仅重试 Telegram 明确拒绝的限频请求；其它错误和任务取消直接向上传播。
    不重跑下载/发布，也不发送等待提示，以免提示本身再次撞上限频。
    """
    retries = 0
    while True:
        try:
            return await operation(*args, **kwargs)
        except RetryAfter as e:
            if retries >= FINAL_MESSAGE_MAX_RETRIES:
                logger.warning("final Telegram delivery exhausted rate-limit retries")
                raise
            retries += 1
            delay = e.retry_after
            seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
            wait_s = max(0.0, seconds) + 1.0
            logger.warning(
                f"final Telegram delivery rate limited; wait {wait_s:g}s "
                f"before retry {retries}/{FINAL_MESSAGE_MAX_RETRIES}"
            )
            await asyncio.sleep(wait_s)
