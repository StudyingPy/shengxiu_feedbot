"""Pixiv Provider —— 实现统一 Provider 接口。

特殊性：
- pixiv 有 illust 和 novel 两种产物，统一 GalleryWork 只覆盖 illust。
- novel 走专属路径（publish_novel），由 channel 层根据 ref.kind 分流。
  这意味着 fetch_and_download() 仅在 ref.kind == 'illust' 时合法。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...provider import GalleryImage, GalleryWork, ParsedRef, ProgressHook, Provider
from ...utils import logger
from .api import PixivAPI, PixivAPIError, PixivAuthError, PixivNotFoundError
from .downloader import DownloadedImage, PixivDownloader, relative_url
from .model import IllustWork, NovelWork
from .parser import parse_illust_meta, parse_novel_meta
from .ugoira import (
    UgoiraError,
    UgoiraFFmpegMissingError,
    UgoiraValidationError,
    convert_ugoira,
    ffmpeg_available,
    parse_ugoira_meta,
)
from .url import extract_pixiv_refs, parse_inline_query


@dataclass
class IllustResult:
    """完整的 illust 解析结果：元数据 + 已下载到本地的图片 + 对外 URL。

    保留这个类型用于 channel 层的直发逻辑（需要本地 tg_photo_path）。
    publisher.publish_gallery 走 GalleryWork 路径不需要它。
    """

    work: IllustWork
    images: list[DownloadedImage]
    public_urls_original: list[str]
    public_urls_tgphoto: list[str]


@dataclass
class UgoiraResult:
    """动图转换结果：GIF 本地路径 + Nginx 对外 URL。"""

    work: IllustWork
    gif_path: Path
    public_url: str


class PixivProvider(Provider):
    """Pixiv 数据源。"""

    def __init__(
        self,
        config,
        cache_dir: str | Path = ".",
        public_base_url: str = "",
    ):
        """
        config: Config 实例（运行时变更立即生效）
        cache_dir / public_base_url: 这两个是"动了得搬目录"的基础设施配置，
                                     从 Config 复制下来作为不可变属性
        """
        self.config = config
        self.cache_dir = Path(cache_dir)
        self.public_base_url = public_base_url.rstrip("/")
        # 同一 pid 的转换串行化，避免两个请求同时下载/转换同一个动图。
        self._ugoira_locks: dict[str, asyncio.Lock] = {}

    @property
    def phpsessid(self) -> str:
        return self.config.pixiv.phpsessid

    @property
    def timeout(self) -> int:
        return self.config.pixiv.timeout

    @property
    def concurrency(self) -> int:
        return self.config.pixiv.download_concurrency

    @property
    def name(self) -> str:
        return "pixiv"

    # ------------------------------------------------------------------
    # Provider 接口
    # ------------------------------------------------------------------

    def can_handle(self, text: str) -> bool:
        return bool(extract_pixiv_refs(text))

    def extract_refs(self, text: str) -> list[ParsedRef]:
        return [
            ParsedRef(provider=self.name, kind=r.kind, id=r.id, raw=r.raw)
            for r in extract_pixiv_refs(text)
        ]

    async def fetch_work(self, ref: ParsedRef) -> Any:
        """根据 kind 返回 IllustWork 或 NovelWork。"""
        if ref.kind == "illust":
            return await self.fetch_illust(ref.id)
        if ref.kind == "novel":
            return await self.fetch_novel(ref.id)
        raise ValueError(f"unknown pixiv ref kind: {ref.kind}")

    async def fetch_and_download(
        self, ref: ParsedRef, *, on_progress: ProgressHook = None
    ) -> GalleryWork:
        """完整流程，仅支持 illust。novel 请走 channel 的专属路径。"""
        if ref.kind != "illust":
            raise NotImplementedError(
                f"PixivProvider.fetch_and_download only handles illust, got {ref.kind!r}; "
                "use provider.pixiv.novel_publisher.publish_novel for novels"
            )
        result = await self.fetch_and_download_illust(ref.id, on_progress=on_progress)
        return _illust_result_to_gallery(result)

    # ------------------------------------------------------------------
    # Pixiv 专属高层流程（保留供 channel/inline/CLI 使用）
    # ------------------------------------------------------------------

    async def fetch_illust(self, pid: str) -> IllustWork:
        """只拉元数据，不下载图片。

        多图作品必然需要 /pages 才能拿到所有图片 URL。
        单图作品在登录态下 body['urls'] 已含 URL，未登录时 body['urls'] 全为 null，
        因此需要根据响应内容动态判断是否 fallback 到 /pages。
        """
        async with PixivAPI(self.phpsessid, self.timeout) as api:
            meta_body = await api.fetch_illust(pid)
            page_count = int(meta_body.get("pageCount") or 1)
            urls = meta_body.get("urls") or {}
            need_pages = page_count > 1 or not urls.get("original")
            pages = await api.fetch_illust_pages(pid) if need_pages else None
            return parse_illust_meta(meta_body, pages)

    async def fetch_and_download_illust(
        self, pid: str, *, on_progress: ProgressHook = None
    ) -> IllustResult:
        """完整流程：元数据 + 下载所有原图 + 派生 TG 图。"""
        async with PixivAPI(self.phpsessid, self.timeout) as api:
            meta_body = await api.fetch_illust(pid)
            page_count = int(meta_body.get("pageCount") or 1)
            urls = meta_body.get("urls") or {}
            need_pages = page_count > 1 or not urls.get("original")
            pages = await api.fetch_illust_pages(pid) if need_pages else None
            work = parse_illust_meta(meta_body, pages)

            downloader = PixivDownloader(api, self.cache_dir, self.concurrency)
            original_urls = [img.original for img in work.images]
            downloaded = await downloader.download_illust(
                pid, original_urls, on_progress=on_progress
            )

        public_orig = [
            relative_url(self.public_base_url, self.cache_dir, d.original_path) for d in downloaded
        ]
        public_tg = [
            relative_url(self.public_base_url, self.cache_dir, d.tgphoto_path) for d in downloaded
        ]
        return IllustResult(
            work=work,
            images=downloaded,
            public_urls_original=public_orig,
            public_urls_tgphoto=public_tg,
        )

    async def fetch_novel(self, nid: str) -> NovelWork:
        async with PixivAPI(self.phpsessid, self.timeout) as api:
            body = await api.fetch_novel(nid)
            return parse_novel_meta(body)

    # ------------------------------------------------------------------
    # Ugoira
    # ------------------------------------------------------------------

    async def fetch_and_convert_ugoira(self, work: IllustWork) -> UgoiraResult:
        """拉 ugoira_meta、下载帧 ZIP 并转成 GIF。

        产物在 {cache_dir}/{pid}/ugoira/animation.gif，命中即返回。ZIP 是
        转换期间的临时文件，任何失败都清掉半成品，下次请求全量重试。
        """
        pid = work.pid
        lock = self._ugoira_locks.setdefault(pid, asyncio.Lock())
        async with lock:
            work_dir = self.cache_dir / pid / "ugoira"
            gif_path = work_dir / "animation.gif"

            if not gif_path.exists():
                # 缺 ffmpeg 时在发起任何 HTTP 请求前失败。
                if not ffmpeg_available():
                    raise UgoiraFFmpegMissingError("ffmpeg not found; ugoira conversion unavailable")
                work_dir.mkdir(parents=True, exist_ok=True)
                async with PixivAPI(self.phpsessid, self.timeout) as api:
                    meta = parse_ugoira_meta(await api.fetch_ugoira_meta(pid))
                    zip_bytes = await api.download_image(meta.zip_url)

                zip_path = work_dir / ".frames.zip.tmp"
                try:
                    zip_path.write_bytes(zip_bytes)
                    # ZIP 解包 + ffmpeg 都是阻塞操作，扔到线程池。
                    await asyncio.to_thread(convert_ugoira, zip_path, meta, gif_path)
                except BaseException:
                    try:
                        gif_path.unlink(missing_ok=True)
                    except OSError:
                        logger.exception(f"[{pid}] failed to clean partial ugoira gif")
                    raise
                finally:
                    try:
                        zip_path.unlink(missing_ok=True)
                    except OSError:
                        logger.exception(f"[{pid}] failed to clean ugoira zip")

            return UgoiraResult(
                work=work,
                gif_path=gif_path,
                public_url=relative_url(self.public_base_url, self.cache_dir, gif_path),
            )


def _illust_result_to_gallery(result: IllustResult) -> GalleryWork:
    """把 pixiv 的 IllustResult 降维成通用 GalleryWork。"""
    images = [
        GalleryImage(
            page_index=d.page_index,
            local_path=d.original_path,
            public_url=public,
            tg_photo_path=d.tgphoto_path,
        )
        for d, public in zip(result.images, result.public_urls_original)
    ]
    work = result.work
    extra = work.template_vars()
    # 同时暴露 {pid} 别名，老模板里写了 {pid} 仍能用
    extra["pid"] = work.pid
    return GalleryWork(
        provider="pixiv",
        kind="illust",
        work_id=work.pid,
        source_url=f"https://www.pixiv.net/artworks/{work.pid}",
        title=work.title,
        author=work.author,
        images=images,
        extra_vars=extra,
    )


__all__ = [
    "PixivProvider",
    "IllustResult",
    "UgoiraResult",
    "IllustWork",
    "NovelWork",
    "PixivAPIError",
    "PixivAuthError",
    "PixivNotFoundError",
    "UgoiraError",
    "UgoiraFFmpegMissingError",
    "UgoiraValidationError",
    "extract_pixiv_refs",
    "parse_inline_query",
]
