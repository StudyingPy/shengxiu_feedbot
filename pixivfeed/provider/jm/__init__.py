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

from ...utils import logger


class JMError(Exception):
    """JM 解析失败的统一异常基类。"""


class JMNotFoundError(JMError):
    """禁漫号不存在（404 / 已删除 / 已下架）。"""


# 可调阈值：clean_jm_title 截断长度。EH 搜索框对超长 query 命中率会变差。
_TITLE_MAX_LEN = 80

# 标题里需要去掉的尾缀关键词（汉化组、修正、嵌字 等不影响命中的中文标记）。
# 命中后从字符串里整段移除（不是替换为空格），相邻空白用 _collapse_ws 规整。
_TITLE_NOISE = (
    "汉化", "漢化", "翻译", "翻譯", "重嵌", "重嵌字",
    "嵌字版", "嵌字", "无修正", "無修正", "去码",
    "汉化版", "漢化版", "中文", "中國翻譯", "中国翻译",
)


async def fetch_jm_title(jm_id: str, *, timeout: float = 20.0) -> str:
    """异步拉一个禁漫号对应作品的原始标题。

    实现思路：jmcomic 库的 `get_album_detail()` 是同步阻塞的（基于 requests），
    用 `asyncio.to_thread` 抛到线程池，再用 `asyncio.wait_for` 套外层超时。

    异常映射：
    - `MissingAlbumPhotoException` → `JMNotFoundError`
    - 网络重试全失败 / 其它库内异常 → `JMError`
    - asyncio.TimeoutError → `JMError("请求超时")`
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_sync_fetch_title, jm_id),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        raise JMError(f"jm 请求超时（>{timeout:.0f}s）") from e


def _sync_fetch_title(jm_id: str) -> str:
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
    return title


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

    # 2. 噪声关键词
    for kw in _TITLE_NOISE:
        s = s.replace(kw, " ")

    # 3. 空白归一
    s = re.sub(r"\s+", " ", s).strip()

    # 4. 截断
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


__all__ = [
    "JMError",
    "JMNotFoundError",
    "fetch_jm_title",
    "clean_jm_title",
]
