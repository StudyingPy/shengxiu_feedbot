"""通用 Telegra.ph 发布器（与具体 Provider 解耦）。

职责：
1. Telegra.ph 账户生命周期：首次启动 createAccount 并写回 config.yaml
2. 把 GalleryWork（任意 Provider 的图集类作品）渲染成 node tree 发布
3. 多图作品超过 max_images_per_page 时拆多页，互链导航

不在这里做的：
- pixiv novel 的小说排版（[chapter:] [newpage] [[jumpuri:]] 等自定义标记）
  → 留在 provider/pixiv/novel_publisher.py
- 图床上传（catbox 之类）
  → 我们坚持本地缓存 + Nginx 反代方案，所有 Provider 在 download() 阶段把
    图片落到 cache_dir，并把 base_url 拼出的 public_url 写进 GalleryImage
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telegraph.aio import Telegraph
from telegraph.utils import html_to_nodes

from ..config import Config
from ..provider import GalleryWork
from ..utils import logger


NodeTree = list[Any]


@dataclass
class PublishResult:
    """发布结果。多页作品 urls 含全部页面链接，第一页是入口。"""

    urls: list[str]
    page_count: int
    image_count: int

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

    def __init__(self, config: Config):
        self.config = config
        self._tg = Telegraph(access_token=config.publish.telegraph_token or None)

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
    ) -> PublishResult:
        """发布一个 GalleryWork 到 Telegra.ph。

        模板由调用方传进来。Channel 在 bot_data 里持有 Config，
        知道每个 provider 配的是哪一组模板（pixiv 用 templates.illust，
        eh/ex/nh 共用 templates.gallery）。
        """
        await self.ensure_account()

        max_per_page = self.config.publish.max_images_per_page
        tvars = work.template_vars()

        title_str = render_template(page_title_template, tvars) or work.title or "(untitled)"
        if len(title_str) > 256:
            title_str = title_str[:253] + "..."

        urls = [img.public_url for img in work.images]
        if not urls:
            raise ValueError(f"{work.provider}/{work.work_id}: no images to publish")
        chunks = [urls[i : i + max_per_page] for i in range(0, len(urls), max_per_page)]

        if len(chunks) == 1:
            content = self._build_content(
                chunks[0], tvars,
                page_header_template, page_footer_template,
                chunk_index=0, total_chunks=1, next_url=None,
            )
            page = await self._tg.create_page(title=title_str, content=content, return_content=False)
            page_url = page["url"]
            logger.info(f"published {work.provider}[{work.work_id}] -> {page_url}")
            return PublishResult(urls=[page_url], page_count=1, image_count=len(urls))

        # 多页：从最后一页往前发布，每页知道下一页 URL
        page_urls: list[str] = []
        next_url: str | None = None
        for i in range(len(chunks) - 1, -1, -1):
            chunk_urls = chunks[i]
            content = self._build_content(
                chunk_urls, tvars,
                page_header_template, page_footer_template,
                chunk_index=i, total_chunks=len(chunks), next_url=next_url,
            )
            page_title_str = title_str if i == 0 else f"{title_str} ({i + 1}/{len(chunks)})"
            if len(page_title_str) > 256:
                page_title_str = page_title_str[:253] + "..."
            page = await self._tg.create_page(title=page_title_str, content=content, return_content=False)
            page_urls.append(page["url"])
            next_url = page["url"]

        page_urls.reverse()
        logger.info(
            f"published {work.provider}[{work.work_id}] across {len(chunks)} pages, "
            f"primary={page_urls[0]}"
        )
        return PublishResult(urls=page_urls, page_count=len(chunks), image_count=len(urls))

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
