"""AJAX JSON → dataclass 解析。

Pixiv 字段命名混乱（illustTitle / userName / pageCount 等驼峰，与一些下划线字段并存），
且字段层级深、可空字段多。集中在这里做一次映射，下游只面对干净的 dataclass。
"""

from __future__ import annotations

import re
from typing import Any

from .model import IllustImageUrls, IllustWork, NovelWork

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _safe_str(v: Any, default: str = "") -> str:
    return str(v) if v is not None else default


def _html_to_text(html: str) -> str:
    """非常宽松的 HTML → 纯文本转换。

    Pixiv 的 description 含简单标签（<br>、<a>、<strong>），用正则清理足够。
    不引入 BeautifulSoup 是为了避免在 caption 这种轻量场景上启动重型解析器。
    """
    if not html:
        return ""
    # <br> 系列保留为换行
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    # 段落分隔
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    # 其他标签直接抹掉
    text = _HTML_TAG_RE.sub("", text)
    # HTML 实体的最常见几个
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    # 收紧多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_illust_meta(body: dict[str, Any], pages: list[dict[str, Any]] | None = None) -> IllustWork:
    """从 /ajax/illust/{pid} 响应（+ 可选的 /pages 响应）构造 IllustWork。

    单图作品 pages 可省略，首图 URL 会从 body['urls'] 取。
    多图作品必须传 pages，否则只能得到第一张图的 URL。
    """
    pid = _safe_str(body.get("id") or body.get("illustId"))
    title = _safe_str(body.get("illustTitle") or body.get("title"))
    description_html = _safe_str(body.get("description") or body.get("illustComment"))
    description = _html_to_text(description_html)
    user_id = _safe_str(body.get("userId"))
    author = _safe_str(body.get("userName"))
    create_date = _safe_str(body.get("createDate") or body.get("uploadDate"))
    page_count = _safe_int(body.get("pageCount"), 1)
    bookmark_count = _safe_int(body.get("bookmarkCount"))
    like_count = _safe_int(body.get("likeCount"))
    view_count = _safe_int(body.get("viewCount"))
    x_restrict = _safe_int(body.get("xRestrict"))
    ai_type = _safe_int(body.get("aiType"))
    illust_type = _safe_int(body.get("illustType"))

    # tags 在 body['tags']['tags'][i]['tag']，可能还有 ['translation']['en']
    tags: list[str] = []
    raw_tags = body.get("tags") or {}
    for t in raw_tags.get("tags") or []:
        tag = t.get("tag")
        if tag:
            tags.append(str(tag))

    # 图片 URL
    images: list[IllustImageUrls] = []
    if pages:
        for p in pages:
            urls = p.get("urls") or {}
            images.append(
                IllustImageUrls(
                    original=_safe_str(urls.get("original")),
                    regular=_safe_str(urls.get("regular")),
                    small=_safe_str(urls.get("small")),
                    thumb=_safe_str(urls.get("thumb_mini") or urls.get("thumb")),
                )
            )
    else:
        # 单图作品的 fallback：从 body['urls'] 拿
        urls = body.get("urls") or {}
        images.append(
            IllustImageUrls(
                original=_safe_str(urls.get("original")),
                regular=_safe_str(urls.get("regular")),
                small=_safe_str(urls.get("small")),
                thumb=_safe_str(urls.get("thumb")),
            )
        )

    return IllustWork(
        pid=pid,
        title=title,
        author=author,
        user_id=user_id,
        description=description,
        create_date=create_date,
        tags=tags,
        page_count=page_count,
        bookmark_count=bookmark_count,
        like_count=like_count,
        view_count=view_count,
        x_restrict=x_restrict,
        ai_type=ai_type,
        illust_type=illust_type,
        images=images,
    )


def parse_novel_meta(body: dict[str, Any]) -> NovelWork:
    """从 /ajax/novel/{nid} 响应构造 NovelWork。

    封面图 URL 在 body['coverUrl']，正文在 body['content'] 含 Pixiv 自定义标记。
    嵌入图片单独从 body['textEmbeddedImages'] 解析（在 publisher 阶段处理）。
    """
    nid = _safe_str(body.get("id"))
    title = _safe_str(body.get("title"))
    description_html = _safe_str(body.get("description"))
    description = _html_to_text(description_html)
    user_id = _safe_str(body.get("userId"))
    author = _safe_str(body.get("userName"))
    create_date = _safe_str(body.get("createDate") or body.get("uploadDate"))
    text_length = _safe_int(body.get("textCount") or body.get("characterCount"))
    cover_url = _safe_str(body.get("coverUrl"))
    content = _safe_str(body.get("content"))

    tags: list[str] = []
    raw_tags = body.get("tags") or {}
    for t in raw_tags.get("tags") or []:
        tag = t.get("tag")
        if tag:
            tags.append(str(tag))

    series_nav = body.get("seriesNavData") or {}
    series_id = _safe_str(series_nav.get("seriesId")) or None
    series_title = _safe_str(series_nav.get("title")) or None

    return NovelWork(
        nid=nid,
        title=title,
        author=author,
        user_id=user_id,
        description=description,
        create_date=create_date,
        tags=tags,
        text_length=text_length,
        cover_url=cover_url,
        content=content,
        series_id=series_id,
        series_title=series_title,
    )


__all__ = ["parse_illust_meta", "parse_novel_meta"]
