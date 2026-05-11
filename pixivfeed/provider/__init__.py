"""Provider 抽象层。

设计目标：
- 让所有数据源（pixiv / e-hentai / exhentai / nhentai）共用同一套 Channel/Publisher 调用契约
- 但 *不* 把 pixiv 的细节强行抽象到顶层。pixiv 有 illust / novel 两种产物，
  其它站点只有"画廊"（一组图片），强行统一会产生大量空字段。
  所以这里 GalleryWork 是「图集类作品」的最大公约数；pixiv 的 IllustWork / NovelWork
  仍保留在 provider/pixiv 下，但实现 to_gallery() 把自己降维成 GalleryWork
  喂给通用 publisher。

Provider 契约：
    can_handle(text)        —— 文本里是否含本 Provider 能处理的链接
    extract_refs(text)      —— 提取所有 ref（kind + id + raw 片段）
    fetch_work(ref)         —— 拉元数据（不下载图片），返回类型自定
    fetch_and_download(ref) —— 完整流程，返回 GalleryWork

参考实现：
    biliparser 的 Provider/ProviderRegistry 形状（自动 URL 分流）
    DojinGo 的 Loader 闭包（延迟加载下载）—— 我们这里直接 download() 一把过
    因为 publisher 阶段不需要边下边发。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ProgressHook = Callable[[int, int], Awaitable[None]] | None
"""统一进度回调协议：`await hook(done, total)`。

调用方约定单位：
- item hook：`done` / `total` 是"已完成项数 / 总项数"（图片张数、telegraph 页数等）
- bytes hook：`done` / `total` 是"已下载字节 / 总字节"

Provider 层不感知这两种语义，只负责按真实进展调 hook；具体的文案/格式
由 channel 层（通过 make_item_hook / make_bytes_hook 工厂）决定。

设为 `None` 表示禁用进度回调，downloader 应跳过调用而不是传 0。"""


StatusUpdater = Callable[[str], Awaitable[None]] | None
"""阶段性文本更新协议：`await on_status("⏳ 等待 H@H 节点启动 ...")`。

用于较长流程内部需要把"我现在在做什么"告诉用户的场景（H@H 节点等待、
切换备用链接等）。和 ProgressHook 互补：ProgressHook 推数字进度，
StatusUpdater 推阶段文案。

Channel 层可以直接把 `Progress.update` 绑定方法当作 StatusUpdater 传入。
设为 `None` 时被调用方跳过文案推送。"""


# ---------------------------------------------------------------------------
# 通用数据模型
# ---------------------------------------------------------------------------


@dataclass
class ParsedRef:
    """从一段文本里提取出来的一个作品引用。

    kind 由各 Provider 自行约定。pixiv 用 'illust' / 'novel'，
    eh/ex/nh 都只有 'gallery'。
    """

    provider: str   # PixivProvider.name 等
    kind: str
    id: str
    raw: str        # 用户原始输入片段，便于排错


@dataclass
class GalleryImage:
    """已下载到本地的一张图片。"""

    page_index: int
    local_path: Path           # 原图本地缓存路径
    public_url: str            # 通过 base_url 拼出来的对外 URL，喂给 Telegra.ph
    # tg_photo 是缩放/压缩到 TG sendPhoto 上限的派生 JPEG。
    # 不是所有 Provider 都需要它（多数图集站只走 telegraph）。
    tg_photo_path: Path | None = None


@dataclass
class GalleryWork:
    """图集类作品的最大公约数模型，供通用 telegraph publisher 消费。

    pixiv 的 IllustWork、eh/ex/nh 的画廊都能降维到这里。
    模板渲染用 template_vars()，由具体 Provider 决定提供哪些字段。
    """

    provider: str               # 'pixiv' / 'e-hentai' / ...
    kind: str                   # 'illust' / 'gallery'
    work_id: str                # PID / Gallery ID（含 token）
    source_url: str             # 用户能看到原作的 URL，发到 footer
    title: str
    author: str = ""
    images: list[GalleryImage] = field(default_factory=list)
    # 原始作品自定义字段，模板用 {tags} {description} {x_restrict} 等可以拿到
    extra_vars: dict[str, Any] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.images)

    def template_vars(self) -> dict[str, Any]:
        base = {
            "provider": self.provider,
            "work_id": self.work_id,
            "source_url": self.source_url,
            "title": self.title,
            "author": self.author,
            "page_count": self.page_count,
        }
        # extra_vars 可以覆盖 base 里的同名键（比如 pixiv 想让 {pid} 别名）
        base.update(self.extra_vars)
        return base


# ---------------------------------------------------------------------------
# Provider 基类
# ---------------------------------------------------------------------------


class Provider(ABC):
    """所有数据源 Provider 的基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider 名称。用于路由、日志、缓存 key。"""

    @abstractmethod
    def can_handle(self, text: str) -> bool:
        """文本里是否含本 Provider 能处理的链接。"""

    @abstractmethod
    def extract_refs(self, text: str) -> list[ParsedRef]:
        """从文本提取所有作品引用（按出现顺序去重）。"""

    @abstractmethod
    async def fetch_work(self, ref: ParsedRef) -> Any:
        """拉元数据（不下载图片）。

        返回类型由 Provider 自定。pixiv 返回 IllustWork / NovelWork，
        其它返回站点专属 dataclass 或直接 GalleryWork（图片字段为空）。
        """

    @abstractmethod
    async def fetch_and_download(
        self, ref: ParsedRef, *, on_progress: ProgressHook = None
    ) -> GalleryWork:
        """完整流程：拉元数据 + 下载图片到 cache_dir，返回 GalleryWork。

        on_progress 是可选的进度回调（见 ProgressHook 文档）。各 Provider
        按各自的语义调用：eh/ex/nh/pixiv illust 都按"图片张数"调；
        eh/ex archive 模式按"已下载字节数"调（在 _archive.py 流式循环里）。

        eh/ex/nh 直接实现就够。pixiv 对 illust 实现，
        novel 走专属 publish_novel 路径，不经过这里。
        """


class ProviderRegistry:
    """注册表 + URL 路由。"""

    def __init__(self) -> None:
        self._providers: list[Provider] = []

    def register(self, provider: Provider) -> None:
        self._providers.append(provider)

    def find(self, text: str) -> Provider | None:
        for p in self._providers:
            if p.can_handle(text):
                return p
        return None

    def find_by_name(self, name: str) -> Provider | None:
        for p in self._providers:
            if p.name == name:
                return p
        return None

    def all(self) -> list[Provider]:
        return list(self._providers)

    def extract_all_refs(self, text: str) -> list[ParsedRef]:
        """对一段文本里所有 Provider 的链接都做提取，按文本里的位置排序。

        群消息可能混杂多个站点的链接，这里一次性都拿出来交给 handler 顺序处理。
        """
        refs: list[ParsedRef] = []
        for p in self._providers:
            refs.extend(p.extract_refs(text))

        def _pos(r: ParsedRef) -> int:
            i = text.find(r.raw)
            return i if i >= 0 else 0

        refs.sort(key=_pos)
        return refs


__all__ = [
    "Provider",
    "ProviderRegistry",
    "ProgressHook",
    "StatusUpdater",
    "ParsedRef",
    "GalleryImage",
    "GalleryWork",
]
