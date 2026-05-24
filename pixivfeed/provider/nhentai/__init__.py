"""nhentai Provider。

数据获取借鉴 DojinGo：
- nhapi.cat42.uk 第三方 JSON API（nhentai.net 本身没开放 API，DojinGo 走的是社区镜像）
- 图片 CDN 在 i1~i4.nhentai.net 之间随机分配，单个失败用其他几个 fallback

URL 形式：
    https://nhentai.net/g/{id}
    https://nhentai.to/g/{id}      （镜像）
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from ...utils import logger
from .. import GalleryImage, GalleryWork, ParsedRef, ProgressHook, Provider
from .._common import (
    download_many,
    make_http_client,
    relative_url,
    safe_ext_from_url,
)

_NHENTAI_RE = re.compile(r"https?://nhentai\.(?:net|to)/g/(\d+)", re.IGNORECASE)

# 这些常量直接照搬 DojinGo（仍然是 2026 年现状）
NHENTAI_API = "https://nhapi.cat42.uk/gallery/"
NHENTAI_CDNS = [
    "https://i1.nhentai.net/galleries",
    "https://i2.nhentai.net/galleries",
    "https://i3.nhentai.net/galleries",
    "https://i4.nhentai.net/galleries",
]
# 文件类型缩写 → 扩展名（DojinGo nhImage.t）
_TYPE_EXT = {"j": ".jpg", "p": ".png", "g": ".gif", "w": ".webp"}


class NHentaiError(Exception):
    """nhentai 解析或下载失败。"""


@dataclass
class NHentaiAlbum:
    """画廊元数据。"""

    gallery_id: str
    media_id: str
    title: str
    page_types: list[str]   # 每页对应的 type code (j/p/g/w)
    tags: list[str]
    num_pages: int


def _best_title(t: dict) -> str:
    """nhTitle.bestTitle 移植。"""
    return t.get("pretty") or t.get("english") or t.get("japanese") or ""


def _cdn_url(media_id: str, page: int, ext: str) -> str:
    """从 CDN 列表里随机挑一个组装 URL。"""
    base = random.choice(NHENTAI_CDNS)
    return f"{base}/{media_id}/{page}{ext}"


class NHentaiProvider(Provider):
    """nhentai 数据源。

    public_base_url / cache_dir 与 PixivProvider 共用同一份配置——
    所有 Provider 的图片都进同一个 cache_dir，由同一个 Nginx 暴露。
    """

    def __init__(
        self,
        cache_dir: str | Path,
        public_base_url: str,
        *,
        config,
    ):
        self.cache_dir = Path(cache_dir)
        self.public_base_url = public_base_url.rstrip("/")
        self.config = config

    @property
    def name(self) -> str:
        return "nhentai"

    @property
    def _shared_cfg(self):
        return self.config.collectors

    def can_handle(self, text: str) -> bool:
        if not self.config.collectors.nhentai.enabled:
            return False
        return bool(_NHENTAI_RE.search(text))

    def extract_refs(self, text: str) -> list[ParsedRef]:
        seen: set[str] = set()
        refs: list[ParsedRef] = []
        for m in _NHENTAI_RE.finditer(text):
            gid = m.group(1)
            if gid in seen:
                continue
            seen.add(gid)
            refs.append(ParsedRef(provider=self.name, kind="gallery", id=gid, raw=m.group(0)))
        return refs

    # ------------------------------------------------------------------

    async def fetch_work(self, ref: ParsedRef) -> NHentaiAlbum:
        return await self._fetch_album(ref.id)

    async def _fetch_album(self, gallery_id: str) -> NHentaiAlbum:
        url = NHENTAI_API + gallery_id
        async with make_http_client(timeout=self._shared_cfg.timeout) as client:
            for attempt in range(5):
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        break
                except Exception as e:
                    if attempt == 4:
                        raise NHentaiError(f"fetch {url} failed: {e}") from e
            else:
                raise NHentaiError(f"fetch {url} returned non-200")

        media_id = str(data.get("media_id") or "")
        if not media_id:
            raise NHentaiError(f"nhentai gallery {gallery_id}: missing media_id")
        title_dict = data.get("title") or {}
        title = _best_title(title_dict) or f"nhentai-{gallery_id}"
        pages = (data.get("images") or {}).get("pages") or []
        page_types = [(p.get("t") or "j") for p in pages]
        tags = [t.get("name") for t in (data.get("tags") or []) if t.get("name")]

        return NHentaiAlbum(
            gallery_id=gallery_id,
            media_id=media_id,
            title=title,
            page_types=page_types,
            tags=tags,
            num_pages=len(pages),
        )

    async def fetch_and_download(
        self, ref: ParsedRef, *, on_progress: ProgressHook = None
    ) -> GalleryWork:
        album = await self._fetch_album(ref.id)

        work_dir = self.cache_dir / f"nh_{album.gallery_id}"
        work_dir.mkdir(parents=True, exist_ok=True)

        # 给每页生成主 URL + fallback CDN 列表
        main_urls: list[str] = []
        all_dest: list[Path] = []
        per_page_fallbacks: list[list[str]] = []
        for idx, t in enumerate(album.page_types, start=1):
            ext = _TYPE_EXT.get(t, ".jpg")
            main_urls.append(_cdn_url(album.media_id, idx, ext))
            all_dest.append(work_dir / f"p{idx}{ext}")
            # 同 idx 的所有 CDN 候选
            per_page_fallbacks.append(
                [f"{base}/{album.media_id}/{idx}{ext}" for base in NHENTAI_CDNS]
            )

        async with make_http_client(timeout=self._shared_cfg.timeout) as client:
            await self._download_with_cdn_fallback(
                client, main_urls, all_dest, per_page_fallbacks,
                on_progress=on_progress,
            )

        images = [
            GalleryImage(
                page_index=i,
                local_path=path,
                public_url=relative_url(self.public_base_url, self.cache_dir, path),
            )
            for i, path in enumerate(all_dest)
        ]

        extra = {
            "tags": " ".join(f"#{t}" for t in album.tags),
            "media_id": album.media_id,
            "gallery_id": album.gallery_id,
        }

        return GalleryWork(
            provider="nhentai",
            kind="gallery",
            work_id=album.gallery_id,
            source_url=f"https://nhentai.net/g/{album.gallery_id}",
            title=album.title,
            author="",  # nhentai 没有 author 概念，只有 tags 里的 artist
            images=images,
            extra_vars=extra,
        )

    async def _download_with_cdn_fallback(
        self,
        client: httpx.AsyncClient,
        main_urls: list[str],
        dests: list[Path],
        fallbacks: list[list[str]],
        *,
        on_progress: ProgressHook = None,
    ) -> None:
        """先按主 URL 并发下，失败的逐一在 fallback CDN 里重试。"""
        # 先尝试一轮主 URL（不带重试：失败的让 fallback 处理）
        from asyncio import Lock, Semaphore, gather

        sem = Semaphore(self._shared_cfg.download_concurrency)

        failed_indices: list[int] = []
        total = len(main_urls)
        done = 0
        done_lock = Lock()

        async def _tick() -> None:
            nonlocal done
            if on_progress is None:
                return
            async with done_lock:
                done += 1
                cur = done
            try:
                await on_progress(cur, total)
            except Exception:
                logger.exception("nhentai progress hook raised; suppressed")

        async def _try_one(idx: int) -> None:
            url = main_urls[idx]
            dest = dests[idx]
            if dest.exists() and dest.stat().st_size > 0:
                await _tick()
                return
            async with sem:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        raise httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}", request=resp.request, response=resp
                        )
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    tmp.write_bytes(resp.content)
                    tmp.replace(dest)
                except Exception as e:
                    logger.debug(f"nhentai p{idx + 1} primary {url} failed: {e}, will fallback")
                    failed_indices.append(idx)
                    return
            await _tick()

        await gather(*(_try_one(i) for i in range(len(main_urls))))

        # 失败的逐一走其它 CDN
        for idx in failed_indices:
            dest = dests[idx]
            success = False
            for fb in fallbacks[idx]:
                if fb == main_urls[idx]:
                    continue
                try:
                    resp = await client.get(fb)
                    if resp.status_code != 200:
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    tmp.write_bytes(resp.content)
                    tmp.replace(dest)
                    success = True
                    break
                except Exception:
                    continue
            if not success:
                raise NHentaiError(
                    f"nhentai page {idx + 1}: all CDN candidates failed"
                )
            await _tick()


__all__ = ["NHentaiProvider", "NHentaiAlbum", "NHentaiError", "NHENTAI_CDNS"]
