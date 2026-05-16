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
    """作品 → 已发布的 Telegra.ph URL（永久缓存）。

    durability 元数据（PR-2 引入）：
      - durable=True：所有图片已成功上传 R2，或 text-only 无图页
      - durable=False：含 fallback / 部分失败 / 整批失败 / R2 未启用
      - legacy 行（schema 升级前）：durable=0 且 r2_image_count IS NULL
    普通用户命中任何条目都返回 URL；admin --r2 命中非 durable 行视为 miss 触发重发。
    """

    def __init__(self, db: Database):
        self.db = db

    async def get(self, kind: str, pixiv_id: str) -> "CacheEntry | None":
        async with self.db.conn.execute(
            """SELECT telegraph_url, page_count, durable,
                      r2_image_count, fallback_image_count, fallback_reason, created_at
               FROM telegraph_cache WHERE kind = ? AND pixiv_id = ?""",
            (kind, pixiv_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return CacheEntry(
            url=row[0],
            page_count=row[1] or 1,
            durable=bool(row[2]),
            r2_image_count=row[3],
            fallback_image_count=row[4],
            fallback_reason=row[5] or "",
            created_at=row[6],
        )

    async def put(
        self, kind: str, pixiv_id: str, url: str,
        *,
        page_count: int = 1,
        durable: bool = False,
        r2_image_count: int | None = None,
        fallback_image_count: int | None = None,
        fallback_reason: str = "",
    ) -> None:
        """写入或覆盖缓存。force_r2 重发路径走 upsert（不要先 invalidate 再 put——
        避免并发空洞让普通用户在窗口内触发第二次 publish）。
        """
        async with self.db.write_lock:
            await self.db.conn.execute(
                """
                INSERT INTO telegraph_cache(
                    kind, pixiv_id, telegraph_url, page_count, created_at,
                    durable, r2_image_count, fallback_image_count, fallback_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, pixiv_id) DO UPDATE SET
                    telegraph_url = excluded.telegraph_url,
                    page_count = excluded.page_count,
                    created_at = excluded.created_at,
                    durable = excluded.durable,
                    r2_image_count = excluded.r2_image_count,
                    fallback_image_count = excluded.fallback_image_count,
                    fallback_reason = excluded.fallback_reason
                """,
                (
                    kind, pixiv_id, url, page_count, int(time.time()),
                    1 if durable else 0,
                    r2_image_count, fallback_image_count, fallback_reason or None,
                ),
            )
            await self.db.conn.commit()

    async def invalidate(self, kind: str, pixiv_id: str) -> None:
        async with self.db.write_lock:
            await self.db.conn.execute(
                "DELETE FROM telegraph_cache WHERE kind = ? AND pixiv_id = ?",
                (kind, pixiv_id),
            )
            await self.db.conn.commit()

    async def stats(self) -> "CacheStats":
        """聚合 durability 分布给 /stats 用。"""
        async with self.db.conn.execute(
            """SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN durable = 1 THEN 1 ELSE 0 END) AS durable_count,
                SUM(CASE WHEN durable = 0 AND r2_image_count IS NULL THEN 1 ELSE 0 END) AS legacy_count
               FROM telegraph_cache"""
        ) as cur:
            row = await cur.fetchone()
        total = row[0] or 0 if row else 0
        durable = row[1] or 0 if row else 0
        legacy = row[2] or 0 if row else 0
        # fallback_reason 分布（只看非 durable 非 legacy 的行）
        breakdown: dict[str, int] = {}
        async with self.db.conn.execute(
            """SELECT COALESCE(fallback_reason, ''), COUNT(*)
               FROM telegraph_cache
               WHERE durable = 0 AND r2_image_count IS NOT NULL
               GROUP BY fallback_reason"""
        ) as cur:
            for reason, cnt in await cur.fetchall():
                breakdown[reason or "(empty)"] = cnt
        return CacheStats(
            total=total,
            durable=durable,
            legacy=legacy,
            fallback_breakdown=breakdown,
        )


@dataclass
class CacheEntry:
    """telegraph_cache 单条行。get() 返回它而不是裸 URL，调用方可读 durable。"""

    url: str
    page_count: int
    durable: bool
    r2_image_count: int | None         # NULL = legacy 行（schema 升级前未知）
    fallback_image_count: int | None
    fallback_reason: str                # 见 publisher.telegraph.FallbackReason 枚举
    created_at: int


@dataclass
class CacheStats:
    total: int
    durable: int
    legacy: int                           # durable=0 且 r2_image_count IS NULL
    fallback_breakdown: dict[str, int]    # fallback_reason → count（非 durable 非 legacy）


__all__ = ["AllowList", "AllowedEntry", "CacheEntry", "CacheStats", "TelegraphCache", "RuntimeSettings"]


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
