"""Pixiv Novel 专属发布器。

通用 TelegraphPublisher 只处理图集型作品（GalleryWork）。Novel 排版需要：
- 处理 [chapter:] [newpage] [[jumpuri:]] [pixivimage:] [uploadedimage:] 等 Pixiv 自定义标记
- 下载封面、嵌入图、被引用的另一份 illust 的首图
- 把这些图喂给本地 cache_dir + base_url，再插回正文

这部分逻辑只对 pixiv 有意义，所以留在 pixiv 包内，独立于 publisher.telegraph。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...config import Config
from ...publisher._resolver import ResolveItem, resolve_image_urls
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


# Telegra.ph 官方 API 写的是 content JSON 上限 64 KB。纯中文 UTF-8 是 3 字节/字符，
# 加上每段 <p>...</p> 节点的 JSON 包装开销（"tag"/"children" 字段约 25-50 字节/段），
# 14000 字符对应 ~51 KB 实际字节，留约 9 KB（15%）给章节切割不均、嵌入图、header/footer
# 等 overhead。15000+ 会撞 60 KB 上限。
NOVEL_TEXT_SOFT_LIMIT = 14_000

# 字节预检阈值：粗切后逐 chunk 实测 JSON 字节，超过此值则二分。
# 留 5 KB 给请求里 title/access_token/return_content 等其它字段。
TELEGRAPH_CONTENT_BYTE_LIMIT = 60_000


async def publish_novel(
    config: Config,
    publisher: TelegraphPublisher,
    provider,  # PixivProvider，避免循环 import
    nid: str,
    *,
    progress=None,  # channel.telegram.progress.Progress 或 None
    force_r2: bool = False,
) -> tuple[NovelWork, PublishResult]:
    """端到端：拉 novel + 下载封面与嵌入图 + 发布。

    force_r2=True 绕过 R2 size_guard（admin --r2 flag），与 publish_gallery 一致。
    """
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

        # 收集所有需要解析为公网 URL 的图片。order matters：
        # idx 0 = 封面（如果有），后续 = textEmbeddedImages，再之后 = pixivimage 引用。
        resolve_items: list[ResolveItem] = []
        cover_idx: int | None = None
        embed_idx_by_img_id: dict[str, int] = {}
        pixivimage_idx_by_id: dict[str, int] = {}

        def _push_item(local_path: Path) -> int:
            try:
                rel = local_path.resolve().relative_to(Path(provider.cache_dir).resolve())
                key = rel.as_posix()
            except (ValueError, AttributeError):
                # 不在 cache_dir 下 → helper 会把 r2_key="" 当上传失败走 fallback
                key = ""
            fallback = relative_url(provider.public_base_url, provider.cache_dir, local_path)
            resolve_items.append(ResolveItem(
                r2_key=key, local_path=local_path, fallback_url=fallback,
            ))
            return len(resolve_items) - 1

        # 1. 封面
        if novel.cover_url:
            await _status("⏳ 下载封面...")
            cover_path = await downloader.download_novel_cover(nid, novel.cover_url)
            cover_idx = _push_item(cover_path)

        # 2. textEmbeddedImages 嵌入图
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
            embed_idx_by_img_id[img_id] = _push_item(local_path)
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
                pixivimage_idx_by_id[illust_id] = _push_item(downloaded[0].original_path)
            except Exception as e:
                logger.warning(f"failed to embed pixivimage:{illust_id} in novel {nid}: {e}")
            done += 1
            await _update(f"⏳ 下载引用插画 {done}/{total_px}")

    # 4. 统一把所有图片上传 R2 / 决策 fallback URL（与 telegraph publisher 共享同一份决策逻辑）
    r2_client = getattr(publisher, "r2_client", None)
    r2_enabled = config.storage.r2.enabled
    size_guard_bytes = int(config.storage.r2.max_upload_size_gb * 1024 ** 3)
    if resolve_items:
        await _status(f"⏳ 上传图片到 R2 (共 {len(resolve_items)})")
    resolved = await resolve_image_urls(
        r2_client, resolve_items,
        r2_enabled=r2_enabled,
        force_r2=force_r2,
        size_guard_bytes=size_guard_bytes,
        on_status=lambda t: _status(t),
    )

    # 回填 cover_url + embedded_public 使用 helper 返回的最终 URL
    if cover_idx is not None:
        novel.cover_url = resolved.urls[cover_idx]
    embedded_public: dict[str, dict] = {}
    for img_id, idx in embed_idx_by_img_id.items():
        embedded_public[img_id] = {"url": resolved.urls[idx]}
    for illust_id, idx in pixivimage_idx_by_id.items():
        embedded_public[f"pixivimage_{illust_id}"] = {"url": resolved.urls[idx]}

    # 5. 渲染 + 发布
    await _status("⏳ 发布到 Telegra.ph...")
    await publisher.ensure_account()
    tvars = novel.template_vars()

    title = render_template(templates.page_title, tvars) or novel.title or "(untitled)"
    if len(title) > 256:
        title = title[:253] + "..."

    content_chunks = _split_novel_text(novel.content)
    # 字节预检 + 二分：粗切按字符数，二次校验按真实 JSON 字节数。
    # 防御极端段落分布（章节超长、嵌入图密集等）撞 64KB 上限。
    content_chunks = _ensure_byte_safe_chunks(
        content_chunks,
        novel=novel,
        embedded_public=embedded_public,
        header_template=templates.page_header,
        footer_template=templates.page_footer,
        tvars=tvars,
    )

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
        urls=page_urls,
        page_count=len(page_urls),
        image_count=len(resolve_items),
        r2_image_count=resolved.r2_ok_count,
        fallback_image_count=resolved.fallback_count,
        fallback_reason=resolved.fallback_reason,
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


def _find_split_point(text: str) -> int:
    """在文本接近中点处找一个"自然"切分位（换行/句号 > 标点 > 空格）。

    返回切分后**右半起点**的索引。在中点 ±1/6 长度的窗口里搜索，找不到就回退到中点。
    """
    if len(text) < 2:
        return len(text) // 2
    target = len(text) // 2
    span = max(200, len(text) // 6)
    lo, hi = max(0, target - span), min(len(text), target + span)
    for sep in ("\n\n", "[newpage]", "\n", "。", "！", "？", ".", "!", "?", " "):
        idx = text.rfind(sep, lo, hi)
        if idx > 0:
            return idx + len(sep)
    return target


def _ensure_byte_safe_chunks(
    chunks: list[str],
    *,
    novel: NovelWork,
    embedded_public: dict[str, dict],
    header_template: str,
    footer_template: str,
    tvars: dict[str, Any],
    byte_limit: int = TELEGRAPH_CONTENT_BYTE_LIMIT,
) -> list[str]:
    """对每个 chunk 真实构建 nodes 后测 JSON 字节，超限就二分递归。

    用 chunk_index=0 + 非空 next_url 来构造"最坏 overhead"的测试节点
    （首页含原作品链接、封面、header、footer，再加下一页链接），
    确保后续真实发布时不会再超限。
    """
    safe: list[str] = []
    pending: list[str] = list(chunks)
    # 兜底：每个原始 chunk 最多二分 6 层 = 64 段，足够撑 ~1MB 单段超长文本
    budget = max(1, len(chunks)) * 64

    while pending and budget > 0:
        budget -= 1
        ch = pending.pop(0)
        if len(ch) < 500:
            # 文本太短就别再切了，否则会切到字面没意义
            safe.append(ch)
            continue
        test_nodes = _build_novel_page_nodes(
            novel=novel,
            embedded_public=embedded_public,
            header_template=header_template,
            footer_template=footer_template,
            tvars=tvars,
            content_chunk=ch,
            chunk_index=0,
            total_chunks=2,
            next_url="https://telegra.ph/placeholder-for-size-estimate",
        )
        size = len(json.dumps(test_nodes, ensure_ascii=False).encode("utf-8"))
        if size <= byte_limit:
            safe.append(ch)
            continue
        mid = _find_split_point(ch)
        if mid <= 0 or mid >= len(ch):
            safe.append(ch)
            continue
        left, right = ch[:mid].rstrip(), ch[mid:].lstrip()
        if not left or not right:
            safe.append(ch)
            continue
        logger.info(
            f"novel chunk over byte limit ({size} > {byte_limit}); bisecting "
            f"at char {mid}/{len(ch)}"
        )
        pending.insert(0, right)
        pending.insert(0, left)

    if pending:
        # budget 用尽（极端单段巨长文本），剩下的直接放过，让 Telegra.ph 自己报错
        logger.warning(f"_ensure_byte_safe_chunks budget exhausted, {len(pending)} chunk(s) left")
        safe.extend(pending)
    return safe


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
