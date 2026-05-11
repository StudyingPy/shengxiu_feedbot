"""Pixiv Novel 专属发布器。

通用 TelegraphPublisher 只处理图集型作品（GalleryWork）。Novel 排版需要：
- 处理 [chapter:] [newpage] [[jumpuri:]] [pixivimage:] [uploadedimage:] 等 Pixiv 自定义标记
- 下载封面、嵌入图、被引用的另一份 illust 的首图
- 把这些图喂给本地 cache_dir + base_url，再插回正文

这部分逻辑只对 pixiv 有意义，所以留在 pixiv 包内，独立于 publisher.telegraph。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...config import Config
from ...publisher.telegraph import (
    NodeTree,
    PublishResult,
    TelegraphPublisher,
    html_to_nodes_safe,
    render_template,
)
from ...utils import logger
from .api import PixivAPI
from .downloader import PixivDownloader, relative_url
from .model import NovelWork
from .parser import parse_novel_meta


@dataclass
class _NovelEmbedded:
    """已下载好的小说嵌入图。key 是模板里 [uploadedimage:N] 或 [pixivimage:N] 对应的 key。"""

    key: str
    url: str


# Telegra.ph 官方 API 写的是 content JSON 上限 64 KB。纯文本转成 node tree
# 后还有标签、链接、图片节点等 JSON 开销，所以这里用偏保守的正文字符软上限，
# 超过后拆成多篇 Telegra.ph 并在每篇末尾放“下一页”。
NOVEL_TEXT_SOFT_LIMIT = 18_000


async def publish_novel(
    config: Config,
    publisher: TelegraphPublisher,
    provider,  # PixivProvider，避免循环 import
    nid: str,
    *,
    progress=None,  # channel.telegram.progress.Progress 或 None
) -> tuple[NovelWork, PublishResult]:
    """端到端：拉 novel + 下载封面与嵌入图 + 发布。"""
    templates = config.templates.novel

    async def _status(text: str) -> None:
        if progress is not None:
            await progress.status(text)

    async def _update(text: str) -> None:
        if progress is not None:
            await progress.update(text)

    async with PixivAPI(provider.phpsessid, provider.timeout) as api:
        await _status("⏳ 拉取小说元数据...")
        body = await api.fetch_novel(nid)
        novel = parse_novel_meta(body)

        downloader = PixivDownloader(api, provider.cache_dir, provider.concurrency)

        # 1. 封面
        if novel.cover_url:
            await _status("⏳ 下载封面...")
            cover_path = await downloader.download_novel_cover(nid, novel.cover_url)
            novel.cover_url = relative_url(provider.public_base_url, provider.cache_dir, cover_path)

        # 2. textEmbeddedImages 嵌入图
        embedded_public: dict[str, dict] = {}
        raw_embedded = body.get("textEmbeddedImages") or {}
        total_embed = len(raw_embedded)
        if total_embed:
            await _status(f"⏳ 下载嵌入图 0/{total_embed}")
        done = 0
        for img_id, info in raw_embedded.items():
            urls = info.get("urls") or {}
            url = urls.get("original") or urls.get("1200x1200")
            if not url:
                done += 1
                continue
            local_path = await downloader.download_novel_embed(nid, img_id, url)
            embedded_public[img_id] = {
                "url": relative_url(provider.public_base_url, provider.cache_dir, local_path)
            }
            done += 1
            await _update(f"⏳ 下载嵌入图 {done}/{total_embed}")

        # 3. [pixivimage:illust_id] 标记 → 下载对应 illust 的首图
        pixivimage_ids = set(re.findall(r"\[pixivimage:(\d+)\]", novel.content))
        total_px = len(pixivimage_ids)
        if total_px:
            await _status(f"⏳ 下载引用插画 0/{total_px}")
        done = 0
        for illust_id in pixivimage_ids:
            try:
                illust_body = await api.fetch_illust(illust_id)
                page_count = int(illust_body.get("pageCount") or 1)
                urls = illust_body.get("urls") or {}
                if page_count > 1 or not urls.get("original"):
                    pages = await api.fetch_illust_pages(illust_id)
                    img_url = pages[0]["urls"]["original"]
                else:
                    img_url = urls["original"]
                downloaded = await downloader.download_illust(illust_id, [img_url])
                public = relative_url(
                    provider.public_base_url, provider.cache_dir, downloaded[0].original_path
                )
                embedded_public[f"pixivimage_{illust_id}"] = {"url": public}
            except Exception as e:
                logger.warning(f"failed to embed pixivimage:{illust_id} in novel {nid}: {e}")
            done += 1
            await _update(f"⏳ 下载引用插画 {done}/{total_px}")

    # 4. 渲染 + 发布
    await _status("⏳ 发布到 Telegra.ph...")
    await publisher.ensure_account()
    tvars = novel.template_vars()

    title = render_template(templates.page_title, tvars) or novel.title or "(untitled)"
    if len(title) > 256:
        title = title[:253] + "..."

    content_chunks = _split_novel_text(novel.content)

    # 从最后一页往前发，才能在前一页末尾写入下一页 URL；对外仍只返回第一页。
    page_urls: list[str] = []
    next_url: str | None = None
    for i in range(len(content_chunks) - 1, -1, -1):
        nodes = _build_novel_page_nodes(
            novel=novel,
            embedded_public=embedded_public,
            header_template=templates.page_header,
            footer_template=templates.page_footer,
            tvars=tvars,
            content_chunk=content_chunks[i],
            chunk_index=i,
            total_chunks=len(content_chunks),
            next_url=next_url,
        )

        page_title = title if i == 0 else f"{title} ({i + 1}/{len(content_chunks)})"
        if len(page_title) > 256:
            page_title = page_title[:253] + "..."

        page = await publisher.tg.create_page(title=page_title, content=nodes, return_content=False)
        page_urls.append(page["url"])
        next_url = page["url"]

    page_urls.reverse()
    logger.info(
        f"published novel [{nid}] across {len(page_urls)} page(s), primary={page_urls[0]}"
    )

    return novel, PublishResult(
        urls=page_urls, page_count=len(page_urls), image_count=len(embedded_public)
    )


def _split_novel_text(content: str, limit: int = NOVEL_TEXT_SOFT_LIMIT) -> list[str]:
    """按正文字符数把 Pixiv 小说拆成多段。

    优先在空行、[newpage]、自然句尾处切开，尽量避免把段落切碎。
    这里限制的是“原始正文字符数”，不是最终 JSON 字节数；这样改动小，
    也足以避开 Telegra.ph 对 content 的实际大小限制。
    """
    content = content or ""
    if len(content) <= limit:
        return [content]

    parts = re.split(r"(\[newpage\]|\n{2,})", content)
    blocks: list[str] = []
    buf = ""
    for part in parts:
        if not part:
            continue
        buf += part
        if re.fullmatch(r"\[newpage\]|\n{2,}", part):
            blocks.append(buf)
            buf = ""
    if buf:
        blocks.append(buf)

    chunks: list[str] = []
    cur = ""

    def flush_cur() -> None:
        nonlocal cur
        if cur.strip():
            chunks.append(cur.strip())
        cur = ""

    for block in blocks:
        if len(cur) + len(block) <= limit:
            cur += block
            continue

        flush_cur()
        cur = block

        # 单个段落过长时再硬切。优先找换行 / 中文句号 / 标点 / 空格。
        while len(cur) > limit:
            cut = -1
            for sep in ("\n", "。", "！", "？", ".", "!", "?", " "):
                cut = max(cut, cur.rfind(sep, 0, limit))
            if cut < int(limit * 0.5):
                cut = limit - 1
            chunks.append(cur[: cut + 1].strip())
            cur = cur[cut + 1 :].lstrip()

    flush_cur()
    return chunks or [content]


def _build_novel_page_nodes(
    *,
    novel: NovelWork,
    embedded_public: dict[str, dict],
    header_template: str,
    footer_template: str,
    tvars: dict[str, Any],
    content_chunk: str,
    chunk_index: int,
    total_chunks: int,
    next_url: str | None,
) -> NodeTree:
    nodes: NodeTree = []

    if chunk_index == 0:
        # 原作品链接置于篇首（独立于用户模板）
        source_url = f"https://www.pixiv.net/novel/show.php?id={novel.nid}"
        nodes.append(
            {
                "tag": "p",
                "children": [
                    "原作品：",
                    {"tag": "a", "attrs": {"href": source_url}, "children": [source_url]},
                ],
            }
        )
        if novel.cover_url:
            nodes.append(
                {"tag": "figure", "children": [{"tag": "img", "attrs": {"src": novel.cover_url}}]}
            )
        nodes.extend(html_to_nodes_safe(render_template(header_template, tvars)))
    else:
        nodes.append({"tag": "p", "children": [f"（续 {chunk_index + 1} / {total_chunks}）"]})

    nodes.extend(_render_novel_content(content_chunk, embedded_public))

    if chunk_index == 0:
        nodes.extend(html_to_nodes_safe(render_template(footer_template, tvars)))

    if next_url:
        nodes.append(
            {
                "tag": "p",
                "children": [
                    {"tag": "a", "attrs": {"href": next_url}, "children": ["下一页 →"]}
                ],
            }
        )

    return nodes


def _render_novel_content(content: str, embedded_images: dict[str, dict]) -> NodeTree:
    """把小说正文从 Pixiv 标记 + 纯文本，转成 Telegra.ph node tree。

    Pixiv 自定义标记：
        [newpage]               换页（这里用 <hr> 表示）
        [chapter:标题]          章节标题（h3）
        [[jumpuri:文字>URL]]    超链接
        [pixivimage:illust_id]  插入另一个 Pixiv 作品的图
        [uploadedimage:img_id]  插入小说自带的图
    """
    # 替换 [chapter:xxx] 为 <h3>
    text = re.sub(
        r"\[chapter:([^\]\n]+)\]",
        lambda m: f"</p><h3>{m.group(1)}</h3><p>",
        content,
    )

    # 替换 [newpage]
    text = re.sub(r"\[newpage\]", "</p><hr><p>", text)

    # 替换 [[jumpuri:文字>URL]]
    def _jumpuri_repl(m: re.Match) -> str:
        return f'<a href="{m.group(2)}">{m.group(1)}</a>'

    text = re.sub(r"\[\[jumpuri:([^>]+)>([^\]]+)\]\]", _jumpuri_repl, text)

    # 替换 [uploadedimage:xxx] 和 [pixivimage:xxx]
    def _image_repl(m: re.Match) -> str:
        kind = m.group(1)
        img_id = m.group(2)
        key = img_id if kind == "uploadedimage" else f"pixivimage_{img_id}"
        entry = embedded_images.get(key)
        if not entry or not entry.get("url"):
            return f"</p><p>[未能加载图片 {kind}:{img_id}]</p><p>"
        return f'</p><figure><img src="{entry["url"]}"/></figure><p>'

    text = re.sub(r"\[(uploadedimage|pixivimage):(\d+)\]", _image_repl, text)

    # 段落处理
    paragraphs = re.split(r"\n{2,}", text)
    wrapped = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        p = p.replace("\n", "<br/>")
        wrapped.append(f"<p>{p}</p>")

    full_html = "".join(wrapped)
    full_html = re.sub(r"<p>\s*</p>", "", full_html)
    full_html = re.sub(r"<p>(<h3>.*?</h3>)</p>", r"\1", full_html)
    full_html = re.sub(r"<p>(<hr/?>)</p>", r"\1", full_html)
    full_html = re.sub(r"<p>(<figure>.*?</figure>)</p>", r"\1", full_html)
    full_html = re.sub(r"(<h3>.*?</h3>)<p><br/>", r"\1<p>", full_html)
    full_html = re.sub(r"(<hr/?>)<p><br/>", r"\1<p>", full_html)
    full_html = re.sub(r"(</figure>)<p><br/>", r"\1<p>", full_html)
    full_html = re.sub(r"<br/>\s*</p>", "</p>", full_html)

    return html_to_nodes_safe(full_html)


__all__ = ["publish_novel"]
