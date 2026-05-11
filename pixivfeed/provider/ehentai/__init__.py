"""e-hentai / exhentai Provider，支持四种抓取模式。

模式定义见 ./_modes.py：page_sample / page_original / archive_resample / archive_original
archive 流水线见 ./_archive.py

URL 形式：
    https://e-hentai.org/g/{gid}/{token}
    https://exhentai.org/g/{gid}/{token}

ParsedRef.id 使用 'gid/token' 复合 key，handler 拿到后可以选择不同 mode 调
fetch_and_download_with_mode(ref, mode)。

公共契约 fetch_and_download(ref) 走该 Provider 的 default_mode（由 config 提供）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from ...utils import logger
from .. import GalleryImage, GalleryWork, ParsedRef, ProgressHook, Provider, StatusUpdater
from .._common import (
    download_to_file,
    make_http_client,
    relative_url,
    safe_ext_from_url,
)
from ._archive import (
    ArchiveError,
    ArchiveLockedError,
    compute_archive_timeout,
    download_archive_with_timeout,
    extract_archive,
    fetch_archiver_token,
    request_archive,
)
from ._modes import BASE_HEADERS, EHMode


class EHError(Exception):
    """e-hentai/exhentai 解析或下载失败的统一类型。"""


class EHGalleryUnavailable(EHError):
    """画廊在当前 host 上不可用（404、被搬到 ex、需要更高权限等）。

    handler 拿到这个异常时可以考虑 fallback：在 e-hentai 上不可用 → 试试 exhentai。
    """


@dataclass
class EHGallery:
    """画廊元数据。fetch_meta 阶段就拿全的字段。"""

    host: str
    gallery_id: str
    token: str
    title: str
    page_count: int                # 总页数（从主页第一页 HTML 抠到）
    image_page_urls: list[str]     # 子页 URL（page_sample / page_original 用）


# ---------------------------------------------------------------------------
# URL/HTML 解析常量（DojinGo 移植 + 扩展）
# ---------------------------------------------------------------------------

_EH_TITLE_RE = re.compile(r'<h1 id="gn">(.*?)</h1>')
# 主页 HTML 里的 "X pages" 文本
_EH_PAGE_COUNT_RE = re.compile(r"<td class=\"gdt2\">(\d+)\s*pages?</td>", re.IGNORECASE)
_EH_IMAGE_RE = re.compile(r'<img id="img" src="(.*?)"')
# 子页里 "Download original X x Y" 链接
_EH_FULLIMG_RE = re.compile(
    r'<a href="(https?://[^"]+/fullimg(?:\.php)?\?[^"]+)"',
    re.IGNORECASE,
)
# 画廊不可用的几类错误文案。eh 通常返回 200 + 错误页，需要识别这些文案
# 才能把"真正 404"和"网络抖动"区分开。
_EH_UNAVAILABLE_RE = re.compile(
    r"(Gallery (Not Available|not found|removed)|"
    r"This gallery has been removed|"
    r"Key missing, or incorrect key provided|"
    r"You are not allowed to view this gallery|"
    r"This gallery is unavailable due to a copyright claim)",
    re.IGNORECASE,
)


def _build_subpage_re(host: str) -> re.Pattern:
    return re.compile(rf'<a href="(https://{re.escape(host)}/s/\w+/[\w-]+)">')


def _next_page_marker(album_url: str, next_page: int) -> str:
    return f'<a href="{album_url}/?p={next_page}" onclick="return false">'


def _normalize_gallery_url(host: str, raw_url: str) -> tuple[str, str, str]:
    raw = raw_url.strip().rstrip("/")
    for prefix in (f"https://{host}", f"http://{host}"):
        if raw.startswith(prefix):
            path = raw[len(prefix):]
            break
    else:
        raise EHError(f"unrecognized eh url: {raw_url!r}")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 3 or parts[0] != "g":
        raise EHError(f"invalid eh gallery path: {raw_url!r}")
    return f"https://{host}/g/{parts[1]}/{parts[2]}", parts[1], parts[2]


# ---------------------------------------------------------------------------
# 主 Provider
# ---------------------------------------------------------------------------


class _EHFamilyProvider(Provider):
    """e-hentai 与 exhentai 共享的实现基类。"""

    HOST: str = ""

    def __init__(
        self,
        cache_dir: str | Path,
        public_base_url: str,
        *,
        config,                       # Config 实例，运行时读 default_mode 等
    ):
        self.cache_dir = Path(cache_dir)
        self.public_base_url = public_base_url.rstrip("/")
        self.config = config

    @property
    def name(self) -> str:
        return self.HOST  # 'e-hentai.org' 或 'exhentai.org'

    def _cookies_for(self, mode: EHMode | None) -> dict[str, str]:
        """根据 mode 决定使用的 cookie。

        基类只放 nw=1。子类按需 override 加 ipb_pass_hash 等登录 cookie。

        在 e-hentai 上：
          - PAGE_SAMPLE：纯公开数据，nw=1 即可
          - 其他模式：需要登录态（消耗 GP/Credits 或免费 archive 配额），
            借用 exhentai 的 cookie——同一个账户用 e-hentai 域名也认。
        在 exhentai 上：所有 mode 都需要登录 cookie。
        """
        return {"nw": "1"}

    @property
    def _site_url_re(self) -> re.Pattern:
        return re.compile(
            rf"https?://{re.escape(self.HOST)}/g/\w+/[\w-]+",
            re.IGNORECASE,
        )

    @property
    def _collector_cfg(self):
        """从 self.config 读对应的 ehentai/exhentai 子配置。"""
        if self.HOST == "e-hentai.org":
            return self.config.collectors.ehentai
        return self.config.collectors.exhentai

    @property
    def _shared_cfg(self):
        return self.config.collectors

    @property
    def default_mode(self) -> EHMode:
        return EHMode(self._collector_cfg.default_mode)

    @property
    def archive_timeout(self) -> int:
        return self._collector_cfg.archive_timeout

    def can_handle(self, text: str) -> bool:
        if not self._collector_cfg.enabled:
            return False
        return bool(self._site_url_re.search(text))

    def extract_refs(self, text: str) -> list[ParsedRef]:
        seen: set[str] = set()
        refs: list[ParsedRef] = []
        for m in self._site_url_re.finditer(text):
            url = m.group(0)
            try:
                _, gid, token = _normalize_gallery_url(self.HOST, url)
            except EHError:
                continue
            key = f"{gid}/{token}"
            if key in seen:
                continue
            seen.add(key)
            refs.append(ParsedRef(provider=self.name, kind="gallery", id=key, raw=url))
        return refs

    # ------------------------------------------------------------------
    # 公共契约：fetch_work 拉 meta，fetch_and_download 用默认模式抓
    # ------------------------------------------------------------------

    async def fetch_work(self, ref: ParsedRef) -> EHGallery:
        gid, token = ref.id.split("/", 1)
        return await self._fetch_gallery_meta(gid, token)

    async def fetch_and_download(
        self, ref: ParsedRef, *, on_progress: ProgressHook = None
    ) -> GalleryWork:
        return await self.fetch_and_download_with_mode(
            ref, self.default_mode, on_progress=on_progress,
        )

    async def fetch_and_download_with_mode(
        self,
        ref: ParsedRef,
        mode: EHMode,
        *,
        on_progress: ProgressHook = None,
        on_status: StatusUpdater = None,
    ) -> GalleryWork:
        """供按钮交互层调用：明确指定模式。

        - PAGE_*：`on_progress` 是 item hook（按图片张数）。
        - ARCHIVE_*：`on_status` 优先（富文本带 `[N线程]` 后缀、动态超时已自动算），
          没传时 fallback 到 `on_progress`（bytes hook，无后缀）。
        """
        gid, token = ref.id.split("/", 1)
        gallery = await self._fetch_gallery_meta(gid, token)

        # 针对每个 mode 单独建子目录，避免不同分辨率串图
        work_dir = self.cache_dir / f"{self._cache_prefix()}_{gid}_{token}_{mode.value}"
        work_dir.mkdir(parents=True, exist_ok=True)

        async with self._make_client(mode) as client:
            if mode == EHMode.PAGE_SAMPLE:
                image_urls = await self._extract_page_image_urls(
                    client, gallery.image_page_urls, full=False,
                )
                local_paths = await self._download_direct(
                    client, image_urls, work_dir, on_progress=on_progress,
                )
            elif mode == EHMode.PAGE_ORIGINAL:
                image_urls = await self._extract_page_image_urls(
                    client, gallery.image_page_urls, full=True,
                )
                local_paths = await self._download_direct(
                    client, image_urls, work_dir, on_progress=on_progress,
                )
            else:  # archive
                local_paths = await self._archive_pipeline(
                    client, gallery, mode, work_dir,
                    on_progress=on_progress, on_status=on_status,
                )

        images = [
            GalleryImage(
                page_index=i,
                local_path=p,
                public_url=relative_url(self.public_base_url, self.cache_dir, p),
            )
            for i, p in enumerate(local_paths)
        ]

        return GalleryWork(
            provider=self.name,
            kind="gallery",
            work_id=ref.id,
            source_url=f"https://{self.HOST}/g/{gid}/{token}",
            title=gallery.title,
            author="",
            images=images,
            extra_vars={
                "gallery_id": gid,
                "token": token,
                "host": self.HOST,
                "mode": mode.value,
            },
        )

    # ------------------------------------------------------------------
    # 内部：HTTP client 与画廊主页解析
    # ------------------------------------------------------------------

    def _make_client(self, mode: EHMode | None = None) -> httpx.AsyncClient:
        return make_http_client(
            headers=BASE_HEADERS,
            cookies=self._cookies_for(mode),
            timeout=self._shared_cfg.timeout,
        )

    def _cache_prefix(self) -> str:
        return "eh" if self.HOST == "e-hentai.org" else "ex"

    async def _fetch_gallery_meta(self, gid: str, token: str) -> EHGallery:
        album_url = f"https://{self.HOST}/g/{gid}/{token}"
        async with self._make_client() as client:
            pages_html = await self._paged_fetch(client, album_url)

        if not pages_html:
            raise EHError(f"{album_url}: no pages fetched")

        # 错误页识别（eh 常 200 返回错误文案）
        first_page = pages_html[0]
        if err := _EH_UNAVAILABLE_RE.search(first_page):
            raise EHGalleryUnavailable(
                f"{self.HOST}: gallery {gid}/{token} unavailable ({err.group(0)[:60]})"
            )

        title_match = _EH_TITLE_RE.search(first_page)
        title = title_match.group(1) if title_match else f"{self.HOST}-{gid}"

        page_count_match = _EH_PAGE_COUNT_RE.search(first_page)
        page_count = int(page_count_match.group(1)) if page_count_match else 0

        subpage_re = _build_subpage_re(self.HOST)
        subpages: list[str] = []
        seen: set[str] = set()
        for html in pages_html:
            for m in subpage_re.finditer(html):
                u = m.group(1)
                if u not in seen:
                    seen.add(u)
                    subpages.append(u)

        if not subpages:
            # 没匹到错误文案但也没子页：当成 unavailable 处理（可能是没识别到的新文案）
            raise EHGalleryUnavailable(
                f"{album_url}: no image subpages found (gallery may be deleted, "
                "moved to exhentai, or require login)"
            )

        if page_count == 0:
            page_count = len(subpages)

        return EHGallery(
            host=self.HOST,
            gallery_id=gid,
            token=token,
            title=title,
            page_count=page_count,
            image_page_urls=subpages,
        )

    @staticmethod
    async def _paged_fetch(client: httpx.AsyncClient, album_url: str) -> list[str]:
        pages: list[str] = []
        for page in range(0, 200):
            url = f"{album_url}/?p={page}"
            resp = await client.get(url)
            if resp.status_code == 404:
                raise EHGalleryUnavailable(f"GET {url} returned HTTP 404")
            if resp.status_code != 200:
                raise EHError(f"GET {url} returned HTTP {resp.status_code}")
            html = resp.text
            pages.append(html)
            if _next_page_marker(album_url, page + 1) not in html:
                break
        else:
            logger.warning(f"{album_url}: paged_fetch hit safety limit (200 pages)")
        return pages

    # ------------------------------------------------------------------
    # 模式 1 & 2：网页逐页抓取
    # ------------------------------------------------------------------

    async def _extract_page_image_urls(
        self,
        client: httpx.AsyncClient,
        subpages: list[str],
        *,
        full: bool,
    ) -> list[str]:
        """对每个子页 GET → 抠图片 URL。

        full=False: <img id="img"> 的 sample URL
        full=True : "Download original X x Y" 链接（302 后才是真原图，但 httpx 跟 302）
        """
        from asyncio import Semaphore, gather
        sem = Semaphore(self._shared_cfg.download_concurrency)

        async def _one(sub_url: str) -> str:
            async with sem:
                last_exc: Exception | None = None
                for attempt in range(3):
                    try:
                        resp = await client.get(sub_url)
                        if resp.status_code != 200:
                            raise EHError(f"subpage {sub_url} HTTP {resp.status_code}")
                        text = resp.text
                        if full:
                            m = _EH_FULLIMG_RE.search(text)
                            if m:
                                return m.group(1)
                            # 没有 fullimg 链接 = 用户没买原图权限或不让下；fallback 到 sample
                            logger.debug(
                                f"{sub_url}: no 'Download original' link, fallback to sample"
                            )
                        m_sample = _EH_IMAGE_RE.search(text)
                        if not m_sample:
                            raise EHError(f"subpage {sub_url}: image src not found")
                        return m_sample.group(1)
                    except Exception as e:
                        last_exc = e
                assert last_exc is not None
                raise last_exc

        return list(await gather(*(_one(u) for u in subpages)))

    async def _download_direct(
        self,
        client: httpx.AsyncClient,
        urls: list[str],
        work_dir: Path,
        *,
        on_progress: ProgressHook = None,
    ) -> list[Path]:
        """直接下载图片到本地。每张图按 idx 命名。"""
        from asyncio import Lock, Semaphore, gather
        sem = Semaphore(self._shared_cfg.download_concurrency)

        dests: list[Path] = []
        for idx, url in enumerate(urls):
            ext = safe_ext_from_url(url, default=".jpg")
            dests.append(work_dir / f"p{idx}{ext}")

        total = len(urls)
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
                logger.exception("eh/ex download progress hook raised; suppressed")

        async def _one(url: str, dest: Path) -> None:
            async with sem:
                # PAGE_ORIGINAL 有时会拿到 fullimg.php 那种带 query 的 URL
                # 服务端 302 到真正的图片，httpx follow_redirects=True 默认开了
                await download_to_file(client, url, dest, retries=3)
            await _tick()

        await gather(*(_one(u, d) for u, d in zip(urls, dests)))
        return dests

    # ------------------------------------------------------------------
    # 模式 3 & 4：archive 流水线
    # ------------------------------------------------------------------

    async def _archive_pipeline(
        self,
        client: httpx.AsyncClient,
        gallery: EHGallery,
        mode: EHMode,
        work_dir: Path,
        *,
        on_progress: ProgressHook = None,
        on_status: StatusUpdater = None,
    ) -> list[Path]:
        """走 archiver.php 拿 zip → 解压。

        - 用 `request_archive` 返回的预估字节数计算动态超时（5min + 5s/MB，封顶 1h），
          避免大画廊（>500MB）固定 300s timeout 死循环；config 的 archive_timeout
          作为下限，用户调高也尊重。
        - on_status / on_progress 透传给 `download_archive_with_timeout`。
        """
        album_url = f"https://{self.HOST}/g/{gallery.gallery_id}/{gallery.token}"
        try:
            archiver_token = await fetch_archiver_token(client, album_url)
            zip_url, estimated, _gp = await request_archive(
                client,
                self.HOST,
                gallery.gallery_id,
                gallery.token,
                archiver_token,
                mode,
            )
            timeout = compute_archive_timeout(self.archive_timeout, estimated)
            if estimated > 0:
                logger.info(
                    f"[{self.HOST}/{gallery.gallery_id}] zip url obtained "
                    f"(estimated {estimated} bytes, timeout {timeout}s), downloading..."
                )
            else:
                logger.info(
                    f"[{self.HOST}/{gallery.gallery_id}] zip url obtained "
                    f"(estimated size unknown, timeout {timeout}s), downloading..."
                )

            zip_path = work_dir / "archive.zip"
            await download_archive_with_timeout(
                client, zip_url, zip_path, timeout,
                on_progress=on_progress, on_status=on_status,
            )

            extract = extract_archive(zip_path, work_dir)
            # 删 zip（节省空间，按你之前的选择）
            try:
                zip_path.unlink()
            except OSError:
                pass

            return extract.image_paths
        except ArchiveLockedError as e:
            # session 已被锁；底层已自动 invalidate，让消息流上层用户看到提示而不是
            # "archive download failed" 的笼统错误。
            raise EHError(
                "archive session 已被锁定（多 IP 滥用风控），"
                "已自动取消旧链接，请稍后重新提交本画廊"
            ) from e
        except ArchiveError as e:
            raise EHError(f"archive download failed: {e}") from e


# ---------------------------------------------------------------------------
# 具体子类
# ---------------------------------------------------------------------------


class EHentaiProvider(_EHFamilyProvider):
    HOST = "e-hentai.org"

    def _cookies_for(self, mode: EHMode | None) -> dict[str, str]:
        """e-hentai：

        - 任何阶段（包括 meta、PAGE_SAMPLE）：如果用户配了 ex cookie，都带上。
          原因：部分画廊（年龄分类、特殊 tag）即便在 e-hentai 域名下也需要登录态
          才能访问，未登录会被 "Gallery Not Available" 错误页拦截，被误报为 404。
          E-Hentai 和 ExHentai 共用账户，ex cookie 在 e-hentai 域名也认。
        - 没配 ex cookie 时仅放 nw=1，公开画廊正常工作；受限画廊会自然失败 →
          上层 handler 会 fallback 到 exhentai 重试。
        """
        cookies = {"nw": "1"}
        ex_cfg = self.config.collectors.exhentai
        if ex_cfg.ipb_pass_hash and ex_cfg.ipb_member_id and ex_cfg.igneous:
            cookies["ipb_pass_hash"] = ex_cfg.ipb_pass_hash
            cookies["ipb_member_id"] = ex_cfg.ipb_member_id
            cookies["igneous"] = ex_cfg.igneous
        return cookies


class ExHentaiProvider(_EHFamilyProvider):
    HOST = "exhentai.org"

    def _cookies_for(self, mode: EHMode | None) -> dict[str, str]:
        """exhentai：所有 mode 都需要登录 cookie。"""
        cfg = self.config.collectors.exhentai
        cookies = {"nw": "1"}
        if cfg.ipb_pass_hash and cfg.ipb_member_id and cfg.igneous:
            cookies["ipb_pass_hash"] = cfg.ipb_pass_hash
            cookies["ipb_member_id"] = cfg.ipb_member_id
            cookies["igneous"] = cfg.igneous
        return cookies


__all__ = [
    "EHentaiProvider",
    "ExHentaiProvider",
    "EHGallery",
    "EHError",
    "EHGalleryUnavailable",
    "EHMode",
]
