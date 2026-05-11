"""从用户消息中提取 Pixiv 作品 ID。

支持的输入格式：
- https://www.pixiv.net/artworks/12345
- https://www.pixiv.net/en/artworks/12345
- https://www.pixiv.net/i/12345              （旧格式）
- https://www.pixiv.net/member_illust.php?illust_id=12345&mode=...
- https://www.pixiv.net/novel/show.php?id=12345
- 纯数字 12345                                （inline mode 默认按 illust 处理）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

WorkKind = Literal["illust", "novel"]


@dataclass
class _PixivRef:
    """pixiv 内部 ref。channel 拿到后会包成顶层 ParsedRef。"""

    kind: WorkKind
    id: str
    raw: str


# 排序：更具体的先匹配。novel 必须在 illust 之前判断。
_PATTERNS: list[tuple[WorkKind, re.Pattern[str]]] = [
    ("novel", re.compile(r"pixiv\.net/(?:[a-z]{2}/)?novel/show\.php\?[^\s]*\bid=(\d+)", re.IGNORECASE)),
    ("novel", re.compile(r"pixiv\.net/(?:[a-z]{2}/)?n/(\d+)", re.IGNORECASE)),
    ("illust", re.compile(r"pixiv\.net/(?:[a-z]{2}/)?artworks/(\d+)", re.IGNORECASE)),
    ("illust", re.compile(r"pixiv\.net/(?:[a-z]{2}/)?i/(\d+)", re.IGNORECASE)),
    (
        "illust",
        re.compile(
            r"pixiv\.net/(?:[a-z]{2}/)?member_illust\.php\?[^\s]*\billust_id=(\d+)",
            re.IGNORECASE,
        ),
    ),
]


def extract_pixiv_refs(text: str) -> list[_PixivRef]:
    """从一段文本里提取所有 Pixiv 作品引用，按出现顺序去重。"""
    if not text:
        return []
    seen: set[tuple[WorkKind, str]] = set()
    refs: list[_PixivRef] = []
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            key = (kind, m.group(1))
            if key in seen:
                continue
            seen.add(key)
            refs.append(_PixivRef(kind=kind, id=m.group(1), raw=m.group(0)))
    refs.sort(key=lambda r: text.find(r.raw))
    return refs


def parse_inline_query(query: str) -> _PixivRef | None:
    """解析 inline mode 输入。

    `@bot 12345`               → illust 12345
    `@bot artworks/12345`      → illust 12345
    `@bot novel/12345`         → novel 12345
    `@bot https://...`         → 走完整 URL 提取
    """
    q = (query or "").strip()
    if not q:
        return None

    # 完整 URL
    refs = extract_pixiv_refs(q)
    if refs:
        return refs[0]

    # `novel/12345` 简写
    m = re.match(r"^novel/(\d+)$", q, re.IGNORECASE)
    if m:
        return _PixivRef(kind="novel", id=m.group(1), raw=q)

    # `artworks/12345` 简写或纯数字
    m = re.match(r"^(?:artworks?/)?(\d+)$", q, re.IGNORECASE)
    if m:
        return _PixivRef(kind="illust", id=m.group(1), raw=q)

    return None


__all__ = ["WorkKind", "extract_pixiv_refs", "parse_inline_query"]
