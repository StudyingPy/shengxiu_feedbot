"""nhentai Provider。

数据获取参考 DojinGo（最新版本）：
- 走 nhentai.net 官方 JSON API（`/api/v2/galleries/<id>` 和 `/api/v2/cdn`）。
  历史上曾用 `nhapi.cat42.uk` 第三方镜像，2026 年那边 502 整站挂掉，已切回官方。
- 图片 CDN 在 i1~i4.nhentai.net 之间随机分配，单个失败用其他几个 fallback。

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

# nhentai 官方公开 API。返回结构见 _fetch_album。
NHENTAI_API = "https://nhentai.net/api/v2/galleries/"
# CDN 列表保留 `/galleries` 后缀——下载 URL 用 `{base}/{media_id}/{idx}{ext}` 拼，
# 跟官方 page.path（`galleries/{media_id}/{idx}.{ext}`）拼出来等价。
NHENTAI_CDNS = [
    "https://i1.nhentai.net/galleries",
    "https://i2.nhentai.net/galleries",
    "https://i3.nhentai.net/galleries",
    "https://i4.nhentai.net/galleries",
]
# 文件类型缩写 → 扩展名（DojinGo nhImage.t 旧 schema）
_TYPE_EXT = {"j": ".jpg", "p": ".png", "g": ".gif", "w": ".webp"}
# 反向：扩展名 → 单字符 type code。官方 API 不再返回 t 字段，从 path 扩展名反推
# 后填回 NHentaiAlbum.page_types，保持外部消费方（handlers.py 的 size prefetch）不变。
_EXT_TYPE = {".jpg": "j", ".jpeg": "j", ".png": "p", ".gif": "g", ".webp": "w"}

# nhentai.net 主站偶尔会对默认 httpx UA 触发风控，给一份普通浏览器 UA 兜底。
_NH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


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
        """拉一个画廊的元数据。

        新版 nhentai 官方 API 返回结构（节选）：
            {
              "id": 630134,
              "media_id": "3790839",
              "title": {"english": "...", "japanese": "...", "pretty": "..."},
              "tags": [{"name": "...", ...}, ...],
              "num_pages": 33,
              "pages": [
                {"number": 1, "path": "galleries/3790839/1.webp", ...},
                ...
              ]
            }

        和旧 cat42 镜像的差异：path 取代了 t（type code），扩展名直接读得到。
        """
        url = NHENTAI_API + gallery_id
        last_exc: Exception | None = None
        data: dict | None = None
        async with make_http_client(
            timeout=self._shared_cfg.timeout, headers=_NH_HEADERS,
        ) as client:
            for attempt in range(5):
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        break
                    if resp.status_code == 404:
                        raise NHentaiError(f"nhentai gallery {gallery_id} not found (404)")
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp,
                    )
                except NHentaiError:
                    raise
                except Exception as e:
                    last_exc = e
                    logger.debug(
                        f"nhentai api {url} attempt {attempt + 1}/5 failed: {e}"
                    )
            if data is None:
                raise NHentaiError(
                    f"fetch {url} failed after 5 attempts: {last_exc}"
                ) from last_exc

        media_id = str(data.get("media_id") or "")
        if not media_id:
            raise NHentaiError(f"nhentai gallery {gallery_id}: missing media_id")
        title_dict = data.get("title") or {}
        title = _best_title(title_dict) or f"nhentai-{gallery_id}"

        # 兼容两种 schema：
        # - 新（官方）：data["pages"] = [{number, path: "galleries/<mid>/<n>.<ext>", ...}]
        # - 旧（cat42）：data["images"]["pages"] = [{t: "j"/"p"/...}, ...]
        pages = data.get("pages") or (data.get("images") or {}).get("pages") or []
        page_types: list[str] = []
        for p in pages:
            t = p.get("t")
            if t:
                page_types.append(t)
                continue
            path = p.get("path") or ""
            ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ".jpg"
            page_types.append(_EXT_TYPE.get(ext, "j"))

        tags = [t.get("name") for t in (data.get("tags") or []) if t.get("name")]
        num_pages = int(data.get("num_pages") or len(pages))

        return NHentaiAlbum(
            gallery_id=gallery_id,
            media_id=media_id,
            title=title,
            page_types=page_types,
            tags=tags,
            num_pages=num_pages,
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

        async with make_http_client(
            timeout=self._shared_cfg.timeout, headers=_NH_HEADERS,
        ) as client:
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
