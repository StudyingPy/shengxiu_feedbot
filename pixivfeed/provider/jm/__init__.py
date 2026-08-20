"""禁漫天堂（JM / 18comic / jmcomic）—— 仅查询标题。

刻意 *不* 实现 Provider 接口（没有 fetch_and_download / can_handle 等）：
本模块的唯一用途是"输入禁漫号 → 拿到标题"，再把标题喂给 /ehsearch。下载图片
有反爬限制且站点活跃度高，不在本项目范围内。

依赖 [jmcomic](https://pypi.org/project/jmcomic/) 库（同步 API），用
`asyncio.to_thread` 包成 async 给 channel 层调用。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from pykakasi import kakasi

from ...utils import logger


class JMError(Exception):
    """JM 解析失败的统一异常基类。"""


class JMNotFoundError(JMError):
    """禁漫号不存在（404 / 已删除 / 已下架）。"""


@dataclass(frozen=True)
class JMAlbumMetadata:
    """`/jm` 搜索所需的最小专辑元数据。"""

    title: str
    authors: tuple[str, ...]


# 可调阈值：clean_jm_title 截断长度。EH 搜索框对超长 query 命中率会变差。
_TITLE_MAX_LEN = 80

# 标题里需要去掉的尾缀关键词（汉化组、修正、嵌字 等不影响命中的中文标记）。
# 命中后从字符串里整段移除（不是替换为空格），相邻空白用 _collapse_ws 规整。
_TITLE_NOISE = (
    "汉化", "漢化", "翻译", "翻譯", "重嵌", "重嵌字",
    "嵌字版", "嵌字", "无修正", "無修正", "去码",
    "汉化版", "漢化版", "中文", "中國翻譯", "中国翻译",
)

# 适合把作品名和副标题拆开的装饰分隔符。它们在日文标题中常用于
# `主标题 ～副标题～`，比普通空格更能稳定地表示语义边界。
_TITLE_SECTION_RE = re.compile(r"[~～〜|｜]+")

# 副标题中常用的强分隔符。JM 与 EH 的标题可能在它们后方出现假名、异体字或
# 录入差异；保留分隔符前的短语通常更适合作为最后一级搜索锚点。
_TITLE_ANCHOR_RE = re.compile(r"[○●◎◇◆・:：]")
_JAPANESE_NAME_RE = re.compile(r"[ぁ-んァ-ヶ一-龯々國]")
_KAKASI = kakasi()


async def fetch_jm_title(jm_id: str, *, timeout: float = 20.0) -> str:
    """异步拉一个禁漫号对应作品的原始标题。

    实现思路：jmcomic 库的 `get_album_detail()` 是同步阻塞的（基于 requests），
    用 `asyncio.to_thread` 抛到线程池，再用 `asyncio.wait_for` 套外层超时。

    异常映射：
    - `MissingAlbumPhotoException` → `JMNotFoundError`
    - 网络重试全失败 / 其它库内异常 → `JMError`
    - asyncio.TimeoutError → `JMError("请求超时")`
    """
    return (await fetch_jm_metadata(jm_id, timeout=timeout)).title


async def fetch_jm_metadata(
    jm_id: str, *, timeout: float = 20.0,
) -> JMAlbumMetadata:
    """异步拉取标题与作者列表；超时及异常语义与 `fetch_jm_title` 一致。"""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_sync_fetch_metadata, jm_id),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        raise JMError(f"jm 请求超时（>{timeout:.0f}s）") from e


def _sync_fetch_metadata(jm_id: str) -> JMAlbumMetadata:
    """同步实现，跑在 to_thread 里。jmcomic 的 import 也放在这里——
    库本身有 import 副作用（loguru 配置等），延后到第一次调用避免影响 bot 启动。
    """
    try:
        import jmcomic  # type: ignore[import-untyped]
        from jmcomic import MissingAlbumPhotoException  # type: ignore[import-untyped]
    except ImportError as e:
        # pip install -e . 应当已经把 jmcomic 拉进来；漏装时给清晰提示。
        raise JMError(
            "jmcomic 库未安装。请在项目根目录跑 `pip install -e .`"
        ) from e

    option = jmcomic.JmOption.default()
    client = option.new_jm_client()
    try:
        album = client.get_album_detail(jm_id)
    except MissingAlbumPhotoException as e:
        raise JMNotFoundError(f"禁漫号 {jm_id} 不存在") from e
    except Exception as e:
        # jmcomic 自己的 RequestRetryAllFailException / ResponseUnexpectedException
        # 和站点反爬都落到这里。logger.debug 留个 trace；上层只看到 JMError。
        logger.debug(f"jmcomic.get_album_detail({jm_id}) failed: {e}")
        raise JMError(f"jm 解析失败：{e}") from e

    title = (getattr(album, "title", "") or "").strip()
    if not title:
        raise JMError(f"禁漫号 {jm_id} 返回了空标题")
    authors = tuple(
        value
        for author in (getattr(album, "authors", None) or [])
        if (value := str(author).strip())
    )
    return JMAlbumMetadata(title=title, authors=authors)


def clean_jm_title(title: str, *, max_len: int = _TITLE_MAX_LEN) -> str:
    """把禁漫标题清洗成更适合 EH 搜索的关键词。

    清洗步骤（顺序敏感）：
    1. 去掉各类括号块：`(C99)` `[作者]` `【XX社】` `（汉化）`
    2. 去掉常见汉化/修正/翻译关键字（_TITLE_NOISE）
    3. 多空白归一为单空格，trim
    4. 截断到 max_len（utf-8 char 数，不是 bytes）

    刻意不做的事：
    - 不翻译中文 → 日文（简单的关键词替换准确率太低，宁可保留原文让 EH 自己模糊匹配）
    - 不做罗马音转换
    - 不识别作者名做反向查询

    清洗结果可能为空字符串（极端情况：整个标题就是 `[XXX汉化]`），
    调用方应判空回退到原标题。
    """
    s = title

    # 1. 各类括号块（含全/半角圆括号、方括号、尖括号、中文方括号）
    # 用循环处理嵌套：`[X[Y]]` 一次只剥外层最里 pair，反复直到不再变化。
    bracket_pairs = [
        ("(", ")"), ("（", "）"),
        ("[", "]"), ("【", "】"),
        ("〈", "〉"), ("《", "》"),
    ]
    for _ in range(8):  # 上限 8 层，多了不正常
        before = s
        for op, cl in bracket_pairs:
            # 非贪婪 + 不跨行
            s = re.sub(rf"\{op}[^\{op}\{cl}]*\{cl}", " ", s)
        if s == before:
            break

    # JM 偶尔会返回被截断的元数据尾缀（例如 `[中國翻譯`）。成对括号已经在
    # 上面移除；这里仅清掉从最后一个未闭合左括号开始的尾部，避免留下孤立 `[`。
    orphan_suffix = re.search(r"\s*[\[【(（〈《][^\]】)）〉》]*$", s)
    if orphan_suffix and orphan_suffix.start() > 0:
        s = s[:orphan_suffix.start()]

    # 2. 噪声关键词
    for kw in _TITLE_NOISE:
        s = s.replace(kw, " ")

    # 3. 空白归一
    s = re.sub(r"\s+", " ", s).strip()

    # 4. 截断
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


def jm_search_candidates(title: str, *, max_len: int = _TITLE_MAX_LEN) -> list[str]:
    """生成由精确到宽松的 EH 搜索词，供 `/jm` 零结果时逐级回退。

    顺序为：清洗后的完整标题、按标题装饰符切出的较长片段、片段中强分隔符
    前的稳定锚点。候选会去重且最多返回 4 个，避免一个 JM 查询产生过多站点请求。
    """
    cleaned = clean_jm_title(title, max_len=max_len)
    if not cleaned:
        cleaned = re.sub(r"\s+", " ", title).strip()[:max_len].rstrip()
    if not cleaned:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    def _add(value: str, *, primary: bool = False) -> None:
        value = value.strip(" \t\r\n-—–_~～〜|｜[]【】()（）〈〉《》")
        value = re.sub(r"\s+", " ", value).strip()
        key = re.sub(r"[\W_]+", "", value.casefold())
        min_len = 2 if primary else 4
        if (
            len(key) < min_len
            or key.isdecimal()
            or key in seen
            or len(candidates) >= 4
        ):
            return
        seen.add(key)
        candidates.append(value[:max_len].rstrip())

    _add(cleaned, primary=True)
    sections = [part.strip() for part in _TITLE_SECTION_RE.split(cleaned)]
    # 较长片段往往是有辨识度的副标题；逐片段紧跟它的短锚点，能尽早命中。
    for section in sorted(sections, key=len, reverse=True):
        _add(section)
        anchor = _TITLE_ANCHOR_RE.split(section, maxsplit=1)[0]
        if anchor != section:
            _add(anchor)

    return candidates


def jm_creator_hints(title: str, authors: tuple[str, ...] = ()) -> list[str]:
    """合并 JM 作者字段与标题开头的 `[社团 (作者)]` 线索。

    JM 没有独立的社团字段，且 `authors` 偶尔不完整；标题前缀只作为补充线索，
    不承担硬过滤。返回值保持原文显示、按规范化后的大小写和标点去重。
    """
    values = [*authors]
    if match := re.match(r"^\s*[\[【]([^\]】]+)[\]】]", title):
        prefix = match.group(1).strip()
        nested = re.findall(r"[（(]([^）)]+)[）)]", prefix)
        outer = re.sub(r"[（(][^）)]*[）)]", " ", prefix)
        values.extend([outer, *nested])

    hints: list[str] = []
    seen: set[str] = set()

    def _add_hint(value: str) -> None:
        key = re.sub(r"[\W_]+", "", value.casefold())
        if len(key) < 2 or key in seen:
            return
        seen.add(key)
        hints.append(value)

    for value in values:
        for part in re.split(r"\s*(?:/|／|,|，|&|＆|×)\s*", value):
            part = re.sub(r"\s+", " ", part).strip()
            _add_hint(part)
            if _JAPANESE_NAME_RE.search(part):
                romanized = "".join(
                    token["hepburn"] for token in _KAKASI.convert(part)
                ).casefold()
                # EH 新标签仅接受 ASCII 字母、数字、连字符、句点与空格。
                romanized = re.sub(r"[^a-z0-9. -]+", "", romanized)
                romanized = re.sub(r"\s+", " ", romanized).strip()
                _add_hint(romanized)
    return hints


__all__ = [
    "JMError",
    "JMNotFoundError",
    "JMAlbumMetadata",
    "fetch_jm_title",
    "fetch_jm_metadata",
    "clean_jm_title",
    "jm_search_candidates",
    "jm_creator_hints",
]
