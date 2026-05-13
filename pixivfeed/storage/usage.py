"""用量统计与用户信息表。

设计原则：
- 写入永远尽力而为：任何记录失败都吞掉错误（log warning），绝不让统计影响主流程。
- /stats 是 admin only 的查询命令，输出格式就在这里组装好返回字符串。
- 时间窗仅按 UTC 秒数算，不引入 tzinfo 复杂度。

KIND 取值（统一字符串常量）：
    pixiv_telegraph    pixiv illust 走 telegraph 发布
    pixiv_direct       pixiv illust 直发图片（含群里默认走的）
    pixiv_novel        pixiv 小说
    eh_page            eh/ex 网页模式（page_sample / page_original）
    eh_archive         eh/ex 归档模式（archive_resample / archive_original）
    nhentai            nhentai
    archive_cmd        /archive 命令（zip 直接发回；具体子类型在 ref_id 里看不到，但 kind 已能区分）
    zip2tph            /zip2tph 用户上传 zip → telegraph
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .db import Database


KIND_PIXIV_TELEGRAPH = "pixiv_telegraph"
KIND_PIXIV_DIRECT = "pixiv_direct"
KIND_PIXIV_NOVEL = "pixiv_novel"
KIND_EH_PAGE = "eh_page"
KIND_EH_ARCHIVE = "eh_archive"
KIND_NHENTAI = "nhentai"
KIND_ARCHIVE_CMD = "archive_cmd"
KIND_ZIP2TPH = "zip2tph"

KIND_ZH = {
    KIND_PIXIV_TELEGRAPH: "pixiv telegraph",
    KIND_PIXIV_DIRECT: "pixiv 直发",
    KIND_PIXIV_NOVEL: "pixiv 小说",
    KIND_EH_PAGE: "eh/ex 网页",
    KIND_EH_ARCHIVE: "eh/ex 归档",
    KIND_NHENTAI: "nhentai",
    KIND_ARCHIVE_CMD: "/archive",
    KIND_ZIP2TPH: "/zip2tph",
}


@dataclass
class UserSummary:
    user_id: int
    display: str          # "{first} {last}" 或 username 兜底
    username: str | None
    tasks: int
    gp_cost: int
    bytes_in: int
    bytes_out: int


@dataclass
class ChatSummary:
    chat_id: int
    type: str                # "private" / "group" / "supergroup" / "channel"，未知时空
    display: str             # title 或 username 兜底；private 时回落到 user_<id>
    title: str | None
    username: str | None
    tasks: int
    gp_cost: int
    bytes_in: int
    bytes_out: int


class UsageStore:
    """对 usage_log + users 表的薄封装。所有写入静默失败。"""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def upsert_user(
        self,
        user_id: int,
        first_name: str | None,
        last_name: str | None,
        username: str | None,
    ) -> None:
        """每次接到授权用户消息时调一次，更新 last_seen 和最新昵称。"""
        try:
            async with self.db.write_lock:
                await self.db.conn.execute(
                    """
                    INSERT INTO users (user_id, first_name, last_name, username, last_seen)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        first_name = excluded.first_name,
                        last_name  = excluded.last_name,
                        username   = excluded.username,
                        last_seen  = excluded.last_seen
                    """,
                    (user_id, first_name, last_name, username, int(time.time())),
                )
                await self.db.conn.commit()
        except Exception as e:
            from ..utils import logger
            logger.warning(f"usage.upsert_user failed (non-fatal): {e}")

    async def upsert_chat(
        self,
        chat_id: int,
        chat_type: str,
        title: str | None,
        username: str | None,
    ) -> None:
        """每次接到授权用户消息时调一次，方便 /stats 按 chat_id 分组时回填标题。
        chat_type 取 Telegram 'private' / 'group' / 'supergroup' / 'channel'。"""
        try:
            async with self.db.write_lock:
                await self.db.conn.execute(
                    """
                    INSERT INTO chats (chat_id, type, title, username, last_seen)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        type      = excluded.type,
                        title     = excluded.title,
                        username  = excluded.username,
                        last_seen = excluded.last_seen
                    """,
                    (chat_id, chat_type, title, username, int(time.time())),
                )
                await self.db.conn.commit()
        except Exception as e:
            from ..utils import logger
            logger.warning(f"usage.upsert_chat failed (non-fatal): {e}")

    async def log(
        self,
        *,
        user_id: int,
        chat_id: int | None,
        kind: str,
        provider: str | None = None,
        ref_id: str | None = None,
        gp_cost: int = 0,
        bytes_in: int = 0,
        bytes_out: int = 0,
        status: str = "ok",
    ) -> None:
        """写入一条用量记录。失败不抛。"""
        try:
            async with self.db.write_lock:
                await self.db.conn.execute(
                    """
                    INSERT INTO usage_log
                        (ts, user_id, chat_id, kind, provider, ref_id, gp_cost, bytes_in, bytes_out, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(time.time()), user_id, chat_id, kind, provider, ref_id,
                        int(gp_cost), int(bytes_in), int(bytes_out), status,
                    ),
                )
                await self.db.conn.commit()
        except Exception as e:
            from ..utils import logger
            logger.warning(f"usage.log failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def get_user_by_username(self, username: str) -> int | None:
        """按 @username 找 user_id。username 不带 @；不区分大小写。"""
        username = username.lstrip("@").strip()
        if not username:
            return None
        async with self.db.conn.execute(
            "SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)",
            (username,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def get_user_display(self, user_id: int) -> tuple[str, str | None]:
        """返回 (display_name, username)。display_name = '{first} {last}' 或 username 兜底。"""
        async with self.db.conn.execute(
            "SELECT first_name, last_name, username FROM users WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return f"user_{user_id}", None
        first, last, uname = row
        name = " ".join(p for p in (first, last) if p) or uname or f"user_{user_id}"
        return name, uname

    async def get_chat_by_username(self, username: str) -> int | None:
        """按 @username 找 chat_id。不区分大小写。"""
        username = username.lstrip("@").strip()
        if not username:
            return None
        async with self.db.conn.execute(
            "SELECT chat_id FROM chats WHERE LOWER(username) = LOWER(?)",
            (username,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def get_chat_display(self, chat_id: int) -> tuple[str, str, str | None]:
        """返回 (display, chat_type, username)。
        display：title 优先，否则 @username，否则按类型给出占位。
        私聊会自动回落到 users 表里的 display name。"""
        async with self.db.conn.execute(
            "SELECT type, title, username FROM chats WHERE chat_id = ?",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return f"chat_{chat_id}", "", None
        ctype, title, uname = row
        if ctype == "private":
            # 私聊 chat_id == user_id，借 users 表拿真名
            try:
                disp, uuname = await self.get_user_display(chat_id)
                return f"私聊 · {disp}", ctype, uuname or uname
            except Exception:
                return f"私聊 · user_{chat_id}", ctype, uname
        disp = title or (f"@{uname}" if uname else f"chat_{chat_id}")
        return disp, ctype, uname

    async def total_summary(
        self, since_ts: int, chat_id: int | None = None,
    ) -> dict:
        """指定时间窗的总览（可选限定 chat）。"""
        params: list = [since_ts]
        where = "ts >= ?"
        if chat_id is not None:
            where += " AND chat_id = ?"
            params.append(chat_id)
        async with self.db.conn.execute(
            f"""
            SELECT
                COUNT(*),
                SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END),
                COALESCE(SUM(gp_cost), 0),
                COALESCE(SUM(bytes_in), 0),
                COALESCE(SUM(bytes_out), 0)
            FROM usage_log
            WHERE {where}
            """,
            params,
        ) as cur:
            row = await cur.fetchone()
        total, ok, failed, cancelled, gp, bin_, bout = row or (0,0,0,0,0,0,0)
        return {
            "total": total or 0,
            "ok": ok or 0,
            "failed": failed or 0,
            "cancelled": cancelled or 0,
            "gp_cost": gp or 0,
            "bytes_in": bin_ or 0,
            "bytes_out": bout or 0,
        }

    async def per_user_summary(
        self, since_ts: int, chat_id: int | None = None, limit: int = 10,
    ) -> list[UserSummary]:
        """按用户聚合，按任务数倒序。"""
        params: list = [since_ts]
        where = "u_log.ts >= ?"
        if chat_id is not None:
            where += " AND u_log.chat_id = ?"
            params.append(chat_id)
        params.append(limit)
        async with self.db.conn.execute(
            f"""
            SELECT
                u_log.user_id,
                COALESCE(u.first_name, ''),
                COALESCE(u.last_name, ''),
                u.username,
                COUNT(*),
                COALESCE(SUM(u_log.gp_cost), 0),
                COALESCE(SUM(u_log.bytes_in), 0),
                COALESCE(SUM(u_log.bytes_out), 0)
            FROM usage_log u_log
            LEFT JOIN users u ON u.user_id = u_log.user_id
            WHERE {where}
            GROUP BY u_log.user_id
            ORDER BY COUNT(*) DESC
            LIMIT ?
            """,
            params,
        ) as cur:
            rows = await cur.fetchall()
        out: list[UserSummary] = []
        for uid, first, last, uname, tasks, gp, bin_, bout in rows:
            display = " ".join(p for p in (first, last) if p) or uname or f"user_{uid}"
            out.append(UserSummary(
                user_id=uid, display=display, username=uname,
                tasks=tasks, gp_cost=gp, bytes_in=bin_, bytes_out=bout,
            ))
        return out

    async def per_chat_summary(
        self,
        since_ts: int,
        *,
        limit: int = 10,
        exclude_private: bool = False,
    ) -> list[ChatSummary]:
        """按 chat 聚合，按任务数倒序。chat_id 为 NULL（理论上不该有）的行被跳过。
        exclude_private=True 时过滤掉私聊行（chats.type='private'）—— 用在 /stats
        默认总览的"按群组排行"里，让群组维度更醒目。"""
        params: list = [since_ts]
        where = "u_log.ts >= ? AND u_log.chat_id IS NOT NULL"
        if exclude_private:
            # COALESCE 让没 upsert 过的 chat（type=NULL）默认按非私聊算，避免数据丢失
            where += " AND COALESCE(c.type, 'group') != 'private'"
        params.append(limit)
        async with self.db.conn.execute(
            f"""
            SELECT
                u_log.chat_id,
                COALESCE(c.type, ''),
                c.title,
                c.username,
                COUNT(*),
                COALESCE(SUM(u_log.gp_cost), 0),
                COALESCE(SUM(u_log.bytes_in), 0),
                COALESCE(SUM(u_log.bytes_out), 0)
            FROM usage_log u_log
            LEFT JOIN chats c ON c.chat_id = u_log.chat_id
            WHERE {where}
            GROUP BY u_log.chat_id
            ORDER BY COUNT(*) DESC
            LIMIT ?
            """,
            params,
        ) as cur:
            rows = await cur.fetchall()
        out: list[ChatSummary] = []
        for cid, ctype, title, uname, tasks, gp, bin_, bout in rows:
            if ctype == "private":
                # 私聊用 users 表回填昵称，比纯 user_<id> 友好
                try:
                    user_disp, user_uname = await self.get_user_display(cid)
                    display = f"私聊 · {user_disp}"
                    uname = uname or user_uname
                except Exception:
                    display = f"私聊 · user_{cid}"
            else:
                display = title or (f"@{uname}" if uname else f"chat_{cid}")
            out.append(ChatSummary(
                chat_id=cid, type=ctype or "", display=display,
                title=title, username=uname,
                tasks=tasks, gp_cost=gp, bytes_in=bin_, bytes_out=bout,
            ))
        return out

    async def user_summary(
        self, user_id: int, since_ts: int,
    ) -> dict:
        """单个用户的详细聚合。"""
        async with self.db.conn.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END),
                COALESCE(SUM(gp_cost), 0),
                COALESCE(SUM(bytes_in), 0),
                COALESCE(SUM(bytes_out), 0)
            FROM usage_log
            WHERE user_id = ? AND ts >= ?
            """,
            (user_id, since_ts),
        ) as cur:
            row = await cur.fetchone()
        total, ok, failed, cancelled, gp, bin_, bout = row or (0,0,0,0,0,0,0)
        return {
            "total": total or 0,
            "ok": ok or 0,
            "failed": failed or 0,
            "cancelled": cancelled or 0,
            "gp_cost": gp or 0,
            "bytes_in": bin_ or 0,
            "bytes_out": bout or 0,
        }

    async def kind_breakdown(
        self, since_ts: int,
        user_id: int | None = None, chat_id: int | None = None,
    ) -> list[tuple[str, int, int]]:
        """按 kind 分组，返回 [(kind, count, gp_cost), ...]。"""
        params: list = [since_ts]
        where = "ts >= ?"
        if user_id is not None:
            where += " AND user_id = ?"
            params.append(user_id)
        if chat_id is not None:
            where += " AND chat_id = ?"
            params.append(chat_id)
        async with self.db.conn.execute(
            f"""
            SELECT kind, COUNT(*), COALESCE(SUM(gp_cost), 0)
            FROM usage_log
            WHERE {where}
            GROUP BY kind
            ORDER BY COUNT(*) DESC
            """,
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [(k, c, g) for k, c, g in rows]


__all__ = [
    "UsageStore",
    "UserSummary",
    "ChatSummary",
    "KIND_PIXIV_TELEGRAPH",
    "KIND_PIXIV_DIRECT",
    "KIND_PIXIV_NOVEL",
    "KIND_EH_PAGE",
    "KIND_EH_ARCHIVE",
    "KIND_NHENTAI",
    "KIND_ARCHIVE_CMD",
    "KIND_ZIP2TPH",
    "KIND_ZH",
]
