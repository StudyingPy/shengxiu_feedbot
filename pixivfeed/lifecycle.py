"""进程级后台任务的登记与退出收尾。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from .utils import logger

_BACKGROUND_TASKS_KEY = "_pixivfeed_background_tasks"


def start_background_task(
    app: Any,
    coro: Awaitable[Any],
    *,
    name: str,
) -> asyncio.Task[Any]:
    """启动并登记一个需要在应用退出时统一收尾的后台任务。"""
    task = asyncio.create_task(coro, name=name)
    tasks: set[asyncio.Task[Any]] = app.bot_data.setdefault(
        _BACKGROUND_TASKS_KEY,
        set(),
    )
    tasks.add(task)

    def _finished(done: asyncio.Task[Any]) -> None:
        # 已完成的短任务不应一直留在 bot_data；同时主动取走异常，避免
        # fire-and-forget 任务失败后只出现含糊的“exception was never retrieved”。
        tasks.discard(done)
        if done.cancelled():
            return
        try:
            error = done.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(f"background task {done.get_name()!r} failed: {error!r}")

    task.add_done_callback(_finished)
    return task


async def shutdown_bot_background_tasks(app: Any) -> None:
    """停止 JobQueue，并取消、等待所有登记过的后台任务。

    调用方必须在关闭 Database/R2Client 之前执行；否则仍在运行的队列任务或
    R2 LRU 扫描可能继续访问已经关闭的底层资源。
    """
    job_queue = app.bot_data.get("job_queue")
    if job_queue is not None:
        try:
            await job_queue.stop_all()
        except Exception:
            logger.exception("job queue shutdown failed")

    tasks: set[asyncio.Task[Any]] = app.bot_data.get(
        _BACKGROUND_TASKS_KEY,
        set(),
    )
    if not tasks:
        return

    tracked = list(tasks)
    for task in tracked:
        if not task.done():
            task.cancel()

    results = await asyncio.gather(*tracked, return_exceptions=True)
    tasks.clear()
    for task, result in zip(tracked, results, strict=True):
        if isinstance(result, BaseException) and not isinstance(
            result,
            asyncio.CancelledError,
        ):
            logger.warning(f"background task {task.get_name()!r} ended with error during shutdown: " f"{result!r}")


__all__ = ["shutdown_bot_background_tasks", "start_background_task"]
