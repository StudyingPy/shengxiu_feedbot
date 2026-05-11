"""Pixiv 作品数据模型。

只挑模板/下载/发布会用到的字段。原始 AJAX 返回字段过百，全部映射没意义。
所有字段名跟模板占位符对齐，便于直接用 dataclasses.asdict 喂给 str.format。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IllustImageUrls:
    """单张图片的多档分辨率 URL（从 Pixiv AJAX 来，i.pximg.net 域名）。"""

    original: str  # 原图，可能是 png/jpg/gif
    regular: str   # 长边 1200，jpg
    small: str     # 长边 540，jpg
    thumb: str     # 长边 250，jpg


@dataclass
class IllustWork:
    """Pixiv 插画/漫画作品。"""

    pid: str
    title: str
    author: str
    user_id: str
    description: str
    create_date: str
    tags: list[str]
    page_count: int
    bookmark_count: int = 0
    like_count: int = 0
    view_count: int = 0
    x_restrict: int = 0          # 0=全年龄, 1=R-18, 2=R-18G
    ai_type: int = 0             # 0=非AI, 2=AI生成（1 已废弃）
    illust_type: int = 0         # 0=插画, 1=漫画, 2=动图
    images: list[IllustImageUrls] = field(default_factory=list)

    @property
    def is_ugoira(self) -> bool:
        return self.illust_type == 2

    @property
    def x_restrict_label(self) -> str:
        return {0: "", 1: "R-18", 2: "R-18G"}.get(self.x_restrict, "")

    @property
    def ai_type_label(self) -> str:
        return "AI生成" if self.ai_type == 2 else ""

    def template_vars(self) -> dict[str, object]:
        """供模板 str.format 使用的扁平变量字典。"""
        return {
            "pid": self.pid,
            "title": self.title,
            "author": self.author,
            "user_id": self.user_id,
            "description": self.description,
            "create_date": self.create_date,
            "tags": " ".join(f"#{t}" for t in self.tags),
            "page_count": self.page_count,
            "bookmark_count": self.bookmark_count,
            "like_count": self.like_count,
            "view_count": self.view_count,
            "x_restrict": self.x_restrict_label,
            "ai_type": self.ai_type_label,
        }


@dataclass
class NovelEmbeddedImage:
    """小说正文嵌入的图片。

    novel_image_id 是 [uploadedimage:xxx] 标记里的 xxx，对应 textEmbeddedImages 字典的 key。
    illust_id 是 [pixivimage:xxx] 标记里的 xxx，对应一个独立 illust 作品。
    两者只会有一个非 None。
    """

    novel_image_id: str | None
    illust_id: str | None
    url: str  # 已经处理好的可直接给 Telegra.ph 的 URL


@dataclass
class NovelWork:
    """Pixiv 小说作品。"""

    nid: str
    title: str
    author: str
    user_id: str
    description: str
    create_date: str
    tags: list[str]
    text_length: int
    cover_url: str           # 封面（已处理为可直接发布的 URL）
    content: str             # 小说正文（带 Pixiv 自定义标记）
    series_id: str | None = None
    series_title: str | None = None

    def template_vars(self) -> dict[str, object]:
        # caption_short 取 description 前 100 字
        short = (self.description or "").replace("\n", " ").strip()
        if len(short) > 100:
            short = short[:100] + "…"
        return {
            "nid": self.nid,
            "title": self.title,
            "author": self.author,
            "user_id": self.user_id,
            "description": self.description,
            "caption_short": short,
            "create_date": self.create_date,
            "tags": " ".join(f"#{t}" for t in self.tags),
            "text_length": self.text_length,
            "series_title": self.series_title or "",
        }


__all__ = ["IllustWork", "IllustImageUrls", "NovelWork", "NovelEmbeddedImage"]
