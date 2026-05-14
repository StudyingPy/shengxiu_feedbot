"""eh/ex 关键词搜索 —— scrape 列表页 HTML。

eh 没有官方搜索 API（api.e-hentai.org/api.php 只接 gid+token 数组返 metadata），
搜索只能 scrape `<table class="itg gltc">`。front page / search / popular / watched
共用同一套模板，Compact 视图 selector 最稳定。

调用方在 channel 层（handlers.py 的 cmd_ehsearch）；本模块不感知 Telegram。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

from ...utils import logger

# 这些类型在 __init__.py 里定义；本模块由 __init__.py re-export，
# 但 EHError 是搜索错误的父类，需要在这里 import。运行时无循环依赖，
# 因为本文件只在调用方主动 import 时被加载，那时 __init__.py 早就完成 import。


class EHSearchError(Exception):
    """搜索流程统一异常基类。

    不继承 EHError 是为了避免循环导入；在调用方（handlers.py）也只 catch
    EHSearchError 这一支，不混入 fetch/download 的 EHError 兜底逻辑。
    """


class EHSearchAuthError(EHSearchError):
    """ex cookie 失效 / 未登录被拒。调用方应回退到 e-hentai。"""


class EHSearchBlockedError(EHSearchError):
    """IP 被临时封禁 / ratelimit。"""


@dataclass
class SearchResultItem:
    gid: int
    token: str            # 10-char hex
    url: str              # https://<host>/g/<gid>/<token>/
    title: str
    category: str         # "Manga" / "Doujinshi" / ...
    tags: list[str] = field(default_factory=list)  # "language:english" / "f:big breasts"
    pages: int = 0
    uploader: str = ""
    posted_at: str = ""   # "2026-05-14 17:48"


@dataclass
class SearchResultPage:
    items: list[SearchResultItem]
    total_count: int
    next_url: str | None
    prev_url: str | None
    host: str             # "e-hentai.org" / "exhentai.org"
    keyword: str


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

_TOTAL_RE = re.compile(r"Found\s+([\d,]+)\s+results", re.IGNORECASE)
_GALLERY_URL_RE = re.compile(
    r"https?://(?:e-hentai|exhentai)\.org/g/(\d+)/([0-9a-fA-F]+)",
)
_PAGES_RE = re.compile(r"(\d+)\s*pages?", re.IGNORECASE)
_BANNED_RE = re.compile(r"temporarily banned", re.IGNORECASE)


def _parse_item(tr: Tag) -> SearchResultItem | None:
    """从一行 <tr> 抠出一条 gallery；解析失败返回 None。"""
    # 详情链接 + gid/token
    gl3c = tr.select_one("td.gl3c a[href]")
    if not gl3c:
        return None
    href = gl3c["href"]
    m = _GALLERY_URL_RE.match(href)
    if not m:
        return None
    gid = int(m.group(1))
    token = m.group(2)

    # 标题
    glink = tr.select_one("td.gl3c .glink")
    title = glink.get_text(strip=True) if glink else f"gallery-{gid}"

    # 分类
    cat_el = tr.select_one("td.gl1c .cn")
    category = cat_el.get_text(strip=True) if cat_el else ""

    # tags：每个 <div class="gt" title="namespace:value">value</div>
    tags: list[str] = []
    for gt in tr.select("td.gl3c .gt"):
        t = gt.get("title", "")
        if t:
            tags.append(t)

    # 上传者
    uploader_el = tr.select_one('td.gl4c a[href*="/uploader/"]')
    uploader = uploader_el.get_text(strip=True) if uploader_el else ""

    # 页数 —— td.gl4c 内含 2 个 div，第 2 个是 "N pages"
    pages = 0
    for d in tr.select("td.gl4c > div"):
        txt = d.get_text(strip=True)
        if pm := _PAGES_RE.search(txt):
            pages = int(pm.group(1))
            break

    # 发布时间 —— posted_<gid> 元素
    posted_at = ""
    posted_el = tr.select_one(f"#posted_{gid}")
    if posted_el:
        posted_at = posted_el.get_text(strip=True)

    return SearchResultItem(
        gid=gid,
        token=token,
        url=f"https://{_host_from_href(href)}/g/{gid}/{token}/",
        title=title,
        category=category,
        tags=tags,
        pages=pages,
        uploader=uploader,
        posted_at=posted_at,
    )


def _host_from_href(href: str) -> str:
    if href.startswith("https://exhentai.org") or href.startswith("http://exhentai.org"):
        return "exhentai.org"
    return "e-hentai.org"


def _parse_nav_url(soup: BeautifulSoup, anchor_id: str) -> str | None:
    """Next/Prev 按钮：禁用时变 <span>，启用时是 <a href>。"""
    el = soup.find(id=anchor_id)
    if el is None or el.name != "a":
        return None
    href = el.get("href")
    return href if href else None


def parse_search_page(html: str, host: str, keyword: str) -> SearchResultPage:
    """解析整个搜索/列表页。

    出错时不 raise，尽可能返回部分结果；只有完全没识别到 itg 表才返回空列表。
    """
    soup = BeautifulSoup(html, "html.parser")

    # 总数
    total = 0
    if (p := soup.select_one("div.searchtext p")) and (m := _TOTAL_RE.search(p.get_text())):
        total = int(m.group(1).replace(",", ""))

    # 主表
    items: list[SearchResultItem] = []
    table = soup.select_one("table.itg.gltc")
    if table:
        for tr in table.select("tr"):
            # 跳过表头（只含 th）
            if tr.find("th") and not tr.find("td"):
                continue
            it = _parse_item(tr)
            if it is not None:
                items.append(it)

    next_url = _parse_nav_url(soup, "unext") or _parse_nav_url(soup, "dnext")
    prev_url = _parse_nav_url(soup, "uprev") or _parse_nav_url(soup, "dprev")

    return SearchResultPage(
        items=items,
        total_count=total,
        next_url=next_url,
        prev_url=prev_url,
        host=host,
        keyword=keyword,
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def search_eh(
    provider,  # _EHFamilyProvider 子类（避免 circular import 不写注解）
    keyword: str,
    *,
    next_param: int | None = None,
    prev_param: int | None = None,
) -> SearchResultPage:
    """对单一站点执行一次搜索 GET。

    复用 `provider._make_client()` + `provider.HOST`；cookie / headers / timeout
    全部来自 provider 配置，跟 fetch_gallery 走同一套。

    `next_param` / `prev_param`：eh 用 `?next=<gid>` / `?prev=<gid>` 翻页，互斥；
    都不传就是第 1 页。
    """
    url = f"https://{provider.HOST}/"
    params: dict[str, str | int] = {"f_search": keyword}
    if next_param is not None:
        params["next"] = next_param
    elif prev_param is not None:
        params["prev"] = prev_param

    async with provider._make_client() as client:
        try:
            resp = await client.get(url, params=params)
        except Exception as e:
            raise EHSearchError(f"GET {url} failed: {e}") from e

    if resp.status_code != 200:
        # ex 没 cookie 时 nginx 返 404/默认页（短 body）。eh 不该 401/403。
        if provider.HOST == "exhentai.org" and resp.status_code in (302, 404):
            raise EHSearchAuthError(
                f"ExHentai 返回 HTTP {resp.status_code}，cookie 可能失效或缺失"
            )
        raise EHSearchError(f"GET {url} → HTTP {resp.status_code}")

    body = resp.text
    if len(body) < 200:
        # 极短 body：基本是 cookie 失效后的空壳
        if provider.HOST == "exhentai.org":
            raise EHSearchAuthError("ExHentai 返回空 body，cookie 失效")
        raise EHSearchError("响应 body 异常短")

    if _BANNED_RE.search(body):
        raise EHSearchBlockedError("IP 被 eh 暂时封禁（excessive pageloads）")

    logger.debug(f"ehsearch {provider.HOST} keyword={keyword!r} → {len(body)} bytes")
    return parse_search_page(body, provider.HOST, keyword)


__all__ = [
    "EHSearchError",
    "EHSearchAuthError",
    "EHSearchBlockedError",
    "SearchResultItem",
    "SearchResultPage",
    "search_eh",
    "parse_search_page",
]
