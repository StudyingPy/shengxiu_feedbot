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

from .utils import logger


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
class JMCollectorConfig:
    """禁漫天堂（jmcomic）—— 仅作"禁漫号 → 标题"查询给 /jm 命令用。

    本项目 *不* 下载 JM 图片（站点反爬激进，且与本 bot 主用途——直接展示／发
    Telegra.ph——不契合）。`enabled=False` 时 /jm 命令直接报"未启用"。
    """
    enabled: bool = False
    timeout: int = 20         # fetch_jm_title 单次调用上限（秒）


@dataclass
class CollectorsConfig:
    timeout: int = 30
    download_concurrency: int = 4
    ehentai: EHentaiCollectorConfig = field(default_factory=EHentaiCollectorConfig)
    exhentai: ExHentaiCollectorConfig = field(default_factory=ExHentaiCollectorConfig)
    nhentai: NHentaiCollectorConfig = field(default_factory=NHentaiCollectorConfig)
    jm: JMCollectorConfig = field(default_factory=JMCollectorConfig)


@dataclass
class R2Config:
    """Cloudflare R2 / 任意 S3 兼容对象存储配置。

    启用后，publisher.publish_gallery 会先把图片上传到 R2，再用 R2 URL 喂给
    Telegra.ph。上传失败/未启用时 fallback 到 publish.base_url（nginx 反代）。

    custom_domain 必填——R2 默认开发 URL（pub-xxx.r2.dev）有 ratelimit 不可用作生产
    Telegra.ph 图源；接 CF 的自定义域名后无 ratelimit、CDN 边缘缓存。

    capacity_gb：bot 内置 LRU 自动清理的容量阈值。超过 capacity_gb × 0.9 触发清理，
    清到 capacity_gb × 0.7 停手。设 0 关闭自动清理（让 bot 不管，靠你或 R2 lifecycle）。
    """
    enabled: bool = False
    endpoint: str = ""              # https://<account>.r2.cloudflarestorage.com
    region: str = "auto"            # R2 一律用 "auto"
    bucket: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    custom_domain: str = ""         # https://r2.your-domain.com（自定义域名 base，不含尾斜杠）
    # bucket 内的统一前缀。配置后所有 upload/public_url/list/delete/LRU 都局限在这个前缀下，
    # 避免共用 bucket 时误删其他项目对象。默认空（兼容存量部署），但**强烈建议配置**。
    # 形如 "pixivfeed/" 或 "staging-bot/"；尾斜杠自动补全，首斜杠自动剥掉。
    prefix: str = ""
    capacity_gb: int = 80           # 容量阈值（GB），<= 0 关闭自动 LRU
    # 单次发布总字节超过此阈值（GB）时，跳过 R2 走 nginx 本地缓存（7 天 TTL）。
    # 用户/管理员可在命令上加 --r2 强制覆盖，让大体积也上 R2。设 0.0 关闭护栏（全部上传）。
    max_upload_size_gb: float = 1.0
    lru_check_interval_minutes: int = 60   # bot 内多久跑一次 LRU 扫描（仅 R2 用量超阈值时才删）


@dataclass
class StorageConfig:
    cache_dir: str = "/var/cache/pixiv-feed-bot/images"
    cache_days: int = 7
    db_path: str = "/var/lib/pixiv-feed-bot/data.db"
    r2: R2Config = field(default_factory=R2Config)


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


@dataclass
class JobQueueConfig:
    """各类任务 worker 池的并发上限。重活留少、轻活给多——按机器内存/带宽调。"""
    archive_zip: int = 1
    zip2tph: int = 1
    direct_image: int = 2
    telegraph_publish: int = 3


@dataclass
class SizePrefetchConfig:
    """下载前预获取作品大小（详情卡 / 按钮 label 上展示 ~XX MB）。

    eh/ex 归档：GET archiver.php chooser 页解析 Estimated Size，准确，不消耗配额；
    Pixiv / nhentai：HEAD（失败 fallback Range bytes=0-0）采样前 N 张图求均值乘以总页数。

    任何阶段失败都静默回退（不显示预估行 / 按钮不带数字），不会影响下载主流程。
    """

    enabled: bool = True
    sample_count: int = 3        # Pixiv / nhentai HEAD 采样数
    timeout: int = 5             # 单请求超时秒；总 prefetch 超时 ≈ timeout
    # 各 provider 子开关：某个 provider 频繁失败时可单独关，不影响其他
    eh_archive: bool = True      # eh/ex 归档（chooser 解析）
    pixiv: bool = True
    nhentai: bool = True


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
    job_queue: JobQueueConfig = field(default_factory=JobQueueConfig)
    size_prefetch: SizePrefetchConfig = field(default_factory=SizePrefetchConfig)

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

        # storage 含嵌套 R2Config，需要单独构造（直接 **storage_raw 会把 r2 dict
        # 原样塞进字段，运行时 attrgetter 会炸 'dict has no attribute enabled'）
        storage_raw = dict(data.get("storage") or {})
        r2_raw = storage_raw.pop("r2", None) or {}
        storage = StorageConfig(**storage_raw, r2=R2Config(**r2_raw))

        return cls(
            telegram=TelegramConfig(**(data.get("telegram") or {})),
            auth=AuthConfig(**(data.get("auth") or {})),
            pixiv=PixivConfig(**(data.get("pixiv") or {})),
            collectors=collectors,
            storage=storage,
            publish=PublishConfig(**(data.get("publish") or {})),
            templates=templates,
            logging=LoggingConfig(**(data.get("logging") or {})),
            job_queue=JobQueueConfig(**(data.get("job_queue") or {})),
            size_prefetch=SizePrefetchConfig(**(data.get("size_prefetch") or {})),
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

        for name, val in (
            ("archive_zip", self.job_queue.archive_zip),
            ("zip2tph", self.job_queue.zip2tph),
            ("direct_image", self.job_queue.direct_image),
            ("telegraph_publish", self.job_queue.telegraph_publish),
        ):
            if val < 1:
                errors.append(f"job_queue.{name} must be >= 1 (got {val})")

        # R2 启用时强制核心字段必填
        r2 = self.storage.r2
        if r2.enabled:
            for f in ("endpoint", "bucket", "access_key_id", "secret_access_key", "custom_domain"):
                if not getattr(r2, f):
                    errors.append(f"storage.r2.{f} is required when storage.r2.enabled=true")
            if r2.custom_domain.endswith("/"):
                r2.custom_domain = r2.custom_domain.rstrip("/")
            if r2.endpoint.endswith("/"):
                r2.endpoint = r2.endpoint.rstrip("/")
            # prefix 空 → 警告但不拒绝启动。共用 bucket 场景会让 LRU 扫到/删到无关对象。
            # v0.8.x 软引入；v0.9.x 计划改为默认拒绝（除非显式 allow_empty_prefix）。
            if not r2.prefix:
                logger.warning(
                    "storage.r2.prefix is empty; LRU will scan & evict the entire bucket. "
                    "Set a prefix (e.g. 'feedbot/') unless this bucket is exclusively used "
                    "by pixiv-feed-bot. See config.example.yaml for details."
                )
            if not r2.prefix:
                # 不拒绝启动（兼容 v0.8.1 存量部署），但显式提醒共用 bucket 风险。
                # 后续 minor 版本可能改为默认必填。
                import warnings as _warnings
                _warnings.warn(
                    "storage.r2.prefix is empty — list/LRU/delete will operate on the entire "
                    "bucket. If this bucket is shared with other services, set storage.r2.prefix "
                    "to a project-specific value (e.g. 'pixivfeed/').",
                    stacklevel=2,
                )

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
    """把字符串转成目标类型。target_type 可以是 type 对象或 typing 注解字符串。

    解析失败统一抛 `ValueError`，message 是中文友好提示——`/setting set` handler
    现有逻辑已经 `except ValueError as e: ... ⚠️ 值无效：{e}`，这里把裸 `int()` /
    `float()` 抛出的 "invalid literal for int() with base 10: 'abc'" 这类对用户
    不友好的英文消息包一层即可。
    """
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
        raise ValueError(
            f"无法将 {raw!r} 解析为布尔值（用 true/false / yes/no / 1/0）"
        )
    if type_str in ("int", "<class 'int'>"):
        try:
            return int(raw.strip())
        except ValueError:
            raise ValueError(f"无法将 {raw!r} 解析为整数") from None
    if type_str in ("float", "<class 'float'>"):
        try:
            return float(raw.strip())
        except ValueError:
            raise ValueError(f"无法将 {raw!r} 解析为浮点数") from None
    if type_str in ("str", "<class 'str'>"):
        return raw
    # list[int] / list[str] 等
    if type_str.startswith("list"):
        # 简化：CSV
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if "int" in type_str:
            try:
                return [int(x) for x in items]
            except ValueError as e:
                raise ValueError(
                    f"无法将 {raw!r} 解析为整数列表（用逗号分隔）：{e}"
                ) from None
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
    # jm（禁漫天堂；仅 /jm 命令查询标题用）
    "collectors.jm.enabled",
    "collectors.jm.timeout",
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
    # size_prefetch
    "size_prefetch.enabled",
    "size_prefetch.sample_count",
    "size_prefetch.timeout",
    "size_prefetch.eh_archive",
    "size_prefetch.pixiv",
    "size_prefetch.nhentai",
}

# 敏感字段（/setting list 显示时打码）
SENSITIVE_KEYS: set[str] = {
    "pixiv.phpsessid",
    "collectors.exhentai.ipb_pass_hash",
    "collectors.exhentai.ipb_member_id",
    "collectors.exhentai.igneous",
    "storage.r2.access_key_id",
    "storage.r2.secret_access_key",
}


__all__ = ["Config", "RUNTIME_KEYS", "SENSITIVE_KEYS", "apply_runtime_overrides"]


# ---------------------------------------------------------------------------
# Runtime overlay（PR-4：让 cleanup.py 也能感知 /setting set 的覆盖值）
# ---------------------------------------------------------------------------


def apply_runtime_overrides(cfg: Config, db_path: str | Path) -> Config:
    """从 SQLite runtime_settings 表读出 admin 覆盖值，叠加到 cfg 上后返回同一实例。

    设计要点（reviewer 拍板）：
    - 用 sqlite3 标准库（同步），cleanup.py 等同步脚本不引入 aiosqlite。
    - PRAGMA query_only=1，避免与运行中的 bot 进程竞写锁。
    - 三种降级策略：
        1. DB 不存在 / runtime_settings 表不存在 → 静默回退到 YAML（全新部署正常路径）
        2. key 解析失败（如 "abc" → int）→ **log warning + 跳过此 key**，
           其它 key 继续 overlay。不能静默吞——脏值不可见会让排查很难。
        3. 整体连库异常 → log error 并回 YAML（不让 cleanup 启动失败）

    只 overlay RUNTIME_KEYS 集合里的 key。类型转换按目标字段的 type hint 做。
    """
    import sqlite3

    db_path = Path(db_path)
    if not db_path.exists():
        return cfg

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        logger.error(f"runtime overlay: cannot open {db_path} read-only: {e}; using YAML values")
        return cfg

    try:
        conn.execute("PRAGMA query_only=1")
        try:
            rows = conn.execute(
                "SELECT key, value FROM runtime_settings"
            ).fetchall()
        except sqlite3.OperationalError:
            # 表不存在 = 全新部署，正常降级
            return cfg

        for key, value in rows:
            if key not in RUNTIME_KEYS:
                continue
            try:
                _apply_one_override(cfg, key, value)
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning(
                    f"runtime overlay: cannot apply {key}={value!r}: {e}; "
                    f"falling back to YAML value"
                )
    finally:
        conn.close()

    return cfg


def _apply_one_override(cfg: Config, key: str, raw_value: str) -> None:
    """把 'storage.cache_days' = '30' 写到 cfg.storage.cache_days = 30。

    类型转换：从目标 dataclass 字段的 type 注解推断（int / float / bool / str）。
    """
    import dataclasses

    parts = key.split(".")
    target = cfg
    for p in parts[:-1]:
        target = getattr(target, p)
    leaf = parts[-1]

    # 目标字段类型：通过 dataclasses.fields 查
    fields = {f.name: f for f in dataclasses.fields(target)}
    if leaf not in fields:
        raise AttributeError(f"no field {leaf} on {type(target).__name__}")

    field_type = fields[leaf].type
    if isinstance(field_type, str):
        # PEP 563 / __future__.annotations 下 type 是 str；做一个最小映射
        type_map = {"int": int, "float": float, "bool": bool, "str": str}
        field_type = type_map.get(field_type, str)

    if field_type is bool:
        v = raw_value.strip().lower()
        # 严格白名单：true/false 之外（包括 "abc" 这种脏值）都视为非法，
        # 让外层 warning 路径把它当解析失败上报，不要静默归零。
        if v in ("1", "true", "yes", "on"):
            coerced = True
        elif v in ("0", "false", "no", "off", ""):
            coerced = False
        else:
            raise ValueError(f"cannot coerce {raw_value!r} to bool")
    elif field_type is int:
        coerced = int(raw_value)
    elif field_type is float:
        coerced = float(raw_value)
    else:
        coerced = raw_value

    setattr(target, leaf, coerced)
