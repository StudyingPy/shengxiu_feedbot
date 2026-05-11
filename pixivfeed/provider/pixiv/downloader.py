"""图片下载与派生。

目录结构：
    {cache_dir}/{pid}/original/p0.{ext}    原图（给 Telegra.ph）
    {cache_dir}/{pid}/tgphoto/p0.jpg       缩放/压缩后的 JPEG（给 TG sendPhoto）
    {cache_dir}/novel_{nid}/cover.jpg      小说封面
    {cache_dir}/novel_{nid}/embed_{id}.jpg 小说嵌入图

派生策略：
- TG sendPhoto 服务端会把图片限制在长边 ≤ 2560、≤ 10MB。如果原图超过这个尺寸，
  TG 会自己再压一次 → 二次有损。我们提前用 Pillow 缩放到 2560 + JPEG q=92，
  控制在 10MB 内，确保 TG 服务端不再做有损压缩。
- 透明通道：原图可能是 PNG 带透明，转 JPEG 必须先合成到白底，否则会变黑底。

并发：用 asyncio.Semaphore 限制同时下载数量，避免触发 Pixiv 限流。
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ...utils import logger
from .. import ProgressHook
from .api import PixivAPI

# TG 服务端当前压缩上限（2026 现状）：长边 ≤ 2560，文件 ≤ 10MB
TG_PHOTO_MAX_SIDE = 2560
TG_PHOTO_MAX_BYTES = 10 * 1024 * 1024


def _ext_from_url(url: str) -> str:
    """从图片 URL 末尾提取扩展名（含点）。i.pximg.net 的 URL 总是以扩展名结尾。"""
    name = url.rsplit("/", 1)[-1]
    if "." in name:
        return "." + name.rsplit(".", 1)[-1].lower().split("?")[0]
    return ".jpg"


@dataclass
class DownloadedImage:
    """一张已下载图片的本地路径信息。"""

    page_index: int            # 第几页（0 起）
    original_path: Path        # 原图本地路径
    tgphoto_path: Path         # 派生 JPEG 路径（用于 TG sendPhoto）
    original_url_remote: str   # Pixiv 原始 URL（用于日志/排错）


def _to_tg_photo(original_bytes: bytes, dest: Path) -> None:
    """把原图字节流派生为 TG sendPhoto 用的 JPEG。

    步骤：
    1. Pillow 打开
    2. 必要时缩放到长边 ≤ 2560
    3. 透明通道合成白底
    4. 输出 JPEG，质量 92 起步，超过 10MB 则迭代降质
    """
    img = Image.open(io.BytesIO(original_bytes))
    # 大图缩放
    if max(img.size) > TG_PHOTO_MAX_SIDE:
        img.thumbnail((TG_PHOTO_MAX_SIDE, TG_PHOTO_MAX_SIDE), Image.Resampling.LANCZOS)
    # 透明合成白底
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # 迭代质量直到 ≤ 10MB
    dest.parent.mkdir(parents=True, exist_ok=True)
    for q in (92, 88, 84, 80, 75, 70):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True, progressive=True)
        if buf.tell() <= TG_PHOTO_MAX_BYTES:
            dest.write_bytes(buf.getvalue())
            return
    # 兜底：极端情况下再缩一档
    img.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80, optimize=True, progressive=True)
    dest.write_bytes(buf.getvalue())


class PixivDownloader:
    """根据 IllustWork 下载所有图片到本地。"""

    def __init__(self, api: PixivAPI, cache_dir: str | Path, concurrency: int = 4):
        self.api = api
        self.cache_dir = Path(cache_dir)
        self._sem = asyncio.Semaphore(concurrency)

    # ------------------------------------------------------------------
    # Illust
    # ------------------------------------------------------------------

    async def download_illust(
        self,
        pid: str,
        original_urls: list[str],
        *,
        skip_existing: bool = True,
        on_progress: ProgressHook = None,
    ) -> list[DownloadedImage]:
        """并发下载一个作品的所有图片。

        original_urls 顺序即为页面顺序。返回的 DownloadedImage 保持同样顺序。
        on_progress(done, total) 在每张图（包括缓存命中）完成时调用。
        """
        work_dir = self.cache_dir / pid
        original_dir = work_dir / "original"
        tgphoto_dir = work_dir / "tgphoto"
        original_dir.mkdir(parents=True, exist_ok=True)
        tgphoto_dir.mkdir(parents=True, exist_ok=True)

        total = len(original_urls)
        done = 0
        done_lock = asyncio.Lock()

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
                logger.exception("pixiv download progress hook raised; suppressed")

        async def _one(idx: int, url: str) -> DownloadedImage:
            ext = _ext_from_url(url)
            original_path = original_dir / f"p{idx}{ext}"
            tgphoto_path = tgphoto_dir / f"p{idx}.jpg"

            # 命中缓存
            if skip_existing and original_path.exists() and tgphoto_path.exists():
                logger.debug(f"[{pid}] p{idx} cache hit")
                await _tick()
                return DownloadedImage(idx, original_path, tgphoto_path, url)

            async with self._sem:
                logger.debug(f"[{pid}] downloading p{idx}: {url}")
                data = await self.api.download_image(url)
            original_path.write_bytes(data)

            # 派生 JPEG（CPU 密集，扔到线程池避免阻塞事件循环）
            await asyncio.to_thread(_to_tg_photo, data, tgphoto_path)

            logger.debug(
                f"[{pid}] p{idx} done: original={original_path.stat().st_size}B "
                f"tgphoto={tgphoto_path.stat().st_size}B"
            )
            await _tick()
            return DownloadedImage(idx, original_path, tgphoto_path, url)

        tasks = [_one(i, url) for i, url in enumerate(original_urls)]
        results = await asyncio.gather(*tasks)
        return results

    # ------------------------------------------------------------------
    # Novel
    # ------------------------------------------------------------------

    async def download_novel_cover(self, nid: str, cover_url: str) -> Path:
        """下载小说封面。返回原图本地路径。"""
        if not cover_url:
            raise ValueError("empty cover url")
        work_dir = self.cache_dir / f"novel_{nid}"
        work_dir.mkdir(parents=True, exist_ok=True)
        ext = _ext_from_url(cover_url)
        path = work_dir / f"cover{ext}"
        if path.exists():
            return path
        async with self._sem:
            data = await self.api.download_image(cover_url)
        path.write_bytes(data)
        return path

    async def download_novel_embed(self, nid: str, image_id: str, url: str) -> Path:
        """下载小说正文嵌入的图片（uploadedimage 或 pixivimage 派生）。"""
        work_dir = self.cache_dir / f"novel_{nid}"
        work_dir.mkdir(parents=True, exist_ok=True)
        ext = _ext_from_url(url)
        path = work_dir / f"embed_{image_id}{ext}"
        if path.exists():
            return path
        async with self._sem:
            data = await self.api.download_image(url)
        path.write_bytes(data)
        return path


def relative_url(base_url: str, cache_dir: str | Path, local_path: Path) -> str:
    """把本地缓存路径转成对外可访问的 URL。

    base_url 是 Nginx 暴露 cache_dir 的前缀，尾斜杠会被自动剥除。
    """
    rel = local_path.resolve().relative_to(Path(cache_dir).resolve())
    return f"{base_url.rstrip('/')}/{rel.as_posix()}"


__all__ = [
    "PixivDownloader",
    "DownloadedImage",
    "relative_url",
    "TG_PHOTO_MAX_SIDE",
    "TG_PHOTO_MAX_BYTES",
]
