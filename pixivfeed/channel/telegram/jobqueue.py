"""按"重活类型"分类的优先队列。

设计目的：上游 PTB 默认每个 update 起独立 task，重型任务（archive zip 下载、
telegraph 多页发布等）在群里被多人同时触发会瞬间打爆内存与网络，最终系统卡死。

每个 category 是一个独立的 worker pool（asyncio.Semaphore + 优先 PriorityQueue）：
- archive_zip       : 1   eh/ex archive 全流程 + 用户上传 zip，最重
- direct_image      : 2   pixiv 直发图片下载/发送
- telegraph_publish : 3   通用 telegraph 发布

Admin 永远 priority=0，普通用户 priority=1，按 (priority, enqueue_seq) 排序。
同一用户在同一 category 等待中的任务超过 max_per_user_pending（默认 2）时拒绝入队，
防止用户因显示超时反复点按按钮把队列灌爆。
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ...utils import logger


@dataclass(order=True)
class _Item:
    priority: int
    seq: int
    # 以下字段不参与排序
    user_id: int = field(compare=False)
    coro_factory: Callable[[], Awaitable[None]] = field(compare=False)
    on_position: Callable[[int], Awaitable[None]] | None = field(default=None, compare=False)
    on_reject: Callable[[str], Awaitable[None]] | None = field(default=None, compare=False)
    on_started: Callable[[], Awaitable[None]] | None = field(default=None, compare=False)
    on_cancelled: Callable[[], Awaitable[None]] | None = field(default=None, compare=False)
    cancelled: bool = field(default=False, compare=False)
    # 任务跑起来后塞进去，给 cancel() 用
    running_task: "asyncio.Task | None" = field(default=None, compare=False)
    enqueued_at: float = field(default_factory=time.monotonic, compare=False)


@dataclass
class JobHandle:
    """submit 返回的句柄。用于外部触发取消。"""
    item: _Item
    queue: "_CategoryQueue"

    async def cancel(self) -> str:
        """返回取消的状态描述：'queued' / 'running' / 'finished' / 'already-cancelled'。"""
        if self.item.cancelled:
            return "already-cancelled"
        if self.item.running_task is not None:
            self.item.cancelled = True
            self.item.running_task.cancel()
            return "running"
        # 还在队列里
        self.item.cancelled = True
        return "queued"


class _CategoryQueue:
    def __init__(self, name: str, concurrency: int, max_per_user_pending: int):
        self.name = name
        self.concurrency = concurrency
        self.max_per_user_pending = max_per_user_pending
        self._queue: asyncio.PriorityQueue[_Item] = asyncio.PriorityQueue()
        self._workers: list[asyncio.Task] = []
        self._seq = itertools.count()
        # 用户 -> 在队列里待处理的 _Item 列表（不含正在跑的）
        self._user_pending: dict[int, list[_Item]] = {}
        self._running = 0
        self._stop = False

    async def start(self) -> None:
        for i in range(self.concurrency):
            self._workers.append(asyncio.create_task(self._worker_loop(i)))
        logger.info(f"job queue [{self.name}] started with concurrency={self.concurrency}")

    async def stop(self) -> None:
        self._stop = True
        for t in self._workers:
            t.cancel()
        for t in self._workers:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def pending_count(self) -> int:
        """所有用户在队列里等待的任务数（不含正在跑的）。"""
        return sum(len(v) for v in self._user_pending.values())

    @property
    def running_count(self) -> int:
        return self._running

    def user_pending(self, user_id: int) -> int:
        return len(self._user_pending.get(user_id, []))

    async def submit(
        self,
        *,
        user_id: int,
        is_admin: bool,
        coro_factory: Callable[[], Awaitable[None]],
        on_position: Callable[[int], Awaitable[None]] | None = None,
        on_reject: Callable[[str], Awaitable[None]] | None = None,
        on_started: Callable[[], Awaitable[None]] | None = None,
        on_cancelled: Callable[[], Awaitable[None]] | None = None,
    ) -> "JobHandle | None":
        """入队。返回 JobHandle 表示已入队（或将立即执行），None 表示被拒。

        on_position(pos): 入队后立即调一次告诉用户排在第几位（pos>=1 表示队列里前面有 pos-1 个，
                         pos=0 表示马上要跑）。
        on_started():    实际开始执行前调一次（可用于切换占位消息文案）。
        on_reject(msg):  超过 per-user 上限时调，告诉用户为啥被拒。
        on_cancelled():  收到取消（无论排队中还是运行时）后调一次清理。
        """
        if not is_admin and self.user_pending(user_id) >= self.max_per_user_pending:
            if on_reject:
                await on_reject(
                    f"⚠️ 你已经有 {self.user_pending(user_id)} 个 {self.name} 任务在排队，"
                    "请等待这些任务处理完后再提交新的。"
                )
            logger.info(
                f"job [{self.name}] user={user_id} rejected: "
                f"per-user pending {self.user_pending(user_id)} >= {self.max_per_user_pending}"
            )
            return None

        item = _Item(
            priority=0 if is_admin else 1,
            seq=next(self._seq),
            user_id=user_id,
            coro_factory=coro_factory,
            on_position=on_position,
            on_reject=on_reject,
            on_started=on_started,
            on_cancelled=on_cancelled,
        )
        self._user_pending.setdefault(user_id, []).append(item)
        await self._queue.put(item)
        ahead = self.pending_count - 1
        logger.info(
            f"job [{self.name}] seq={item.seq} user={user_id} prio={item.priority} "
            f"enqueued (ahead={ahead}, running={self._running}/{self.concurrency})"
        )

        # 入队即告知位置：当前队列大小（含本任务） - 已有 worker 空位 = 待排队位
        # 但简化：直接报"前面 N 个"，N = pending_count - 1（除自己）
        if on_position:
            pos = self.pending_count - 1   # 自己之前还有几个
            try:
                await on_position(pos)
            except Exception as e:
                logger.debug(f"on_position callback failed: {e}")
        return JobHandle(item=item, queue=self)

    async def _worker_loop(self, idx: int) -> None:
        while not self._stop:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                # 从 user_pending 里移除
                lst = self._user_pending.get(item.user_id, [])
                try:
                    lst.remove(item)
                    if not lst:
                        self._user_pending.pop(item.user_id, None)
                except ValueError:
                    pass

                if item.cancelled:
                    logger.info(
                        f"job [{self.name}] seq={item.seq} user={item.user_id} cancelled before start"
                    )
                    if item.on_cancelled:
                        try:
                            await item.on_cancelled()
                        except Exception as e:
                            logger.debug(f"on_cancelled callback failed: {e}")
                    continue

                self._running += 1
                wait_s = time.monotonic() - item.enqueued_at
                logger.info(
                    f"job [{self.name}] seq={item.seq} user={item.user_id} "
                    f"prio={item.priority} starting (waited {wait_s:.1f}s)"
                )
                if item.on_started:
                    try:
                        await item.on_started()
                    except Exception as e:
                        logger.debug(f"on_started callback failed: {e}")
                t0 = time.monotonic()
                # 把 coro 包成 Task 以便外部 cancel
                task = asyncio.create_task(item.coro_factory())
                item.running_task = task
                try:
                    await task
                    elapsed = time.monotonic() - t0
                    logger.info(
                        f"job [{self.name}] seq={item.seq} user={item.user_id} "
                        f"completed in {elapsed:.1f}s"
                    )
                except asyncio.CancelledError:
                    elapsed = time.monotonic() - t0
                    logger.info(
                        f"job [{self.name}] seq={item.seq} user={item.user_id} "
                        f"cancelled after {elapsed:.1f}s"
                    )
                    if item.on_cancelled:
                        try:
                            await item.on_cancelled()
                        except Exception as e:
                            logger.debug(f"on_cancelled callback failed: {e}")
                except Exception as e:
                    elapsed = time.monotonic() - t0
                    logger.exception(
                        f"job [{self.name}] seq={item.seq} user={item.user_id} "
                        f"crashed after {elapsed:.1f}s: {e}"
                    )
                finally:
                    item.running_task = None
                    self._running -= 1
            finally:
                self._queue.task_done()


class JobQueueManager:
    """聚合多个 _CategoryQueue。"""

    def __init__(self, admin_users: set[int]):
        self.admin_users = set(admin_users)
        self.categories: dict[str, _CategoryQueue] = {}

    def register(self, name: str, concurrency: int, max_per_user_pending: int = 2) -> None:
        if name in self.categories:
            raise ValueError(f"category {name!r} already registered")
        self.categories[name] = _CategoryQueue(name, concurrency, max_per_user_pending)

    async def start_all(self) -> None:
        for q in self.categories.values():
            await q.start()

    async def stop_all(self) -> None:
        for q in self.categories.values():
            await q.stop()

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_users

    async def submit(
        self,
        category: str,
        *,
        user_id: int,
        coro_factory: Callable[[], Awaitable[None]],
        on_position: Callable[[int], Awaitable[None]] | None = None,
        on_reject: Callable[[str], Awaitable[None]] | None = None,
        on_started: Callable[[], Awaitable[None]] | None = None,
        on_cancelled: Callable[[], Awaitable[None]] | None = None,
    ) -> "JobHandle | None":
        q = self.categories.get(category)
        if q is None:
            raise KeyError(f"unknown queue category: {category}")
        return await q.submit(
            user_id=user_id,
            is_admin=self.is_admin(user_id),
            coro_factory=coro_factory,
            on_position=on_position,
            on_reject=on_reject,
            on_started=on_started,
            on_cancelled=on_cancelled,
        )


__all__ = ["JobQueueManager", "JobHandle"]
