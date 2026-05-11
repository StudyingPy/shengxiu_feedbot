"""Wikipedia 搜索：纯逻辑层，与 Telegram 无关。

调用 MediaWiki 搜索 API，复刻 Reference projects/bot-rs-master/src/funcs/command/wiki.rs
的行为：取 list=search 结果，清洗 snippet 里的 <span class="searchmatch"> 高亮。

Telegram 侧（slash command / inline）共用这个模块。未来要扩英文 wiki、moegirl
或换 API，只动这一处。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

# Wiki API 用 HTML 标签包裹命中关键词；展示前要去掉
_SEARCHMATCH_RE = re.compile(r'<span class="searchmatch">|</span>')

# MediaWiki 强制要求 User-Agent，否则 403。
# 格式遵循 https://meta.wikimedia.org/wiki/User-Agent_policy
_USER_AGENT = "shengxiu-feedbot/0.4 (+https://github.com/StudyingPy/shengxiu_feedbot)"


@dataclass(frozen=True)
class WikiHit:
    title: str
    snippet: str
    wordcount: int
    url: str


class WikipediaError(Exception):
    """搜索失败（网络错误、API 异常等）。无结果不算错误，返回空列表。"""


async def search_wikipedia(
    query: str,
    *,
    lang: str = "zh",
    limit: int = 5,
    timeout: float = 10.0,
) -> list[WikiHit]:
    """在指定语言的维基百科搜索词条。

    Args:
        query: 搜索关键词（已合并空格）。
        lang: 子域名，"zh" / "en" / "ja" 等。
        limit: 返回结果上限（API srlimit）。
        timeout: HTTP 超时（秒）。

    Returns:
        命中的 WikiHit 列表，按 API 默认相关度排序。无结果时返回空列表。

    Raises:
        WikipediaError: 网络或 API 异常。
    """
    query = query.strip()
    if not query:
        return []

    api_url = f"https://{lang}.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "list": "search",
        "format": "json",
        "srlimit": str(limit),
        "srsearch": query,
    }
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = await client.get(api_url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise WikipediaError(f"维基百科 API 请求失败：{e}") from e
    except ValueError as e:
        raise WikipediaError(f"维基百科返回非 JSON：{e}") from e

    try:
        results = data["query"]["search"]
    except (KeyError, TypeError) as e:
        raise WikipediaError(f"维基百科返回结构异常：{e}") from e

    hits: list[WikiHit] = []
    for item in results:
        title = item.get("title", "")
        snippet_raw = item.get("snippet", "")
        snippet = _SEARCHMATCH_RE.sub("", snippet_raw)
        hits.append(
            WikiHit(
                title=title,
                snippet=snippet,
                wordcount=int(item.get("wordcount", 0)),
                url=f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
            )
        )
    return hits


__all__ = ["WikiHit", "WikipediaError", "search_wikipedia"]
