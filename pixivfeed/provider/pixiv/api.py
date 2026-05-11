"""Pixiv AJAX 接口客户端。

只封装我们用得到的端点：
- /ajax/illust/{pid}              获取插画/漫画元数据
- /ajax/illust/{pid}/pages        获取多图作品的所有图片 URL（page_count > 1 时）
- /ajax/illust/{pid}/ugoira_meta  动图元数据（保留接口，目前不实现下载）
- /ajax/novel/{nid}               获取小说全文与元数据

PHPSESSID 是关键：不带或失效时，R-18 作品会返回 error=true。
"""

from __future__ import annotations

from typing import Any

import httpx

from ...utils import logger


class PixivAPIError(Exception):
    """Pixiv 返回 error=true，或 HTTP 状态码异常。"""

    def __init__(self, message: str, *, pid: str | None = None, status: int | None = None):
        super().__init__(message)
        self.pid = pid
        self.status = status


class PixivAuthError(PixivAPIError):
    """PHPSESSID 失效或访问受限内容（R-18）时未登录。"""


class PixivNotFoundError(PixivAPIError):
    """作品不存在或已被删除。"""


# Pixiv 对 AJAX 接口要求 Referer 必须是 pixiv.net 自家页面。User-Agent 太老或太短会 403。
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
    "Referer": "https://www.pixiv.net/",
}


class PixivAPI:
    """异步 HTTP 客户端，需要在 async with 中使用以正确关闭连接。"""

    def __init__(self, phpsessid: str = "", timeout: int = 30):
        self.phpsessid = phpsessid
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> PixivAPI:
        cookies = {}
        if self.phpsessid:
            cookies["PHPSESSID"] = self.phpsessid
        self._client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            cookies=cookies,
            timeout=self.timeout,
            http2=True,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("PixivAPI must be used as async context manager")
        return self._client

    # ------------------------------------------------------------------
    # 内部：通用响应处理
    # ------------------------------------------------------------------

    async def _get_json(self, url: str, *, label: str = "") -> dict[str, Any]:
        """Pixiv AJAX 响应统一形如 {"error": bool, "message": str, "body": ...}。"""
        try:
            resp = await self.client.get(url)
        except httpx.HTTPError as e:
            raise PixivAPIError(f"network error for {label or url}: {e}") from e

        if resp.status_code == 404:
            raise PixivNotFoundError(f"not found: {label or url}", status=404)
        if resp.status_code in (401, 403):
            raise PixivAuthError(
                f"auth required (HTTP {resp.status_code}) for {label or url}; "
                "PHPSESSID may be missing or expired",
                status=resp.status_code,
            )
        if resp.status_code != 200:
            raise PixivAPIError(
                f"unexpected HTTP {resp.status_code} for {label or url}",
                status=resp.status_code,
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise PixivAPIError(f"invalid JSON from {label or url}: {e}") from e

        if data.get("error"):
            msg = data.get("message", "(no message)")
            # Pixiv 对未登录访问 R-18 的典型回复包含「ログイン」或「該当作品は閲覧制限」
            if "ログイン" in msg or "該当作品" in msg or "限制" in msg:
                raise PixivAuthError(f"{label or url}: {msg}")
            if "対象作品" in msg or "見つかりません" in msg or "已删除" in msg:
                raise PixivNotFoundError(f"{label or url}: {msg}")
            raise PixivAPIError(f"{label or url}: {msg}")

        return data["body"]

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    async def fetch_illust(self, pid: str) -> dict[str, Any]:
        """获取插画/漫画元数据。包含首图 URL，但多图作品需要再调用 fetch_illust_pages。"""
        url = f"https://www.pixiv.net/ajax/illust/{pid}"
        body = await self._get_json(url, label=f"illust/{pid}")
        logger.debug(f"fetched illust meta: pid={pid} title={body.get('illustTitle')!r}")
        return body

    async def fetch_illust_pages(self, pid: str) -> list[dict[str, Any]]:
        """获取多图作品的所有图片 URL。返回数组的每项含 urls.{thumb_mini, small, regular, original}。"""
        url = f"https://www.pixiv.net/ajax/illust/{pid}/pages"
        body = await self._get_json(url, label=f"illust/{pid}/pages")
        if not isinstance(body, list):
            raise PixivAPIError(f"unexpected pages response shape: {type(body).__name__}")
        return body

    async def fetch_novel(self, nid: str) -> dict[str, Any]:
        """获取小说元数据与正文。"""
        url = f"https://www.pixiv.net/ajax/novel/{nid}"
        body = await self._get_json(url, label=f"novel/{nid}")
        logger.debug(f"fetched novel: nid={nid} title={body.get('title')!r}")
        return body

    async def download_image(self, url: str) -> bytes:
        """下载 i.pximg.net 上的图片。这是真正会跑流量的接口。

        i.pximg.net 校验 Referer 必须是 www.pixiv.net；DEFAULT_HEADERS 已含。
        """
        try:
            resp = await self.client.get(url)
        except httpx.HTTPError as e:
            raise PixivAPIError(f"download failed for {url}: {e}") from e
        if resp.status_code != 200:
            raise PixivAPIError(f"download HTTP {resp.status_code} for {url}", status=resp.status_code)
        return resp.content


__all__ = [
    "PixivAPI",
    "PixivAPIError",
    "PixivAuthError",
    "PixivNotFoundError",
]
