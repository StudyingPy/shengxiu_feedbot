"""Inline mode handler。

当前实现：维基百科搜索（@bot <关键词>），仅 admin_users 可用。

设计：dispatcher 框架。先尝试匹配特殊路由（pixiv / 未来其他），都不匹配则
默认走 wiki 查询。pixiv 内联解析图片功能已知存在 bug 暂时禁用，相关代码以
注释形式保留在文末 `_DISABLED_PIXIV_INLINE`，未来修好后可恢复。

仅 admin_users 可用：
- 维基本身是公开 API，但 inline 在 bot 不在的群也能触发，限 admin 防滥用。
"""

from __future__ import annotations

import uuid

from telegram import (
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from html import escape as html_escape

from ...storage import AllowList
from ...utils import logger
from ...wikipedia import WikiHit, WikipediaError, search_wikipedia


async def handle_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    iq = update.inline_query
    if iq is None:
        return

    allowlist: AllowList = context.bot_data["allowlist"]
    if iq.from_user.id not in allowlist.admin_users:
        await iq.answer([], cache_time=10)
        return

    query = (iq.query or "").strip()
    if not query:
        await iq.answer([], cache_time=5)
        return

    # Dispatcher：未来恢复 pixiv inline 时，在这里加分支。
    # 当前默认全部走 wiki。
    await _inline_wiki(iq, query)


async def _inline_wiki(iq, query: str) -> None:
    try:
        hits = await search_wikipedia(query, lang="zh", limit=5)
    except WikipediaError as e:
        logger.warning(f"inline wiki '{query}': {e}")
        await iq.answer([], cache_time=5)
        return

    if not hits:
        # 返回一个"未找到"的 article 让用户看到反馈
        result = InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title=f"未找到 “{query}”",
            description="维基百科无相关词条",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ 维基百科未找到词条：{query}",
            ),
        )
        await iq.answer([result], cache_time=30, is_personal=True)
        return

    results = [_hit_to_article(hit) for hit in hits]
    await iq.answer(results, cache_time=300, is_personal=True)


def _hit_to_article(hit: WikiHit) -> InlineQueryResultArticle:
    # description 限制 ~80 字符比较好看
    desc = hit.snippet[:80] + ("..." if len(hit.snippet) > 80 else "")
    body = (
        f"🔍 <b>{html_escape(hit.title)}</b>\n"
        f"{html_escape(hit.url)}\n\n"
        f"{html_escape(hit.snippet)}\n\n"
        f"<i>总词数：{hit.wordcount}</i>"
    )
    return InlineQueryResultArticle(
        id=str(uuid.uuid4()),
        title=hit.title,
        description=desc or hit.url,
        input_message_content=InputTextMessageContent(
            message_text=body,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        ),
    )


# ---------------------------------------------------------------------------
# 已禁用：Pixiv 内联图片解析
# ---------------------------------------------------------------------------
#
# 旧实现存在 bug（图片代理路径在某些情况下加载失败），暂时拿掉以让 inline
# 入口承载 wiki 查询。代码保留于此，待图片代理稳定后恢复——届时把 import
# 和 _inline_illust / _inline_novel 取消注释，再在 handle_inline 的
# dispatcher 处加一行 pixiv 路由优先匹配即可。
#
# 重启步骤：
# 1. 取消下面块的注释（包含 import）
# 2. 在 handle_inline 里 wiki 调用之前加：
#        from ...provider import ProviderRegistry
#        from ...provider.pixiv import PixivProvider
#        from ...provider.pixiv.url import parse_inline_query
#        registry: ProviderRegistry = context.bot_data["registry"]
#        config: Config = context.bot_data["config"]
#        pixiv = registry.find_by_name("pixiv")
#        ref = parse_inline_query(query)
#        if isinstance(pixiv, PixivProvider) and ref is not None:
#            try:
#                if ref.kind == "novel":
#                    await _inline_novel(iq, ref.id, config, pixiv)
#                else:
#                    await _inline_illust(iq, ref.id, config, pixiv)
#                return
#            except PixivAPIError as e:
#                logger.warning(f"inline pixiv ({ref.kind}/{ref.id}): {e}")
#                # fallthrough to wiki
#            except Exception:
#                logger.exception(f"inline pixiv failed for {ref}")
#                # fallthrough to wiki
#
# from telegram import InlineQueryResultPhoto
# from ...config import Config
# from ...provider.pixiv import PixivAPIError, PixivProvider
#
# async def _inline_illust(iq, pid: str, config: Config, provider: PixivProvider) -> None:
#     """通过 Nginx 反代 i.pximg.net 让 TG 能抓到图。"""
#     work = await provider.fetch_illust(pid)
#
#     def _proxy(url: str) -> str:
#         if "i.pximg.net/" in url:
#             return url.replace("https://i.pximg.net/", f"{provider.public_base_url}/pximg/", 1)
#         return url
#
#     if not work.images:
#         await iq.answer([], cache_time=5)
#         return
#
#     first = work.images[0]
#     caption_template = (
#         config.templates.illust.inline_single_caption
#         if work.page_count == 1
#         else config.templates.illust.inline_multi_caption
#     )
#     try:
#         caption = caption_template.format(**work.template_vars())
#     except (KeyError, IndexError, ValueError):
#         caption = caption_template
#     if len(caption) > 1024:
#         caption = caption[:1020] + "..."
#
#     title_for_card = work.title if work.page_count == 1 else f"{work.title} (共 {work.page_count} 张)"
#     result = InlineQueryResultPhoto(
#         id=str(uuid.uuid4()),
#         photo_url=_proxy(first.regular or first.original),
#         thumbnail_url=_proxy(first.thumb or first.small or first.regular),
#         title=title_for_card,
#         description=f"by {work.author}",
#         caption=caption,
#         parse_mode=ParseMode.HTML,
#     )
#     await iq.answer([result], cache_time=300, is_personal=True)
#
#
# async def _inline_novel(iq, nid: str, config: Config, provider: PixivProvider) -> None:
#     novel = await provider.fetch_novel(nid)
#     title_t = config.templates.novel.inline_article_title or "{title} - {author}"
#     desc_t = config.templates.novel.inline_article_description or "{caption_short}"
#     try:
#         title = title_t.format(**novel.template_vars())
#         desc = desc_t.format(**novel.template_vars())
#     except (KeyError, IndexError, ValueError):
#         title, desc = novel.title, novel.description[:80]
#
#     pixiv_url = f"https://www.pixiv.net/novel/show.php?id={nid}"
#     result = InlineQueryResultArticle(
#         id=str(uuid.uuid4()),
#         title=title,
#         description=desc,
#         input_message_content=InputTextMessageContent(
#             message_text=f"{title}\n{pixiv_url}",
#         ),
#     )
#     await iq.answer([result], cache_time=300, is_personal=True)


__all__ = ["handle_inline"]
