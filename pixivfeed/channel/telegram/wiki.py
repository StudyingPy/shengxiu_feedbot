"""/wiki 命令：在中文维基百科查词条。

仅作为 Telegram 适配层——网络/解析逻辑全在 [pixivfeed.wikipedia](../../wikipedia.py)
里，方便单测和未来在 inline 模式复用同一份逻辑。
"""

from __future__ import annotations

from html import escape as html_escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ...storage import AllowList
from ...utils import logger
from ...wikipedia import WikipediaError, search_wikipedia
from .auth import is_authorized


async def cmd_wiki(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/wiki <词条> —— 在中文维基百科查词条，回首条命中。"""
    allowlist: AllowList = context.bot_data["allowlist"]
    if not await is_authorized(update, allowlist):
        return

    msg = update.message
    if msg is None:
        return

    args = context.args or []
    if not args:
        await msg.reply_text("用法：/wiki <词条>\n例：/wiki 上海")
        return

    query = " ".join(args).strip()
    try:
        hits = await search_wikipedia(query, lang="zh", limit=1)
    except WikipediaError as e:
        logger.warning(f"/wiki '{query}' failed: {e}")
        await msg.reply_text("❌ 维基百科查询失败，稍后再试。")
        return

    if not hits:
        await msg.reply_text(
            f"❌ 未查找到词条 <code>{html_escape(query)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    hit = hits[0]
    text = (
        f"🔍 查找到词条\n\n"
        f"<b>标题</b>: {html_escape(hit.title)}\n"
        f"<b>链接</b>: {html_escape(hit.url)}\n"
        f"<b>概要</b>: {html_escape(hit.snippet)}\n"
        f"<b>总词数</b>: {hit.wordcount}"
    )
    await msg.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )


__all__ = ["cmd_wiki"]
