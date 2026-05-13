"""SQLite 数据库初始化与连接管理。

只提供一个进程级单例的 connection。aiosqlite 的连接本身是 thread-safe 的代理，
但底层 SQLite 还是单写多读，写入用一把锁串行化即可——量太小，没必要复杂化。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite

from ..utils import logger

# 表结构。所有 DDL 用 CREATE IF NOT EXISTS，幂等可重复执行。
SCHEMA = """
CREATE TABLE IF NOT EXISTS allowed_users (
    user_id   INTEGER PRIMARY KEY,
    added_at  INTEGER NOT NULL,
    added_by  INTEGER
);

CREATE TABLE IF NOT EXISTS allowed_chats (
    chat_id   INTEGER PRIMARY KEY,
    added_at  INTEGER NOT NULL,
    added_by  INTEGER
);

CREATE TABLE IF NOT EXISTS chat_modes (
    chat_id   INTEGER PRIMARY KEY,
    mode      TEXT NOT NULL CHECK (mode IN ('auto', 'tg', 'ph')),
    updated_at INTEGER NOT NULL
);

-- Pixiv 作品 → Telegra.ph URL 缓存
-- kind: 'illust' | 'novel'
-- pixiv_id: PID 或 NID（数字字符串，避免精度问题）
-- 复合主键 (kind, pixiv_id)，因为 PID 和 NID 命名空间独立但可能撞号
CREATE TABLE IF NOT EXISTS telegraph_cache (
    kind        TEXT NOT NULL,
    pixiv_id    TEXT NOT NULL,
    telegraph_url TEXT NOT NULL,
    page_count  INTEGER,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (kind, pixiv_id)
);

CREATE INDEX IF NOT EXISTS idx_telegraph_cache_created ON telegraph_cache(created_at);

-- 运行时可改的设置。key 用点分路径如 'collectors.exhentai.igneous'。
-- 任何 admin 通过 /setting set 写入的值都进这个表，优先级高于 config.yaml。
-- value 一律存为 string；类型转换在读出时按目标 dataclass 字段类型做。
CREATE TABLE IF NOT EXISTS runtime_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    updated_by  INTEGER
);

-- 用户信息缓存：每次有授权用户触发任务时 upsert 一次，方便 /stats 展示昵称。
-- 这张表只用于显示，不参与权限判断。
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    first_name  TEXT,
    last_name   TEXT,
    username    TEXT,
    last_seen   INTEGER NOT NULL
);

-- 群组/私聊信息缓存：作用同 users，给 /stats 按群组分组时显示标题。
-- type 为 telegram chat type 字符串（private / group / supergroup / channel）。
-- 私聊 chat_id == user_id；title 在私聊里可能是空，展示时回落到用户名。
CREATE TABLE IF NOT EXISTS chats (
    chat_id     INTEGER PRIMARY KEY,
    type        TEXT NOT NULL,
    title       TEXT,
    username    TEXT,
    last_seen   INTEGER NOT NULL
);

-- 用量记录：每次任务（成功/失败/取消）都写一行，供 /stats 分析。
-- kind 见 storage/usage.py 的 KIND_* 常量。
-- gp_cost：归档下载时从 archiver 页面解析得到，其他场景为 0。
-- bytes_in / bytes_out：从外部下载到本地 / 从本地发出（telegraph 公网或 sendDocument）的字节数。
CREATE TABLE IF NOT EXISTS usage_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    chat_id    INTEGER,
    kind       TEXT NOT NULL,
    provider   TEXT,
    ref_id     TEXT,
    gp_cost    INTEGER NOT NULL DEFAULT 0,
    bytes_in   INTEGER NOT NULL DEFAULT 0,
    bytes_out  INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'ok'
);

CREATE INDEX IF NOT EXISTS idx_usage_log_ts        ON usage_log(ts);
CREATE INDEX IF NOT EXISTS idx_usage_log_user_ts   ON usage_log(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_log_chat_ts   ON usage_log(chat_id, ts);
"""


class Database:
    """轻量异步 SQLite 包装。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        # WAL 模式：读不阻塞写，写不阻塞读
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        logger.info(f"SQLite connected: {self.db_path}")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    @property
    def write_lock(self) -> asyncio.Lock:
        """写操作建议在此锁下进行。SQLite 单写者，避免 'database is locked'。"""
        return self._write_lock


__all__ = ["Database"]
