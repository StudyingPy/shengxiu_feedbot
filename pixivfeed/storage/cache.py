"""白名单 + Telegra.ph URL 缓存的 CRUD 操作。

所有写操作都在 db.write_lock 下进行，避免 SQLite 'database is locked' 错误。
读操作不加锁——SQLite 在 WAL 模式下读写不互相阻塞。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .db import Database


# ---------------------------------------------------------------------------
# 白名单
# ---------------------------------------------------------------------------


@dataclass
class AllowedEntry:
    id: int
    added_at: int
    added_by: int | None


class AllowList:
    """白名单管理。用户和聊天分别存表，但 API 形状一致。"""

    def __init__(self, db: Database, admin_users: list[int]):
        self.db = db
        # admin 永远视作放行，不写入数据库——这样修改 admin_users 配置即时生效
        self.admin_users = set(admin_users)

    # -------- user --------

    async def is_user_allowed(self, user_id: int) -> bool:
        if user_id in self.admin_users:
            return True
        async with self.db.conn.execute(
            "SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,)
        ) as cur:
            return (await cur.fetchone()) is not None

    async def add_user(self, user_id: int, added_by: int | None = None) -> bool:
        async with self.db.write_lock:
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO allowed_users(user_id, added_at, added_by) VALUES (?, ?, ?)",
                (user_id, int(time.time()), added_by),
            )
            await self.db.conn.commit()
        return True

    async def remove_user(self, user_id: int) -> bool:
        async with self.db.write_lock:
            cur = await self.db.conn.execute(
                "DELETE FROM allowed_users WHERE user_id = ?", (user_id,)
            )
            await self.db.conn.commit()
            return cur.rowcount > 0

    async def list_users(self) -> list[AllowedEntry]:
        async with self.db.conn.execute(
            "SELECT user_id, added_at, added_by FROM allowed_users ORDER BY added_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [AllowedEntry(*r) for r in rows]

    # -------- chat --------

    async def is_chat_allowed(self, chat_id: int) -> bool:
        async with self.db.conn.execute(
            "SELECT 1 FROM allowed_chats WHERE chat_id = ?", (chat_id,)
        ) as cur:
            return (await cur.fetchone()) is not None

    async def add_chat(self, chat_id: int, added_by: int | None = None) -> bool:
        async with self.db.write_lock:
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO allowed_chats(chat_id, added_at, added_by) VALUES (?, ?, ?)",
                (chat_id, int(time.time()), added_by),
            )
            await self.db.conn.commit()
        return True

    async def remove_chat(self, chat_id: int) -> bool:
        async with self.db.write_lock:
            cur = await self.db.conn.execute(
                "DELETE FROM allowed_chats WHERE chat_id = ?", (chat_id,)
            )
            await self.db.conn.commit()
            return cur.rowcount > 0

    async def list_chats(self) -> list[AllowedEntry]:
        async with self.db.conn.execute(
            "SELECT chat_id, added_at, added_by FROM allowed_chats ORDER BY added_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [AllowedEntry(*r) for r in rows]


# ---------------------------------------------------------------------------
# Telegra.ph URL 缓存
# ---------------------------------------------------------------------------


class TelegraphCache:
    """作品 → 已发布的 Telegra.ph URL（永久缓存）。"""

    def __init__(self, db: Database):
        self.db = db

    async def get(self, kind: str, pixiv_id: str) -> str | None:
        async with self.db.conn.execute(
            "SELECT telegraph_url FROM telegraph_cache WHERE kind = ? AND pixiv_id = ?",
            (kind, pixiv_id),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def put(self, kind: str, pixiv_id: str, url: str, page_count: int = 1) -> None:
        async with self.db.write_lock:
            await self.db.conn.execute(
                """
                INSERT INTO telegraph_cache(kind, pixiv_id, telegraph_url, page_count, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(kind, pixiv_id) DO UPDATE SET
                    telegraph_url = excluded.telegraph_url,
                    page_count = excluded.page_count,
                    created_at = excluded.created_at
                """,
                (kind, pixiv_id, url, page_count, int(time.time())),
            )
            await self.db.conn.commit()

    async def invalidate(self, kind: str, pixiv_id: str) -> None:
        async with self.db.write_lock:
            await self.db.conn.execute(
                "DELETE FROM telegraph_cache WHERE kind = ? AND pixiv_id = ?",
                (kind, pixiv_id),
            )
            await self.db.conn.commit()


__all__ = ["AllowList", "AllowedEntry", "TelegraphCache", "RuntimeSettings"]


# ---------------------------------------------------------------------------
# 运行时设置
# ---------------------------------------------------------------------------


class RuntimeSettings:
    """admin 私聊修改的运行时配置项。

    所有 value 在 SQLite 里都是字符串。读出时由 Config 层做类型转换。
    优先级：runtime_settings > 环境变量 > config.yaml > dataclass 默认。

    全量缓存在内存里以避免每次配置查询都打 SQLite——量很小（几十条）。
    写入时同时更新内存缓存。
    """

    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[str, str] = {}
        self._loaded = False

    async def load(self) -> None:
        """启动时调一次，把全量数据塞进内存。"""
        async with self.db.conn.execute(
            "SELECT key, value FROM runtime_settings"
        ) as cur:
            rows = await cur.fetchall()
        self._cache = {k: v for k, v in rows}
        self._loaded = True

    def get(self, key: str) -> str | None:
        """读单个 key。返回 None 表示未设置（让上层 fallback 到 env/file）。"""
        return self._cache.get(key)

    def all(self) -> dict[str, str]:
        return dict(self._cache)

    async def set(self, key: str, value: str, updated_by: int | None = None) -> None:
        async with self.db.write_lock:
            await self.db.conn.execute(
                """
                INSERT INTO runtime_settings(key, value, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (key, value, int(time.time()), updated_by),
            )
            await self.db.conn.commit()
        self._cache[key] = value

    async def unset(self, key: str) -> bool:
        """删除一个 runtime 设置，下次读时会 fallback 回 config.yaml。"""
        async with self.db.write_lock:
            cur = await self.db.conn.execute(
                "DELETE FROM runtime_settings WHERE key = ?", (key,)
            )
            await self.db.conn.commit()
            removed = cur.rowcount > 0
        if removed:
            self._cache.pop(key, None)
        return removed
