"""配置加载。

四层优先级（高到低）：
    1. SQLite runtime_settings 表       —— admin 私聊 /setting set 写的
    2. 环境变量                          —— 容器部署常用
    3. config.yaml                       —— 主要配置来源
    4. dataclass 默认值                  —— fallback

启动流程：
    cfg = Config.load(path)              # 完成 1~4 的合并（runtime 那层在 db 还没接前是空）
    cfg.bind_runtime(runtime_settings)   # 把 db 里的 runtime 设置覆盖进 dataclass
    # 之后 /setting set 会同时写 db + 改 dataclass

设计上的"运行时可改"字段必须在 RUNTIME_KEYS 里登记。没登记的（如 telegram.token）
不能通过 /setting 改，只能改 yaml 重启。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# 子配置 dataclass
# ---------------------------------------------------------------------------


@dataclass
class TelegramConfig:
    token: str = ""
    base_url: str = ""
    # 本地 Bot API 文件下载根 URL（带 /file/bot 前缀的话由 PTB 内部再拼 token）。
    # 留空 + base_url 已设时，PTB 默认猜测 base_url + "/file"；多数情况下这就够。
    base_file_url: str = ""
    # 是否告诉 PTB 走 local_mode：True 时 getFile 返回的本地路径会被 PTB 直接读，
    # 不再走 HTTPS 拉一遍，从而绕开"File is too big"（20MB）的限制。
    # 仅当 base_url 指向你自己的 telegram-bot-api 实例时才打开。
    local_mode: bool = False


@dataclass
class AuthConfig:
    admin_users: list[int] = field(default_factory=list)
    initial_allowed_users: list[int] = field(default_factory=list)
    initial_allowed_chats: list[int] = field(default_factory=list)


@dataclass
class PixivConfig:
    phpsessid: str = ""
    timeout: int = 30
    download_concurrency: int = 4


@dataclass
class EHentaiCollectorConfig:
    enabled: bool = False
    default_mode: str = "page_sample"   # page_sample / page_original / archive_resample / archive_original
    archive_timeout: int = 300


@dataclass
class ExHentaiCollectorConfig:
    enabled: bool = False
    default_mode: str = "page_sample"
    archive_timeout: int = 300
    ipb_pass_hash: str = ""
    ipb_member_id: str = ""
    igneous: str = ""


@dataclass
class NHentaiCollectorConfig:
    enabled: bool = False


@dataclass
class CollectorsConfig:
    timeout: int = 30
    download_concurrency: int = 4
    ehentai: EHentaiCollectorConfig = field(default_factory=EHentaiCollectorConfig)
    exhentai: ExHentaiCollectorConfig = field(default_factory=ExHentaiCollectorConfig)
    nhentai: NHentaiCollectorConfig = field(default_factory=NHentaiCollectorConfig)


@dataclass
class StorageConfig:
    cache_dir: str = "/var/cache/pixiv-feed-bot/images"
    cache_days: int = 7
    db_path: str = "/var/lib/pixiv-feed-bot/data.db"


@dataclass
class PublishConfig:
    base_url: str = ""
    telegraph_short_name: str = "pixivfeed"
    telegraph_author_name: str = "Pixiv Feed Bot"
    telegraph_author_url: str = ""
    telegraph_token: str = ""
    direct_threshold: int = 5
    max_images_per_page: int = 300


@dataclass
class IllustTemplates:
    page_title: str = "{title} - {author}"
    page_header: str = ""
    page_footer: str = ""
    direct_caption: str = ""
    inline_single_caption: str = ""
    inline_multi_caption: str = ""


@dataclass
class NovelTemplates:
    page_title: str = "{title} - {author}"
    page_header: str = ""
    page_footer: str = ""
    inline_article_title: str = ""
    inline_article_description: str = ""


@dataclass
class GalleryTemplates:
    """eh / ex / nh 共用的画廊模板。
    可用变量：{title} {provider} {source_url} {page_count}
    eh/ex 还有 {gallery_id} {token} {host}；nh 有 {media_id} {gallery_id} {tags}。
    """

    page_title: str = "{title}"
    page_header: str = ""
    page_footer: str = ""


@dataclass
class TemplatesConfig:
    illust: IllustTemplates = field(default_factory=IllustTemplates)
    novel: NovelTemplates = field(default_factory=NovelTemplates)
    gallery: GalleryTemplates = field(default_factory=GalleryTemplates)


@dataclass
class LoggingConfig:
    level: str = "INFO"
    to_file: bool = False
    file_path: str = "/var/log/pixiv-feed-bot/bot.log"


# ---------------------------------------------------------------------------
# 顶层
# ---------------------------------------------------------------------------


@dataclass
class Config:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    pixiv: PixivConfig = field(default_factory=PixivConfig)
    collectors: CollectorsConfig = field(default_factory=CollectorsConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    templates: TemplatesConfig = field(default_factory=TemplatesConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    _source_path: Path | None = field(default=None, repr=False)
    _runtime: Any | None = field(default=None, repr=False)  # RuntimeSettings 实例（可空）

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> Config:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        cfg = cls._from_dict(raw)
        cfg._source_path = path
        cfg._apply_env_overrides()
        cfg._validate()
        return cfg

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> Config:
        templates_raw = data.get("templates") or {}
        templates = TemplatesConfig(
            illust=IllustTemplates(**(templates_raw.get("illust") or {})),
            novel=NovelTemplates(**(templates_raw.get("novel") or {})),
            gallery=GalleryTemplates(**(templates_raw.get("gallery") or {})),
        )

        collectors_raw = data.get("collectors") or {}
        collectors = CollectorsConfig(
            timeout=collectors_raw.get("timeout", 30),
            download_concurrency=collectors_raw.get("download_concurrency", 4),
            ehentai=EHentaiCollectorConfig(**(collectors_raw.get("ehentai") or {})),
            exhentai=ExHentaiCollectorConfig(**(collectors_raw.get("exhentai") or {})),
            nhentai=NHentaiCollectorConfig(**(collectors_raw.get("nhentai") or {})),
        )

        return cls(
            telegram=TelegramConfig(**(data.get("telegram") or {})),
            auth=AuthConfig(**(data.get("auth") or {})),
            pixiv=PixivConfig(**(data.get("pixiv") or {})),
            collectors=collectors,
            storage=StorageConfig(**(data.get("storage") or {})),
            publish=PublishConfig(**(data.get("publish") or {})),
            templates=templates,
            logging=LoggingConfig(**(data.get("logging") or {})),
        )

    def _apply_env_overrides(self) -> None:
        if env_token := os.environ.get("PIXIVFEED_TG_TOKEN"):
            self.telegram.token = env_token
        if env_phpsessid := os.environ.get("PIXIVFEED_PHPSESSID"):
            self.pixiv.phpsessid = env_phpsessid
        if env_telegraph := os.environ.get("PIXIVFEED_TELEGRAPH_TOKEN"):
            self.publish.telegraph_token = env_telegraph

    def _validate(self) -> None:
        errors = []
        if not self.telegram.token or self.telegram.token.startswith("PUT_YOUR"):
            errors.append("telegram.token is missing")
        if not self.publish.base_url:
            errors.append("publish.base_url is missing (required for Telegra.ph image URLs)")
        if self.publish.base_url.endswith("/"):
            self.publish.base_url = self.publish.base_url.rstrip("/")
        if not self.auth.admin_users:
            errors.append("auth.admin_users must contain at least one user id")
        if self.publish.direct_threshold < 0:
            errors.append("publish.direct_threshold must be >= 0")
        if self.publish.max_images_per_page <= 0 or self.publish.max_images_per_page > 300:
            errors.append("publish.max_images_per_page must be in (0, 300]")

        valid_modes = {"page_sample", "page_original", "archive_resample", "archive_original"}
        if self.collectors.ehentai.default_mode not in valid_modes:
            errors.append(f"collectors.ehentai.default_mode must be one of {valid_modes}")
        if self.collectors.exhentai.default_mode not in valid_modes:
            errors.append(f"collectors.exhentai.default_mode must be one of {valid_modes}")

        if errors:
            raise ValueError("Invalid config:\n  - " + "\n  - ".join(errors))

    def save_telegraph_token(self, token: str) -> None:
        """运行时把新生成的 Telegra.ph token 写回 YAML，不动其他字段。"""
        self.publish.telegraph_token = token
        if self._source_path is None:
            return
        try:
            content = self._source_path.read_text(encoding="utf-8")
            new_lines = []
            replaced = False
            in_publish_block = False
            for line in content.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("publish:"):
                    in_publish_block = True
                elif line and not line.startswith((" ", "\t")) and stripped and not stripped.startswith("#"):
                    in_publish_block = False
                if in_publish_block and stripped.startswith("telegraph_token:") and not replaced:
                    indent = line[: len(line) - len(stripped)]
                    new_lines.append(f'{indent}telegraph_token: "{token}"')
                    replaced = True
                else:
                    new_lines.append(line)
            if replaced:
                self._source_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 运行时覆盖
    # ------------------------------------------------------------------

    def bind_runtime(self, runtime_settings) -> None:
        """绑定 RuntimeSettings 实例并把 db 里所有覆盖项应用到 dataclass。

        启动时（db.connect + runtime_settings.load 完成后）调用。
        之后 /setting set 命令会调 set_runtime() 同时改 db 和 dataclass。
        """
        self._runtime = runtime_settings
        for key, value in runtime_settings.all().items():
            try:
                self._set_field(key, value)
            except Exception as e:
                from .utils import logger
                logger.warning(f"runtime setting {key}={value!r} ignored: {e}")

    async def set_runtime(self, key: str, value: str, updated_by: int | None = None) -> None:
        """admin 通过 /setting set 调用。先尝试转类型并赋值，成功后才写 db。"""
        if self._runtime is None:
            raise RuntimeError("Config.bind_runtime() must be called before set_runtime")
        if key not in RUNTIME_KEYS:
            raise KeyError(f"key {key!r} is not runtime-mutable; see /setting list")
        # 先在 dataclass 上验证 + 赋值（失败抛异常）
        self._set_field(key, value)
        # 再写 db
        await self._runtime.set(key, value, updated_by=updated_by)

    async def unset_runtime(self, key: str) -> bool:
        """删除 runtime 覆盖。dataclass 字段不会自动回到 yaml 值——需要重启进程。
        但下次启动会读到正确的 yaml 值。这里告诉用户重启。"""
        if self._runtime is None:
            raise RuntimeError("Config.bind_runtime() must be called before unset_runtime")
        return await self._runtime.unset(key)

    def _set_field(self, key: str, raw_value: str) -> None:
        """按 key 路径定位到 dataclass 字段并赋值，类型自动从字段标注推断。"""
        parts = key.split(".")
        obj: Any = self
        for p in parts[:-1]:
            if not is_dataclass(obj):
                raise KeyError(f"path {key!r} -> {p!r} is not a dataclass")
            if not hasattr(obj, p):
                raise KeyError(f"unknown path segment {p!r} in {key!r}")
            obj = getattr(obj, p)
        leaf = parts[-1]
        if not is_dataclass(obj):
            raise KeyError(f"final container at {key!r} is not a dataclass")
        # 找到字段类型
        target_type = None
        for f in fields(obj):
            if f.name == leaf:
                target_type = f.type
                break
        if target_type is None:
            raise KeyError(f"unknown field {leaf!r} at {key!r}")
        coerced = _coerce(raw_value, target_type)
        setattr(obj, leaf, coerced)

    def get_field(self, key: str) -> Any:
        """读单个字段值，用于 /setting list 显示。"""
        parts = key.split(".")
        obj: Any = self
        for p in parts:
            obj = getattr(obj, p)
        return obj


def _coerce(raw: str, target_type: Any) -> Any:
    """把字符串转成目标类型。target_type 可以是 type 对象或 typing 注解字符串。"""
    # dataclass.fields() 返回的 type 可能是字符串（PEP 563 / from __future__ import annotations）
    if isinstance(target_type, str):
        type_str = target_type
    else:
        type_str = getattr(target_type, "__name__", str(target_type))

    if type_str in ("bool", "<class 'bool'>"):
        v = raw.strip().lower()
        if v in ("1", "true", "yes", "y", "on"):
            return True
        if v in ("0", "false", "no", "n", "off"):
            return False
        raise ValueError(f"cannot parse {raw!r} as bool")
    if type_str in ("int", "<class 'int'>"):
        return int(raw.strip())
    if type_str in ("float", "<class 'float'>"):
        return float(raw.strip())
    if type_str in ("str", "<class 'str'>"):
        return raw
    # list[int] / list[str] 等
    if type_str.startswith("list"):
        # 简化：CSV
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if "int" in type_str:
            return [int(x) for x in items]
        return items
    # 兜底：原样
    return raw


# ---------------------------------------------------------------------------
# 运行时可改的 key 白名单
# ---------------------------------------------------------------------------
#
# 没在这里登记的字段不允许通过 /setting 改：
# - telegram.token / storage.* / publish.base_url / auth.admin_users
#   这些动了等于换基础设施，必须重启 + 改 yaml。
# - publish.telegraph_token 是 createAccount 自动生成的，不该让人改。
#
# 这个集合也是 /setting list 默认展示的内容。
# ---------------------------------------------------------------------------

RUNTIME_KEYS: set[str] = {
    # pixiv
    "pixiv.phpsessid",
    "pixiv.timeout",
    "pixiv.download_concurrency",
    # collectors（共享）
    "collectors.timeout",
    "collectors.download_concurrency",
    # ehentai
    "collectors.ehentai.enabled",
    "collectors.ehentai.default_mode",
    "collectors.ehentai.archive_timeout",
    # exhentai
    "collectors.exhentai.enabled",
    "collectors.exhentai.default_mode",
    "collectors.exhentai.archive_timeout",
    "collectors.exhentai.ipb_pass_hash",
    "collectors.exhentai.ipb_member_id",
    "collectors.exhentai.igneous",
    # nhentai
    "collectors.nhentai.enabled",
    # publish
    "publish.direct_threshold",
    "publish.max_images_per_page",
    "publish.telegraph_short_name",
    "publish.telegraph_author_name",
    "publish.telegraph_author_url",
    # storage
    "storage.cache_days",
    # logging
    "logging.level",
    # templates: illust
    "templates.illust.page_title",
    "templates.illust.page_header",
    "templates.illust.page_footer",
    "templates.illust.direct_caption",
    "templates.illust.inline_single_caption",
    "templates.illust.inline_multi_caption",
    # templates: novel
    "templates.novel.page_title",
    "templates.novel.page_header",
    "templates.novel.page_footer",
    "templates.novel.inline_article_title",
    "templates.novel.inline_article_description",
    # templates: gallery
    "templates.gallery.page_title",
    "templates.gallery.page_header",
    "templates.gallery.page_footer",
}

# 敏感字段（/setting list 显示时打码）
SENSITIVE_KEYS: set[str] = {
    "pixiv.phpsessid",
    "collectors.exhentai.ipb_pass_hash",
    "collectors.exhentai.ipb_member_id",
    "collectors.exhentai.igneous",
}


__all__ = ["Config", "RUNTIME_KEYS", "SENSITIVE_KEYS"]
