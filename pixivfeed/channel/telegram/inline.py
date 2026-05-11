"""Inline mode handler。

仅对 pixiv 提供 inline。eh/ex/nh 没有 ID 简写习惯，inline 体验不好——
真要发那些站点的链接走正常消息就行。

仅 admin_users 可用（其他人 query 返回空结果）。
- 单图作品 → InlineQueryResultPhoto，直接发图
- 多图作品 → 仍发图，caption 含原 Pixiv 链接

输入格式：`@bot 12345` / `@bot artworks/12345` / `@bot novel/12345` / 完整 URL
"""

from __future__ import annotations

import uuid

from telegram import (
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ...config import Config
from ...provider import ProviderRegistry
from ...provider.pixiv import (
    PixivAPIError,
    PixivProvider,
)
from ...provider.pixiv.url import parse_inline_query
from ...storage import AllowList
from ...utils import logger


async def handle_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    iq = update.inline_query
    if iq is None:
        return

    config: Config = context.bot_data["config"]
    registry: ProviderRegistry = context.bot_data["registry"]
    allowlist: AllowList = context.bot_data["allowlist"]

    if iq.from_user.id not in allowlist.admin_users:
        await iq.answer([], cache_time=10)
        return

    pixiv = registry.find_by_name("pixiv")
    if not isinstance(pixiv, PixivProvider):
        await iq.answer([], cache_time=10)
        return

    ref = parse_inline_query(iq.query)
    if ref is None:
        await iq.answer([], cache_time=5)
        return

    try:
        if ref.kind == "novel":
            await _inline_novel(iq, ref.id, config, pixiv)
        else:
            await _inline_illust(iq, ref.id, config, pixiv)
    except PixivAPIError as e:
        logger.warning(f"inline ({ref.kind}/{ref.id}): {e}")
        await iq.answer([], cache_time=5)
    except Exception:
        logger.exception(f"inline failed for {ref}")
        await iq.answer([], cache_time=5)


async def _inline_illust(iq, pid: str, config: Config, provider: PixivProvider) -> None:
    """通过 Nginx 反代 i.pximg.net 让 TG 能抓到图。"""
    work = await provider.fetch_illust(pid)

    def _proxy(url: str) -> str:
        if "i.pximg.net/" in url:
            return url.replace("https://i.pximg.net/", f"{provider.public_base_url}/pximg/", 1)
        return url

    if not work.images:
        await iq.answer([], cache_time=5)
        return

    first = work.images[0]
    caption_template = (
        config.templates.illust.inline_single_caption
        if work.page_count == 1
        else config.templates.illust.inline_multi_caption
    )
    try:
        caption = caption_template.format(**work.template_vars())
    except (KeyError, IndexError, ValueError):
        caption = caption_template
    if len(caption) > 1024:
        caption = caption[:1020] + "..."

    title_for_card = work.title if work.page_count == 1 else f"{work.title} (共 {work.page_count} 张)"
    result = InlineQueryResultPhoto(
        id=str(uuid.uuid4()),
        photo_url=_proxy(first.regular or first.original),
        thumbnail_url=_proxy(first.thumb or first.small or first.regular),
        title=title_for_card,
        description=f"by {work.author}",
        caption=caption,
        parse_mode=ParseMode.HTML,
    )
    await iq.answer([result], cache_time=300, is_personal=True)


async def _inline_novel(iq, nid: str, config: Config, provider: PixivProvider) -> None:
    novel = await provider.fetch_novel(nid)
    title_t = config.templates.novel.inline_article_title or "{title} - {author}"
    desc_t = config.templates.novel.inline_article_description or "{caption_short}"
    try:
        title = title_t.format(**novel.template_vars())
        desc = desc_t.format(**novel.template_vars())
    except (KeyError, IndexError, ValueError):
        title, desc = novel.title, novel.description[:80]

    pixiv_url = f"https://www.pixiv.net/novel/show.php?id={nid}"
    result = InlineQueryResultArticle(
        id=str(uuid.uuid4()),
        title=title,
        description=desc,
        input_message_content=InputTextMessageContent(
            message_text=f"{title}\n{pixiv_url}",
        ),
    )
    await iq.answer([result], cache_time=300, is_personal=True)


__all__ = ["handle_inline"]
