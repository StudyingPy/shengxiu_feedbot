"""下载前预估字节数的公共 helper。

这是面向所有 provider 的通用工具，给"在按下下载按钮之前给用户看一个大概的大小"
这件事提供两个原语：

- `head_or_range_content_length(client, url)`：单 URL 拿字节数。HEAD 优先，
  失败/无 Content-Length 时 fallback 到 GET `Range: bytes=0-0`，从 Content-Range
  解析。两者都失败返回 None。
- `estimate_total_bytes(client, urls, sample_count=N)`：采样前 N 张 URL 求均值，
  乘以总数得到粗略总字节数。仅供"展示一个 ~XX MB 让用户判断"使用，不是精确值。

调用方约定：
- 任何失败都返回 None / 0，永远不抛异常给上层
- timeout 默认 5s，单 URL 慢就直接放弃，不要拖延 UI
- 调用方需自己处理 Referer（不同 provider 不同）
"""

from __future__ import annotations

import asyncio
import re

import httpx

from ..utils import logger

# 解析 Content-Range: bytes 0-0/12345678 末尾的总字节数
_CONTENT_RANGE_RE = re.compile(r"/(\d+)\s*$")


async def head_or_range_content_length(
    client: httpx.AsyncClient,
    url: str,
    *,
    referer: str | None = None,
    timeout: float = 5.0,
) -> int | None:
    """拿单个 URL 的字节数。

    1. HEAD 拿 Content-Length；
    2. 失败 / 状态码非 2xx / 无 Content-Length → 用 streaming GET `Range: bytes=0-0`，
       **只读 headers**（resp.headers 在 stream 模式下 yield 后即可，不必读 body），
       从 Content-Range 解析；如果服务端忽略 Range 返 200，本来 stream 模式也不会
       预读 body，但为安全起见我们立刻 aclose 让 httpx 关 socket、不真的下整张图。
    3. 全失败返回 None。
    """
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer

    # 1) HEAD
    try:
        resp = await client.head(url, headers=headers, timeout=timeout)
        if 200 <= resp.status_code < 300:
            cl = resp.headers.get("content-length")
            if cl:
                try:
                    n = int(cl)
                    if n > 0:
                        return n
                except ValueError:
                    pass
    except Exception as e:
        logger.debug(f"HEAD {url[:80]} failed: {e}")

    # 2) Range fallback：用 stream 而非 .get()，否则服务端忽略 Range 返 200 时
    #    httpx 会读完整响应体（i.pximg.net 大图、nhentai CDN 都可能这样），
    #    把"下载前估算"反过来变成下载本身。stream 拿到 headers 就够了，body
    #    一字节都不读，离开 with 块时 httpx 会 aclose 连接。
    try:
        range_headers = {**headers, "Range": "bytes=0-0"}
        async with client.stream(
            "GET", url, headers=range_headers, timeout=timeout,
        ) as resp:
            if resp.status_code == 206:
                # 标准 partial content：解析 Content-Range
                cr = resp.headers.get("content-range")
                if cr:
                    m = _CONTENT_RANGE_RE.search(cr)
                    if m:
                        try:
                            n = int(m.group(1))
                            if n > 0:
                                return n
                        except ValueError:
                            pass
                # 206 但 Content-Range 怪 → 退而求其次也接受 Content-Length
                # （此时 CL 是这次 partial 的字节数，不是总大小，不可信，丢掉）
                return None
            if 200 <= resp.status_code < 300:
                # 服务端忽略 Range 返 200。Content-Length 此时是文件总长，可用；
                # 但**不读 body** —— async with 离开时 httpx aclose 不会真的
                # 下载完整文件（响应保持 streaming 直到 read 才落字节）。
                cl = resp.headers.get("content-length")
                if cl:
                    try:
                        n = int(cl)
                        if n > 0:
                            return n
                    except ValueError:
                        pass
    except Exception as e:
        logger.debug(f"GET Range {url[:80]} failed: {e}")

    return None


async def estimate_total_bytes(
    client: httpx.AsyncClient,
    urls: list[str],
    *,
    referer: str | None = None,
    sample_count: int = 3,
    timeout: float = 5.0,
) -> int | None:
    """采样估算 N 个 URL 的总字节数。

    - 取前 min(sample_count, len(urls)) 个 URL 并发 head_or_range_content_length；
    - 全部失败 → None；部分失败 → 用成功样本的均值；
    - 返回 估算总字节数 = 均值 × len(urls)。
    """
    if not urls:
        return 0
    n = min(sample_count, len(urls))
    samples = urls[:n]

    results = await asyncio.gather(
        *(head_or_range_content_length(client, u, referer=referer, timeout=timeout)
          for u in samples),
        return_exceptions=True,
    )

    sizes: list[int] = []
    for r in results:
        if isinstance(r, int) and r > 0:
            sizes.append(r)

    if not sizes:
        return None

    avg = sum(sizes) / len(sizes)
    return int(avg * len(urls))


__all__ = [
    "head_or_range_content_length",
    "estimate_total_bytes",
]
