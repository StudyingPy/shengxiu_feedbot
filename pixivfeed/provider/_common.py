"""所有 Provider 共享的小工具。

包括：
- http_client_factory: 构造统一配置的 httpx.AsyncClient（http2 / 超时 / Referer）
- download_concurrent: 用 asyncio.Semaphore 并发下载一组 URL 到本地路径
- relative_url: 把 cache_dir 下的本地路径转成 base_url 可访问的对外 URL
- safe_filename: 从 URL 末尾提取安全的扩展名

刻意保持薄层。pixiv 的 PixivAPI 有自己的 Referer/cookie 处理，
不强行让它走这里——但底层下载用同一个 httpx 实例可以最大化连接池命中。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from ..utils import logger


def make_http_client(
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: int = 30,
    http2: bool = True,
    follow_redirects: bool = True,
) -> httpx.AsyncClient:
    """构造统一配置的 httpx.AsyncClient。"""
    return httpx.AsyncClient(
        headers=headers or {},
        cookies=cookies or {},
        timeout=timeout,
        http2=http2,
        follow_redirects=follow_redirects,
    )


def safe_ext_from_url(url: str, default: str = ".jpg") -> str:
    """从 URL 末尾抠扩展名（含点）。带 query 时会去掉 query 部分。"""
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "." in name:
        return "." + name.rsplit(".", 1)[-1].lower()
    return default


def relative_url(base_url: str, cache_dir: str | Path, local_path: Path) -> str:
    """把本地缓存路径转成对外可访问的 URL。

    base_url 是 Nginx 暴露 cache_dir 的前缀（如 https://example.com/p）。
    尾斜杠会被自动剥除。
    """
    rel = local_path.resolve().relative_to(Path(cache_dir).resolve())
    return f"{base_url.rstrip('/')}/{rel.as_posix()}"


async def download_to_file(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    headers: dict[str, str] | None = None,
    skip_existing: bool = True,
    retries: int = 3,
) -> Path:
    """单文件下载，自动重试。

    headers 用于覆盖（追加）client 默认 header，例如个别图片需要特殊 Referer。
    """
    if skip_existing and dest.exists() and dest.stat().st_size > 0:
        return dest

    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code} for {url}",
                    request=resp.request,
                    response=resp,
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            # 写入临时文件再 rename，避免半截文件被当 cache 命中
            tmp = dest.with_suffix(dest.suffix + ".part")
            tmp.write_bytes(resp.content)
            tmp.replace(dest)
            return dest
        except Exception as e:
            last_exc = e
            wait = 0.5 * (attempt + 1)
            logger.debug(f"download attempt {attempt + 1} failed for {url}: {e}; retry in {wait}s")
            await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc


async def download_many(
    client: httpx.AsyncClient,
    urls: list[str],
    dest_paths: list[Path],
    *,
    concurrency: int = 4,
    headers: dict[str, str] | None = None,
    skip_existing: bool = True,
    retries: int = 3,
    progress: callable | None = None,
) -> list[Path]:
    """并发下载多个 URL 到对应路径。

    progress(done, total) 回调可选，用于日志或后续接 TG 状态消息更新。
    """
    if len(urls) != len(dest_paths):
        raise ValueError(f"urls/dest_paths length mismatch: {len(urls)} vs {len(dest_paths)}")

    sem = asyncio.Semaphore(concurrency)
    done = 0
    total = len(urls)
    lock = asyncio.Lock()

    async def _one(idx: int, url: str, dest: Path) -> Path:
        nonlocal done
        async with sem:
            path = await download_to_file(
                client, url, dest,
                headers=headers, skip_existing=skip_existing, retries=retries,
            )
        async with lock:
            done += 1
            if progress is not None:
                try:
                    progress(done, total)
                except Exception:
                    logger.exception("progress callback raised; suppressed")
        return path

    return await asyncio.gather(*(_one(i, u, p) for i, (u, p) in enumerate(zip(urls, dest_paths))))


__all__ = [
    "make_http_client",
    "safe_ext_from_url",
    "relative_url",
    "download_to_file",
    "download_many",
]
