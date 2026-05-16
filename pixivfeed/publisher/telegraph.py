"""通用 Telegra.ph 发布器（与具体 Provider 解耦）。

职责：
1. Telegra.ph 账户生命周期：首次启动 createAccount 并写回 config.yaml
2. 把 GalleryWork（任意 Provider 的图集类作品）渲染成 node tree 发布
3. 多图作品超过 max_images_per_page 时拆多页，互链导航
4. （v0.8.0+）发布前把图片并发上传到 R2，给 Telegra.ph 喂 R2 URL，避免
   telegra.ph 页面靠 nginx + 7 天 cache_dir + CF 边缘缓存撑活

不在这里做的：
- pixiv novel 的小说排版（[chapter:] [newpage] [[jumpuri:]] 等自定义标记）
  → 留在 provider/pixiv/novel_publisher.py
- 图床上传（catbox 之类）
  → 用 R2 时 publisher 直接接管"对外可访问层"；不开 R2 时仍走 cache_dir +
    Nginx 反代方案（向后兼容）

R2 上传策略：
- enabled=true 时所有 publish_gallery 都尝试上传；成功用 R2 URL，单图失败回退
  到原 public_url（nginx），整批失败时整批回退。
- 上传只针对 work.images[].local_path（原图）；不上传 tg_photo_path（那是
  直发 TG sendPhoto 用的，不参与 telegra.ph）。
- 并发上传：默认 asyncio.gather 全部图同时 PUT，由 R2Client 内部 semaphore
  限流（upload_files_concurrent concurrency=8）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegraph.aio import Telegraph
from telegraph.utils import html_to_nodes

from ..config import Config
from ..provider import GalleryWork, ProgressHook, StatusUpdater
from ..storage import R2Client, upload_files_concurrent
from ..utils import logger

NodeTree = list[Any]


@dataclass
class PublishResult:
    """发布结果。多页作品 urls 含全部页面链接，第一页是入口。

    r2_skipped_reason 非空时表示 R2 上传被护栏跳过，调用方应该在完成消息后
    追加提示（"此 Telegra.ph 因体积过大未上传 R2..."）。
    """

    urls: list[str]
    page_count: int
    image_count: int
    r2_skipped_reason: str = ""    # "" 表示走了 R2 或 R2 未启用；非空表示护栏跳过

    @property
    def primary_url(self) -> str:
        return self.urls[0]


def render_template(template: str, vars: dict[str, Any]) -> str:
    """str.format 风格模板渲染。模板缺字段不致命，记录 warning 后返回原模板。"""
    if not template:
        return ""
    try:
        return template.format(**vars)
    except (KeyError, IndexError, ValueError) as e:
        logger.warning(f"template render failed: {e}; template={template!r}")
        return template


def html_to_nodes_safe(html: str) -> NodeTree:
    """telegraph.utils.html_to_nodes 的安全包装。空字符串返回空 list。"""
    if not html or not html.strip():
        return []
    try:
        return html_to_nodes(html)
    except Exception as e:
        logger.warning(f"html_to_nodes failed: {e}; falling back to plain text")
        return [{"tag": "p", "children": [html]}]


class TelegraphPublisher:
    """全局共享一个 Telegraph 客户端实例（access_token 长期有效）。

    一个实例就够整个 Bot 进程使用——Telegraph access_token 长期有效，
    所有 Provider 通过 publish_gallery() 共享这一份。
    """

    def __init__(self, config: Config, r2_client: R2Client | None = None):
        self.config = config
        self._tg = Telegraph(access_token=config.publish.telegraph_token or None)
        # R2 client：None 表示走 nginx fallback。建议由 channel 层装配，
        # 这里不主动创建（publisher 不直接读 R2 凭据，保持职责单一）。
        self._r2 = r2_client

    async def ensure_account(self) -> None:
        """如果 token 为空，自动 createAccount 并写回配置文件。"""
        if self.config.publish.telegraph_token:
            return
        logger.info("Telegra.ph token missing, creating account...")
        result = await self._tg.create_account(
            short_name=self.config.publish.telegraph_short_name,
            author_name=self.config.publish.telegraph_author_name,
            author_url=self.config.publish.telegraph_author_url or None,
        )
        token = result["access_token"]
        self.config.save_telegraph_token(token)
        # save_telegraph_token 已更新 self.config.publish.telegraph_token，
        # 但 Telegraph 客户端持有的 token 是构造时绑定的，需要重新初始化
        self._tg = Telegraph(access_token=token)
        logger.success(f"Telegra.ph account created: short_name={result.get('short_name')}")

    @property
    def tg(self) -> Telegraph:
        return self._tg

    # ------------------------------------------------------------------
    # 通用图集发布
    # ------------------------------------------------------------------

    async def publish_gallery(
        self,
        work: GalleryWork,
        *,
        page_title_template: str = "",
        page_header_template: str = "",
        page_footer_template: str = "",
        on_progress: ProgressHook = None,
        on_status: StatusUpdater = None,
        force_r2: bool = False,
    ) -> PublishResult:
        """发布一个 GalleryWork 到 Telegra.ph。

        模板由调用方传进来。Channel 在 bot_data 里持有 Config，
        知道每个 provider 配的是哪一组模板（pixiv 用 templates.illust，
        eh/ex/nh 共用 templates.gallery）。

        on_progress(done_pages, total_pages) 在每发完一页 Telegra.ph 后调用。
        单页作品只调一次终态；多页作品每页一调。

        on_status(text) 用于推 R2 上传阶段的状态文本（"⏳ 上传 R2 (12/25)"）。
        没启用 R2 / 没传 on_status 时跳过；publish 阶段仍走 on_progress。

        force_r2=True 时跳过 max_upload_size_gb 护栏（admin 用 --r2 flag 触发）。
        """
        await self.ensure_account()

        max_per_page = self.config.publish.max_images_per_page
        tvars = work.template_vars()

        title_str = render_template(page_title_template, tvars) or work.title or "(untitled)"
        if len(title_str) > 256:
            title_str = title_str[:253] + "..."

        urls, r2_skipped_reason = await self._resolve_image_urls(
            work, on_status=on_status, force_r2=force_r2,
        )
        if not urls:
            raise ValueError(f"{work.provider}/{work.work_id}: no images to publish")
        chunks = [urls[i : i + max_per_page] for i in range(0, len(urls), max_per_page)]

        async def _emit(done: int, total: int) -> None:
            if on_progress is None:
                return
            try:
                await on_progress(done, total)
            except Exception:
                logger.exception("telegraph publish progress hook raised; suppressed")

        if len(chunks) == 1:
            content = self._build_content(
                chunks[0], tvars,
                page_header_template, page_footer_template,
                chunk_index=0, total_chunks=1, next_url=None,
            )
            page = await self._tg.create_page(title=title_str, content=content, return_content=False)
            page_url = page["url"]
            logger.info(f"published {work.provider}[{work.work_id}] -> {page_url}")
            await _emit(1, 1)
            return PublishResult(
                urls=[page_url], page_count=1, image_count=len(urls),
                r2_skipped_reason=r2_skipped_reason,
            )

        # 多页：从最后一页往前发布，每页知道下一页 URL
        page_urls: list[str] = []
        next_url: str | None = None
        total_chunks = len(chunks)
        for i in range(total_chunks - 1, -1, -1):
            chunk_urls = chunks[i]
            content = self._build_content(
                chunk_urls, tvars,
                page_header_template, page_footer_template,
                chunk_index=i, total_chunks=total_chunks, next_url=next_url,
            )
            page_title_str = title_str if i == 0 else f"{title_str} ({i + 1}/{total_chunks})"
            if len(page_title_str) > 256:
                page_title_str = page_title_str[:253] + "..."
            page = await self._tg.create_page(title=page_title_str, content=content, return_content=False)
            page_urls.append(page["url"])
            next_url = page["url"]
            # done = 倒序里完成了几页 = total_chunks - i
            await _emit(total_chunks - i, total_chunks)

        page_urls.reverse()
        logger.info(
            f"published {work.provider}[{work.work_id}] across {total_chunks} pages, "
            f"primary={page_urls[0]}"
        )
        return PublishResult(
            urls=page_urls, page_count=total_chunks, image_count=len(urls),
            r2_skipped_reason=r2_skipped_reason,
        )

    async def _resolve_image_urls(
        self, work: GalleryWork, *, on_status: StatusUpdater = None,
        force_r2: bool = False,
    ) -> tuple[list[str], str]:
        """决定喂给 telegra.ph 的 <img src> URLs。

        返回 (urls, r2_skipped_reason)。r2_skipped_reason 非空时表示 R2 被
        护栏跳过，调用方应在完成消息后追加提示。

        - R2 启用且 client 注入了 + 未被护栏跳过：并发把 local_path 上传到 R2，
          用 R2 公开 URL
        - R2 未启用 / 上传失败 / 被护栏跳过：fallback 到 img.public_url（nginx）

        护栏规则：
          - storage.r2.max_upload_size_gb > 0 且本次发布 sum(local_path size)
            超过阈值 → 跳过 R2，r2_skipped_reason 设为说明文本
          - force_r2=True 时绕过护栏（admin --r2 flag）

        失败策略：单图失败 → 该图回 nginx URL；整图集失败 → 全回 nginx，
        发布仍能继续；这是关键的"R2 故障不阻塞发布"语义。

        on_status 用于在 R2 上传进度上 emit 状态文本（每完成一张 PUT）。
        """
        if self._r2 is None or not self.config.storage.r2.enabled:
            return ([img.public_url for img in work.images], "")

        # 计算本次发布总字节，判断护栏
        total_bytes = 0
        for img in work.images:
            try:
                total_bytes += img.local_path.stat().st_size
            except OSError:
                pass
        max_gb = self.config.storage.r2.max_upload_size_gb
        if not force_r2 and max_gb > 0 and total_bytes > max_gb * 1024 ** 3:
            reason = (
                f"本次发布总体积 {total_bytes / 1024**3:.2f} GB 超过 "
                f"max_upload_size_gb={max_gb} 阈值，未上传 R2"
            )
            logger.info(
                f"R2 size guard skipped for {work.provider}/{work.work_id}: {reason}"
            )
            return ([img.public_url for img in work.images], reason)

        cache_dir = Path(self.config.storage.cache_dir).resolve()
        items: list[tuple[str, Path]] = []      # (r2_key, local_path)
        fallback_urls: list[str] = [img.public_url for img in work.images]
        keys_per_image: list[str | None] = [None] * len(work.images)

        for idx, img in enumerate(work.images):
            # 优先用 GalleryImage 显式指定的 r2_key（zip2tph 这种 tmpdir 路径需要）
            if img.r2_key:
                key = img.r2_key
            else:
                # 没显式指定 → 尝试 local_path 相对 cache_dir 推导
                try:
                    rel = img.local_path.resolve().relative_to(cache_dir)
                except (ValueError, AttributeError):
                    # 不在 cache_dir 下且没显式 key → 跳过 R2 走 nginx fallback
                    continue
                key = rel.as_posix()
            keys_per_image[idx] = key
            items.append((key, img.local_path))

        if not items:
            return (fallback_urls, "")

        if on_status is not None:
            try:
                await on_status(f"⏳ 上传 R2 (0/{len(items)})")
            except Exception:
                logger.exception("on_status raised; suppressed")

        async def _r2_progress(done: int, total: int) -> None:
            if on_status is None:
                return
            try:
                await on_status(f"⏳ 上传 R2 ({done}/{total})")
            except Exception:
                logger.exception("on_status raised; suppressed")

        try:
            results = await upload_files_concurrent(
                self._r2, items, concurrency=8, on_progress=_r2_progress,
            )
        except Exception as e:
            logger.warning(
                f"R2 batch upload raised for {work.provider}/{work.work_id}: {e}; "
                "falling back to nginx URLs for entire gallery"
            )
            return (fallback_urls, "")

        # 合成最终 URL：上传成功 → R2 URL；失败 → fallback
        urls: list[str] = []
        ok_count = 0
        for idx, img in enumerate(work.images):
            key = keys_per_image[idx]
            if key is not None and results.get(key, False):
                urls.append(self._r2.public_url(key))
                ok_count += 1
            else:
                urls.append(img.public_url)
        logger.info(
            f"R2 upload for {work.provider}[{work.work_id}]: "
            f"{ok_count}/{len(work.images)} succeeded "
            f"({len(work.images) - ok_count} fell back to nginx)"
        )
        return (urls, "")

    @staticmethod
    def _build_content(
        image_urls: list[str],
        tvars: dict[str, Any],
        header_template: str,
        footer_template: str,
        *,
        chunk_index: int,
        total_chunks: int,
        next_url: str | None,
    ) -> NodeTree:
        nodes: NodeTree = []

        # 第一页才渲染 header；后续页只放简短续页提示
        if chunk_index == 0:
            # 原作品链接置于篇首（图集与漫画/插画通用）
            source_url = (tvars or {}).get("source_url") or ""
            if source_url:
                nodes.append(
                    {
                        "tag": "p",
                        "children": [
                            "原作品：",
                            {"tag": "a", "attrs": {"href": source_url}, "children": [source_url]},
                        ],
                    }
                )
            nodes.extend(html_to_nodes_safe(render_template(header_template, tvars)))
        else:
            nodes.append({"tag": "p", "children": [f"（续 {chunk_index + 1} / {total_chunks}）"]})

        # 图片（figure 包 img，避免 telegraph 把 img 处理成行内）
        for url in image_urls:
            nodes.append({"tag": "figure", "children": [{"tag": "img", "attrs": {"src": url}}]})

        # 第一页的 footer
        if chunk_index == 0:
            nodes.extend(html_to_nodes_safe(render_template(footer_template, tvars)))

        # 多页时附加导航
        if next_url:
            nodes.append(
                {
                    "tag": "p",
                    "children": [
                        {"tag": "a", "attrs": {"href": next_url}, "children": ["下一页 →"]}
                    ],
                }
            )

        return nodes


__all__ = [
    "TelegraphPublisher",
    "PublishResult",
    "render_template",
    "html_to_nodes_safe",
    "NodeTree",
]
