"""消息处理与命令处理。

主要 handler：
- handle_message       消息监听，每个识别到的 ref 按 Provider 路由
- handle_callback      按钮回调：eh/ex 选模式、cancel
- cmd_pixiv_telegraph  强制 pixiv telegraph 模式
- cmd_pixiv_direct     强制 pixiv 直发
- cmd_start / cmd_help

eh/ex 流程（私聊）：
    用户发链接 → fetch_meta 拿到标题/页数 → 显示并附 4 模式按钮
    点按钮 → edit 消息为"⏳ 处理中" → 走选定模式 → edit 为 telegraph URL

eh/ex 流程（群聊或非交互场景）：
    使用配置里的 default_mode 直接处理。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from collections import deque
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter as TGRetryAfter
from telegram.error import TimedOut as TGTimedOut
from telegram.ext import ContextTypes

from ...config import Config
from ...provider import GalleryImage, GalleryWork, ParsedRef, ProviderRegistry, StatusUpdater
from ...provider.ehentai import EHError, EHGalleryUnavailable, EHMode
from ...provider.ehentai import _EHFamilyProvider as EHFamilyBase
from ...provider.ehentai import (
    EHSearchAuthError,
    EHSearchBlockedError,
    EHSearchError,
    SearchResultPage,
    search_eh,
)
from ...provider.ehentai._archive import (
    ArchiveError,
    ArchiveLockedError,
    compute_archive_timeout,
    download_archive_with_timeout,
    fetch_archive_sizes,
    fetch_archiver_token,
    refresh_download_link,
    request_archive,
)
from ...provider.ehentai._modes import BASE_HEADERS as EH_BASE_HEADERS
from ...provider._size_prefetch import estimate_total_bytes
from ...provider.nhentai import NHENTAI_CDNS, NHentaiAlbum, NHentaiError, NHentaiProvider
from ...provider.pixiv import (
    PixivAPIError,
    PixivAuthError,
    PixivNotFoundError,
    PixivProvider,
)
from ...provider.pixiv.novel_publisher import publish_novel
from ...publisher import TelegraphPublisher
from ...storage import (
    KIND_ARCHIVE_CMD,
    KIND_EH_ARCHIVE,
    KIND_EH_PAGE,
    KIND_EH_SEARCH,
    KIND_NHENTAI,
    KIND_PIXIV_NOVEL,
    KIND_PIXIV_TELEGRAPH,
    KIND_ZH,
    KIND_ZIP2TPH,
    AllowList,
    TelegraphCache,
)
from ...storage.cache import invalidate_for_r2_keys
from ...storage.r2 import R2ListIncomplete, R2StatsSnapshot, lru_evict_to_target, stats_from_objects
from ...utils import (
    check_disk_free,
    format_disk_full_message,
    logger,
)
from .auth import is_admin, is_authorized
from .constants import (
    CANCEL_TOKEN_TTL,
    LOCAL_BOT_API_DOCUMENT_LIMIT,
    PENDING_TTL,
    TG_DOCUMENT_LIMIT,
    TG_UPLOAD_TIMEOUT,
)
from .jobqueue import JobQueueManager
from .progress import (
    ByteRateTracker,
    ImageCounter,
    Progress,
    fmt_bytes,
    fmt_duration,
    make_item_hook,
)


# ---------------------------------------------------------------------------
# 共享上下文
# ---------------------------------------------------------------------------


def _ctx(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[Config, ProviderRegistry, TelegraphPublisher, TelegraphCache, AllowList]:
    bd = context.bot_data
    return bd["config"], bd["registry"], bd["publisher"], bd["telegraph_cache"], bd["allowlist"]


def _parse_r2_flag(
    args: list[str], user_id: int, admin_users: list[int],
) -> tuple[list[str], bool]:
    """从命令 args 里抠 --r2 / --force-r2 flag。

    flag 仅 admin 可用——非 admin 用了静默忽略（避免把命令的"管理员能用"暴露）。
    返回 (剩余 args, force_r2 是否生效)。
    """
    force = False
    filtered: list[str] = []
    is_admin = user_id in set(admin_users)
    for a in args:
        if a in ("--r2", "--force-r2"):
            if is_admin:
                force = True
            # 非 admin 也吞掉，避免误传给参数解析
        else:
            filtered.append(a)
    return (filtered, force)


def _r2_skipped_suffix(pub, *, r2_enabled: bool) -> str:
    """根据 PublishResult.fallback_reason 给完成消息追加风险提示。

    关键约束（reviewer 拍板）：
    - **R2 未启用时一律不弹提示**，避免默认部署用户每次发布都看到吓人警告。
    - 全 R2 成功（reason="" 或 NONE）也不弹提示。
    - 其它 reason 在 R2 启用下展示对应风险文案。

    fallback_reason 枚举见 publisher.telegraph.FallbackReason。
    """
    if not r2_enabled:
        return ""
    reason = getattr(pub, "fallback_reason", "") or ""
    if not reason:
        return ""
    # 各 reason 对应的用户提示
    if reason == "r2_disabled":
        # 逻辑上 R2 enabled=True 不该出现此 reason；防御性留空
        return ""
    if reason == "size_guard_skipped":
        return (
            "\n\n⚠️ 此 Telegra.ph 因体积过大跳过 R2 持久化存储，"
            "最短 7 天后图片可能失效。\n"
            "如需保留请管理员加 <code>--r2</code> 参数重新发布。"
        )
    if reason == "r2_batch_failed":
        return (
            "\n\n⚠️ R2 上传失败，本次未持久化，图片可能在 7 天后失效。\n"
            "稍后可加 <code>--r2</code> 重新发布以触发重试。"
        )
    if reason == "r2_partial":
        fb = getattr(pub, "fallback_image_count", 0)
        total = getattr(pub, "image_count", 0)
        return (
            f"\n\n⚠️ 部分图片（{fb}/{total}）未上传 R2，仍依赖本地缓存，"
            "可能在 7 天后失效。\n"
            "如需修复请管理员加 <code>--r2</code> 重新发布。"
        )
    if reason == "local_file_missing":
        return (
            "\n\n⚠️ 部分本地文件缺失，已使用 fallback URL。"
            "如希望修复请管理员加 <code>--r2</code> 重新发布。"
        )
    # 未知 reason → 保守不弹提示（避免出错文案吓到用户）
    return ""


def _job_queue(context: ContextTypes.DEFAULT_TYPE) -> JobQueueManager:
    return context.bot_data["job_queue"]


def _usage_store(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data.get("usage_store")


def _effective_force_r2(
    context: ContextTypes.DEFAULT_TYPE, force_r2: bool,
) -> bool:
    """force_r2 真正生效的条件：admin 传了 flag **且** R2 已启用 + client 已注入。

    R2 未启用时所有缓存行都不可能 durable（fallback_reason=r2_disabled），如果还
    把 force_r2 当真，每次 admin --r2 都会绕过 cache 重发，但产物依旧是非 durable，
    形成空转。所以未启用 R2 时 force_r2 在 cache gate 上视为 False。

    注意：publish_gallery 内部的 force_r2 仍照常传（让它跳过 size_guard），但
    cache 命中分支只看 _effective_force_r2 的结果。
    """
    if not force_r2:
        return False
    config: Config = context.bot_data.get("config")
    if config is None or not config.storage.r2.enabled:
        return False
    if context.bot_data.get("r2_client") is None:
        return False
    return True


def _record_r2_scan_failure(context: ContextTypes.DEFAULT_TYPE, exc) -> None:
    """把 R2ListIncomplete 写进 bot_data["r2_stats_meta"]——与 bot.py 的后台 LRU
    loop 共享同一份 meta dict，让 /stats system 一致看到 stale 标记 + 24h 失败计数。

    /stats r2_evict 与后台 loop 都调这个，避免分别维护两套状态漂移。
    """
    meta = context.bot_data.get("r2_stats_meta")
    if meta is None:
        # 后台 loop 还没初始化（开机 30s 内手工触发）→ 退化为新建 meta
        meta = {
            "last_scan_failed_at": None,
            "last_scan_failed_cause": None,
            "last_scan_success_at": None,
            "stale": False,
            "failures_deque": deque(),
        }
        context.bot_data["r2_stats_meta"] = meta
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    dq = meta.get("failures_deque")
    if dq is None:
        dq = deque()
        meta["failures_deque"] = dq
    # 写入侧裁掉 >24h 的，避免长期不重启时 deque 无界增长
    cutoff = now.timestamp() - 86400
    while dq and dq[0] < cutoff:
        dq.popleft()
    dq.append(now.timestamp())
    meta["last_scan_failed_at"] = now
    cause = getattr(exc, "cause", str(exc))
    meta["last_scan_failed_cause"] = str(cause)[:200]
    meta["stale"] = True


def _record_r2_scan_success(
    context: ContextTypes.DEFAULT_TYPE, scanned_at,
) -> None:
    """扫描成功后清掉 stale 标记 + 写 last_scan_success_at。

    手动 /stats r2_evict 和后台 LRU loop 都应当调此 helper，否则会出现"后台失败 →
    手动成功 → /stats 仍显示 stale"的视觉漂移。
    """
    meta = context.bot_data.get("r2_stats_meta")
    if meta is None:
        meta = {"failures_deque": deque()}
        context.bot_data["r2_stats_meta"] = meta
    meta["last_scan_success_at"] = scanned_at
    meta["stale"] = False
    # 不清 last_scan_failed_at——保留作历史诊断；stale=False 已足够让 /stats 不警告


async def _track_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """每次接到授权用户消息时静默 upsert，更新 last_seen 和昵称。
    顺带 upsert effective_chat 的标题（群组/频道）—— /stats 按群组分组时要用。
    任何失败都吞掉，不影响主流程。"""
    store = _usage_store(context)
    if store is None:
        return
    u = update.effective_user
    if u is not None:
        try:
            await store.upsert_user(
                user_id=u.id,
                first_name=u.first_name,
                last_name=u.last_name,
                username=u.username,
            )
        except Exception:
            pass
    chat = update.effective_chat
    if chat is not None:
        try:
            await store.upsert_chat(
                chat_id=chat.id,
                chat_type=chat.type or "",
                title=chat.title,
                username=chat.username,
            )
        except Exception:
            pass


async def _log_usage(
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    *,
    kind: str,
    provider: str | None = None,
    ref_id: str | None = None,
    gp_cost: int = 0,
    bytes_in: int = 0,
    bytes_out: int = 0,
    status: str = "ok",
) -> None:
    """统一入口写一条用量记录。任何失败都吞掉。"""
    store = _usage_store(context)
    if store is None:
        return
    user = update.effective_user
    chat = update.effective_chat
    if user is None:
        return
    try:
        await store.log(
            user_id=user.id,
            chat_id=chat.id if chat else None,
            kind=kind,
            provider=provider,
            ref_id=ref_id,
            gp_cost=gp_cost,
            bytes_in=bytes_in,
            bytes_out=bytes_out,
            status=status,
        )
    except Exception:
        pass


async def _log_usage_raw(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int | None,
    chat_id: int | None,
    kind: str,
    provider: str | None = None,
    ref_id: str | None = None,
    gp_cost: int = 0,
    bytes_in: int = 0,
    bytes_out: int = 0,
    status: str = "ok",
) -> None:
    """带 user_id/chat_id 的原始写入接口，给没法直接拿 update 的回调路径用。"""
    if user_id is None:
        return
    store = _usage_store(context)
    if store is None:
        return
    try:
        await store.log(
            user_id=user_id, chat_id=chat_id, kind=kind, provider=provider,
            ref_id=ref_id, gp_cost=gp_cost, bytes_in=bytes_in, bytes_out=bytes_out,
            status=status,
        )
    except Exception:
        pass


def _is_timeout_exc(e: BaseException) -> bool:
    """宽松判定：是否是各种 'timed out' 异常。

    PTB 在不同版本里把超时包装成 TGTimedOut / NetworkError / RemoteProtocolError 等，
    底层 httpx 也会直接抛 ReadTimeout/WriteTimeout/ConnectTimeout。
    本项目对超时统一处理（"温和提示，可能仍在后台完成"），所以这里宽松匹配。
    """
    if isinstance(e, TGTimedOut):
        return True
    name = type(e).__name__.lower()
    if "timeout" in name:
        return True
    msg = str(e).lower()
    return "timed out" in msg or "timeout" in msg


# 全局 cancel token → JobHandle 表，按钮 callback_data 用短 token 索引
# 任务完成或取消后会被清理；超过 1 小时未清也会被 _gc_pending 顺便清。
_CANCEL_TOKENS: dict[str, object] = {}   # token -> dict(handle, owner_id, ts)

# placeholder.message_id -> 当前应该保留的 reply_markup（None 表示已经显式去掉）。
# Progress 实例化时从这里读，使每次进度更新都能保留按钮。_drop_cancel_button 会清成 None。
_PLACEHOLDER_MARKUPS: dict[int, object] = {}


def _cancel_button(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ 取消", callback_data=f"jc:{token}")]]
    )


def _attach_progress_markup(progress, placeholder) -> None:
    """工作函数构造 Progress 后调一次：把 placeholder 上当前的 cancel 按钮同步给 progress，
    避免每次 progress.update 时 edit_text 把按钮抹掉。"""
    if placeholder is None:
        return
    mid = getattr(placeholder, "message_id", None)
    if mid is None:
        return
    markup = _PLACEHOLDER_MARKUPS.get(mid)
    if markup is not None:
        progress.set_markup(markup)


async def _gate_disk_space(
    context: ContextTypes.DEFAULT_TYPE,
    placeholder,
    *,
    extra_required: int = 0,
) -> bool:
    """任务入队前的磁盘剩余空间护栏。

    检查 storage.cache_dir 所在挂载点是否还有 ≥ MIN_FREE_DISK_BYTES + extra_required。
    不足时在 placeholder 上写中文提示并返回 False；调用方应直接 return。
    充足时返回 True，调用方继续 _enqueue。
    """
    config, *_ = _ctx(context)
    cache_dir = Path(config.storage.cache_dir)
    # cache_dir 在启动时已确保存在；若意外不存在则退到其父目录 / 根，避免 disk_usage 报错
    probe = cache_dir if cache_dir.exists() else (cache_dir.parent if cache_dir.parent.exists() else Path("/"))
    ok, free, required = check_disk_free(probe, extra_required=extra_required)
    if ok:
        return True
    logger.warning(
        f"disk gate refused task: free={free} bytes, required={required} bytes "
        f"(extra={extra_required}) at {probe}"
    )
    try:
        await placeholder.edit_text(format_disk_full_message(free, extra_required))
    except Exception:
        pass
    return False


async def _enqueue(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    category: str,
    user_id: int,
    placeholder,
    work_label: str,
    coro_factory,
    cancellable: bool = True,
) -> bool:
    """把重活包装成入队 + 排队反馈 + 实际跑。返回 True 表示已入队，False 表示被拒。

    placeholder: 已有的占位 Message。入队即在它上面写"队列第 N 位"，进入处理时切到 work_label。
    cancellable: True 时占位消息上挂"❌ 取消"按钮，仅 owner / admin 可点。
    """
    jq = _job_queue(context)
    cancel_token = uuid.uuid4().hex[:10] if cancellable else ""
    cancel_markup = _cancel_button(cancel_token) if cancel_token else None
    if cancel_markup is not None:
        _PLACEHOLDER_MARKUPS[placeholder.message_id] = cancel_markup

    async def _on_position(pos: int) -> None:
        # pos = 自己之前还有几个等待中
        kwargs = {}
        if cancel_markup is not None:
            kwargs["reply_markup"] = cancel_markup
        if pos == 0:
            # 立即就跑：仍然先把按钮挂上（_on_started 立马会替换文案，但保留按钮）
            try:
                await placeholder.edit_text(f"⏳ {work_label}", **kwargs)
            except Exception:
                pass
            return
        try:
            await placeholder.edit_text(
                f"⏳ 已加入队列（{category}）\n前面还有 {pos} 个任务等待中",
                **kwargs,
            )
        except Exception:
            pass

    async def _on_started() -> None:
        kwargs = {}
        if cancel_markup is not None:
            kwargs["reply_markup"] = cancel_markup
        try:
            await placeholder.edit_text(f"⏳ {work_label}", **kwargs)
        except Exception:
            pass

    async def _on_reject(msg: str) -> None:
        try:
            await placeholder.edit_text(msg)
        except Exception:
            pass

    async def _on_cancelled() -> None:
        # 任务被取消后清表
        if cancel_token:
            _CANCEL_TOKENS.pop(cancel_token, None)
        _PLACEHOLDER_MARKUPS.pop(placeholder.message_id, None)

    handle = await jq.submit(
        category,
        user_id=user_id,
        coro_factory=coro_factory,
        on_position=_on_position,
        on_started=_on_started,
        on_reject=_on_reject,
        on_cancelled=_on_cancelled,
    )
    if handle is None:
        return False
    if cancel_token:
        _CANCEL_TOKENS[cancel_token] = {
            "handle": handle,
            "owner_id": user_id,
            "ts": time.time(),
        }
    return True


def _pixiv_provider(registry: ProviderRegistry) -> PixivProvider | None:
    p = registry.find_by_name("pixiv")
    return p if isinstance(p, PixivProvider) else None


def _eh_provider(registry: ProviderRegistry, host: str) -> EHFamilyBase | None:
    p = registry.find_by_name(host)
    return p if isinstance(p, EHFamilyBase) else None


# ---------------------------------------------------------------------------
# 待选缓存（按钮交互的 token → ref 映射）
# ---------------------------------------------------------------------------
#
# Telegram callback_data 限制 64 bytes，eh/ex 的 ref.id 可能就占 20+。
# 用短 token 索引，一段时间后自动清理。
#
# 同时记录 placeholder 消息 id 以便点击后 edit。


@dataclass
class _Pending:
    ref: ParsedRef
    chat_id: int                  # 详情卡（带按钮）所在 chat
    msg_id: int                   # 详情卡 message_id
    user_id: int                  # 仅这个 user 可点（防群聊抢按）
    created_at: float
    force_r2: bool = False        # admin --r2 创建时携带，回调到 _eh_run_with_mode 时透传
    # 用户原始链接消息的 chat/id；用于回调里走"直发图片"时把图片 reply 到原消息，
    # 而不是 reply 到即将被删除的详情卡 placeholder。
    # 来自搜索流（/ehsearch 没有原始 message）时保持 None，sender 退回不带 reply_to。
    orig_chat_id: int | None = None
    orig_msg_id: int | None = None


_PENDING: dict[str, _Pending] = {}


@dataclass
class _SearchState:
    """/ehsearch 一次搜索的会话状态。

    key 为 10-char hex seid（callback_data 里出现），TTL 与 _PENDING 共用 PENDING_TTL。
    """
    host: str                        # "e-hentai.org" / "exhentai.org"
    keyword: str
    page: SearchResultPage
    chat_id: int
    msg_id: int
    user_id: int                     # 仅这个 user 可点（与 _PENDING 一致）
    expanded: bool                   # False=10 条，True=全部 25 条
    created_at: float
    force_r2: bool = False           # admin --r2 创建时携带，回调点开/打开时透传到 _eh_run_with_mode
    # 当前消息正显示的 ptoken（仅 L2/L3 有），切层/翻页时必须失效掉，
    # 防止晚到的 size prefetch 把按钮覆盖回旧详情卡。
    # see _search_invalidate_active_ptoken / _make_pending_for_item
    active_ptoken: str | None = None


_SEARCH_STATES: dict[str, _SearchState] = {}


def _gc_pending() -> None:
    now = time.time()
    expired = [k for k, v in _PENDING.items() if now - v.created_at > PENDING_TTL]
    for k in expired:
        _PENDING.pop(k, None)
    # search 状态走同一 TTL
    search_expired = [
        k for k, v in _SEARCH_STATES.items() if now - v.created_at > PENDING_TTL
    ]
    for k in search_expired:
        _SEARCH_STATES.pop(k, None)
    # 顺便清过期 cancel token
    cancel_expired = [
        k for k, v in _CANCEL_TOKENS.items()
        if now - v.get("ts", 0) > CANCEL_TOKEN_TTL  # type: ignore[union-attr]
    ]
    for k in cancel_expired:
        _CANCEL_TOKENS.pop(k, None)


def _eh_mode_buttons(
    token: str,
    *,
    prefix: str = "eh",
    sizes: dict[EHMode, int] | None = None,
) -> list[list[InlineKeyboardButton]]:
    """eh/ex 4 模式按钮的两行（前 4 个模式）。所有详情卡入口共用。

    sizes 不为 None 时按 mode 在 label 后拼 " ~XX MB"。当前只有 archive_* 走
    `fetch_archive_sizes` 拿得到数字，page_* 仍是裸 label。
    prefix: 'eh' / 'eha'，区分回调路由（私聊粘链 vs /archive）。
    """
    def label(m: EHMode) -> str:
        n = (sizes or {}).get(m)
        if n and n > 0:
            return f"{m.label_zh} ~{fmt_bytes(n)}"
        return m.label_zh

    return [
        [
            InlineKeyboardButton(label(EHMode.PAGE_SAMPLE), callback_data=f"{prefix}:{token}:page_sample"),
            InlineKeyboardButton(label(EHMode.PAGE_ORIGINAL), callback_data=f"{prefix}:{token}:page_original"),
        ],
        [
            InlineKeyboardButton(label(EHMode.ARCHIVE_RES), callback_data=f"{prefix}:{token}:archive_resample"),
            InlineKeyboardButton(label(EHMode.ARCHIVE_ORG), callback_data=f"{prefix}:{token}:archive_original"),
        ],
    ]


def _make_eh_keyboard(
    token: str,
    *,
    sizes: dict[EHMode, int] | None = None,
    prefix: str = "eh",
) -> InlineKeyboardMarkup:
    """eh/ex 模式选择键盘。callback_data 只放短 token + mode value。

    `sizes` 由 size prefetch 完成后回填；首次发送时为 None（裸 label）。
    `prefix` 决定回调路由：'eh' 走 handle_callback（私聊粘链），
    'eha' 走 handle_callback_archive（/archive）。
    """
    rows = _eh_mode_buttons(token, prefix=prefix, sizes=sizes)
    rows.append([InlineKeyboardButton("取消", callback_data=f"{prefix}:{token}:cancel")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# size prefetch：发完详情卡后异步拿真实大小，回填按钮 label
# ---------------------------------------------------------------------------
#
# 为什么单独抽：eh/ex 详情卡有 5 处入口，每处发完按钮都要起一次 prefetch；
# Pixiv / nhentai 也要复用同一套竞态保护。
#
# 竞态保护背景：
#   prefetch 是 fire-and-forget 的 create_task，但用户可能在 prefetch 完成前
#   就点了按钮——按钮回调会把消息文本 edit 成 "⏳ 已收到..." 并 _PENDING.pop(token)。
#   如果 prefetch 后于按钮点击完成，简单的 edit_reply_markup 会把"处理中"消息的
#   按钮换回详情卡按钮，造成视觉错乱。
#   `_safe_update_buttons` 在写按钮前三重校验：
#     1. token 仍在 _PENDING（用户没点取消/没点模式按钮）
#     2. _PENDING[token].chat_id 等于 placeholder.chat.id
#     3. _PENDING[token].msg_id 等于 placeholder.message_id
#   全部通过才动按钮；并且**只动 reply_markup**（不动文本），以减少冲突面。


async def _safe_update_buttons(
    placeholder, token: str, new_markup: InlineKeyboardMarkup,
) -> bool:
    """竞态安全地仅更新按钮 label。

    仅当 `_PENDING[token]` 仍指向 `placeholder` 同一条消息时才发 edit。
    其它情况（token 已被 pop / placeholder 被换成另一条 / edit 抛异常）
    一律静默返回 False；prefetch 永远不应该把"处理中"覆盖回详情卡。
    """
    pending = _PENDING.get(token)
    if pending is None:
        return False
    if pending.chat_id != placeholder.chat.id or pending.msg_id != placeholder.message_id:
        return False
    try:
        await placeholder.edit_reply_markup(reply_markup=new_markup)
        return True
    except Exception as e:
        logger.debug(f"_safe_update_buttons {token}: edit failed: {e}")
        return False


async def _safe_update_card(
    placeholder, token: str, new_text: str, new_markup: InlineKeyboardMarkup,
    *, parse_mode: str | None = ParseMode.HTML,
) -> bool:
    """竞态安全地更新整张卡（文本 + 按钮）。

    Pixiv / nhentai 详情卡把 ~XX MB 写在正文里，prefetch 完成时需要重写正文；
    eh/ex 详情卡走 _safe_update_buttons（只动按钮 label）减小冲突面。

    校验维度与 _safe_update_buttons 一致（token + chat + msg_id 三重）。
    """
    pending = _PENDING.get(token)
    if pending is None:
        return False
    if pending.chat_id != placeholder.chat.id or pending.msg_id != placeholder.message_id:
        return False
    try:
        await placeholder.edit_text(
            new_text,
            parse_mode=parse_mode,
            reply_markup=new_markup,
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        logger.debug(f"_safe_update_card {token}: edit failed: {e}")
        return False


def _schedule_eh_size_prefetch(
    context: ContextTypes.DEFAULT_TYPE,
    placeholder,
    token: str,
    ref: ParsedRef,
    *,
    prefix: str = "eh",
    keyboard_builder=None,
) -> None:
    """私聊详情卡发完后调用一次；起异步任务拿 archive 两档大小，回填按钮 label。

    - 受 `config.size_prefetch.enabled` 与 `size_prefetch.eh_archive` 双开关控制
    - prefetch 不会消耗 archive 配额（只 GET chooser 页）
    - 失败一律静默：fetch_archive_sizes 返回空 dict / 抛异常 → 按钮 label 保持原样
    - 调用方传 prefix 决定回调路由（'eh' 走 handle_callback，'eha' 走 handle_callback_archive）
    - `keyboard_builder(sizes: dict[EHMode, int]) -> InlineKeyboardMarkup` 用于
      搜索流 L2/L3 这类带"返回"等额外行的键盘；不传则用标准 `_make_eh_keyboard`。
    """
    config, registry, *_ = _ctx(context)
    sp = config.size_prefetch
    if not sp.enabled or not sp.eh_archive:
        return
    provider = _eh_provider(registry, ref.provider)
    if provider is None:
        return

    try:
        gid, gtoken = ref.id.split("/", 1)
    except ValueError:
        return
    host = ref.provider
    album_url = f"https://{host}/g/{gid}/{gtoken}"

    async def _run() -> None:
        try:
            # archive chooser 需要登录态；用 ARCHIVE_ORG 的 cookies
            # （在 e-hentai 上会带 exhentai 登录 cookie，在 exhentai 上一样）
            async with provider._make_client(EHMode.ARCHIVE_ORG) as client:
                sizes = await fetch_archive_sizes(client, album_url, host, gid, gtoken)
        except Exception as e:
            logger.debug(f"size prefetch {ref.id} failed: {e}")
            return
        if not sizes:
            return
        if keyboard_builder is not None:
            try:
                new_markup = keyboard_builder(sizes)
            except Exception as e:
                logger.debug(f"size prefetch {ref.id}: keyboard_builder failed: {e}")
                return
        else:
            new_markup = _make_eh_keyboard(token, sizes=sizes, prefix=prefix)
        await _safe_update_buttons(placeholder, token, new_markup)

    # fire-and-forget：详情卡 UI 不等 prefetch
    asyncio.create_task(_run())


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, registry, publisher, tg_cache, allowlist = _ctx(context)
    if not await is_authorized(update, allowlist):
        return
    await _track_user(update, context)

    # 优先处理 /setting edit 流程的下一条消息
    from .setting import handle_setting_edit_followup
    if await handle_setting_edit_followup(update, context):
        return

    text = update.effective_message.text or update.effective_message.caption or ""
    refs = registry.extract_all_refs(text)
    if not refs:
        return
    # 管理员可在消息里写 --r2 / --force-r2 强制本批发布上传 R2（绕过 max_upload_size_gb 护栏）
    user_id = update.effective_user.id if update.effective_user else 0
    _, force_r2 = _parse_r2_flag(text.split(), user_id, config.auth.admin_users)
    for ref in refs:
        await _handle_ref(update, context, ref, mode="auto", force_r2=force_r2)


async def cmd_pixiv_telegraph(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, registry, _, _, allowlist = _ctx(context)
    if not await is_authorized(update, allowlist):
        return
    await _track_user(update, context)
    pixiv = _pixiv_provider(registry)
    if pixiv is None:
        await update.message.reply_text("⚠️ 未启用 Pixiv Provider")
        return
    user_id = update.effective_user.id if update.effective_user else 0
    raw_args = list(context.args or [])
    args, force_r2 = _parse_r2_flag(raw_args, user_id, config.auth.admin_users)
    text = " ".join(args) or (update.effective_message.text or "")
    refs = pixiv.extract_refs(text)
    if not refs:
        await update.message.reply_text("用法：/pixiv_telegraph <Pixiv 链接> [--r2]")
        return
    for ref in refs:
        await _handle_ref(update, context, ref, mode="ph", force_r2=force_r2)


async def cmd_pixiv_direct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, registry, _, _, allowlist = _ctx(context)
    if not await is_authorized(update, allowlist):
        return
    await _track_user(update, context)
    pixiv = _pixiv_provider(registry)
    if pixiv is None:
        await update.message.reply_text("⚠️ 未启用 Pixiv Provider")
        return
    user_id = update.effective_user.id if update.effective_user else 0
    raw_args = list(context.args or [])
    args, force_r2 = _parse_r2_flag(raw_args, user_id, config.auth.admin_users)
    text = " ".join(args) or (update.effective_message.text or "")
    refs = pixiv.extract_refs(text)
    if not refs:
        await update.message.reply_text("用法：/pixiv_direct <Pixiv 链接>")
        return
    for ref in refs:
        await _handle_ref(update, context, ref, mode="direct")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, registry, *_ = _ctx(context)
    enabled = ", ".join(p.name for p in registry.all())
    await update.message.reply_text(
        "Feed Bot\n\n"
        "在群里/私聊发送以下站点的链接即可自动转发：\n"
        f"  注册 Provider：{enabled}\n\n"
        "命令：\n"
        "  /pixiv_telegraph <链接>  强制 pixiv Telegra.ph 模式\n"
        "  /pixiv_direct <链接>     强制 pixiv 直发图片\n"
        "  /archive <链接>          直接返回压缩包（eh/ex 仍弹模式按钮）\n"
        "  /ehsearch <关键词>       搜索 eh/ex 画廊（点结果即开）\n"
        "  /zip2tph                 回复一张 zip 图片包，发布为 Telegra.ph\n"
        "  /wiki <词条>             查中文维基百科\n"
        "  /chatid                  查看当前 chat_id\n"
        "  /setting list            （仅 admin）查看运行时配置\n"
        "  /setting help            （仅 admin）查看 setting 命令帮助\n"
        "  /cache help              （仅 admin）telegraph 缓存管理\n"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


# ---------------------------------------------------------------------------
# 单 ref 路由
# ---------------------------------------------------------------------------


async def _handle_ref(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ref: ParsedRef,
    *,
    mode: str,
    force_r2: bool = False,
) -> None:
    """mode ∈ {auto, ph, direct}"""
    if ref.provider == "pixiv" and ref.kind == "novel":
        placeholder = await update.message.reply_text(f"⏳ 已收到（pixiv novel {ref.id}），准备处理...")
        if not await _gate_disk_space(context, placeholder):
            return
        user_id = update.effective_user.id if update.effective_user else 0

        async def _do_novel() -> None:
            await _send_pixiv_novel(
                update, context, ref.id, placeholder=placeholder, force_r2=force_r2,
            )

        await _enqueue(
            context,
            category="telegraph_publish",
            user_id=user_id,
            placeholder=placeholder,
            work_label=f"处理小说 {ref.id} 中...",
            coro_factory=_do_novel,
        )
        return

    if ref.provider == "pixiv" and ref.kind == "illust":
        # 私聊 + auto 模式弹详情卡（含张数 + ~XX MB + 两按钮）；
        # 群聊 / /pixiv_direct / /pixiv_telegraph 跳过详情卡，照旧行为。
        chat = update.effective_chat
        if chat is not None and chat.type == "private" and mode == "auto":
            await _pixiv_offer_modes(update, context, ref, force_r2=force_r2)
            return
        await _handle_pixiv_illust(update, context, ref, mode=mode, force_r2=force_r2)
        return

    # eh / ex：私聊弹按钮，群聊默认模式
    if ref.provider in ("e-hentai.org", "exhentai.org"):
        chat = update.effective_chat
        if chat is not None and chat.type == "private":
            await _eh_offer_modes(update, context, ref, force_r2=force_r2)
        else:
            placeholder = await update.message.reply_text(
                f"⏳ 已收到（{ref.provider}），准备处理..."
            )
            if not await _gate_disk_space(context, placeholder):
                return
            user_id = update.effective_user.id if update.effective_user else 0

            async def _do() -> None:
                await _eh_run_with_mode(
                    update, context, ref, mode=None, placeholder=placeholder,
                    force_r2=force_r2,
                )

            await _enqueue(
                context,
                category="telegraph_publish",
                user_id=user_id,
                placeholder=placeholder,
                work_label=f"{ref.provider} 处理中...",
                coro_factory=_do,
            )
        return

    # nhentai 私聊弹详情卡，群聊 / 其它直接走 telegraph
    if ref.provider == "nhentai":
        chat = update.effective_chat
        if chat is not None and chat.type == "private":
            await _nhentai_offer_modes(update, context, ref, force_r2=force_r2)
            return

    # nhentai 群聊与其它走默认 telegraph
    placeholder = await update.message.reply_text(f"⏳ 已收到（{ref.provider}），准备处理...")
    if not await _gate_disk_space(context, placeholder):
        return
    user_id = update.effective_user.id if update.effective_user else 0

    async def _do_generic() -> None:
        await _send_via_telegraph_generic(
            update, context, ref, placeholder=placeholder, force_r2=force_r2,
        )

    await _enqueue(
        context,
        category="telegraph_publish",
        user_id=user_id,
        placeholder=placeholder,
        work_label=f"{ref.provider} 处理中...",
        coro_factory=_do_generic,
    )


# ---------------------------------------------------------------------------
# eh/ex：弹按钮 / 直接跑
# ---------------------------------------------------------------------------


async def _eh_offer_modes(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ref: ParsedRef,
    *, force_r2: bool = False,
) -> None:
    config, registry, *_ = _ctx(context)
    provider = _eh_provider(registry, ref.provider)
    if provider is None:
        return

    placeholder = await update.message.reply_text("📖 解析中...")

    try:
        gallery = await provider.fetch_work(ref)
    except EHGalleryUnavailable as e:
        # e-hentai 不可用时尝试 fallback 到 exhentai
        if ref.provider == "e-hentai.org":
            fallback = await _try_fallback_to_exhentai(context, ref, placeholder, str(e))
            if fallback is not None:
                ref, gallery, provider = fallback
            else:
                return
        else:
            await placeholder.edit_text(f"⚠️ 解析失败：{e}")
            return
    except EHError as e:
        await placeholder.edit_text(f"⚠️ 解析失败：{e}")
        return
    except Exception as e:
        logger.exception(f"{ref.provider} fetch_work failed for {ref.id}")
        await placeholder.edit_text(f"⚠️ 解析失败：{e}")
        return

    _gc_pending()
    token = uuid.uuid4().hex[:10]
    orig_msg = update.effective_message
    _PENDING[token] = _Pending(
        ref=ref,
        chat_id=placeholder.chat.id,
        msg_id=placeholder.message_id,
        user_id=update.effective_user.id,
        created_at=time.time(),
        force_r2=force_r2,
        orig_chat_id=orig_msg.chat.id if orig_msg else None,
        orig_msg_id=orig_msg.message_id if orig_msg else None,
    )

    # title 转义在 _render_eh_detail_card 内部统一走 _html_escape
    text = _render_eh_detail_card(
        title=gallery.title,
        host=ref.provider,
        category=gallery.category,
        pages=gallery.page_count,
        tags=gallery.tags,
        ehtagdb=_get_ehtagdb(context),
        footer_prompt="选择下载模式：",
    )
    await placeholder.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_make_eh_keyboard(token),
        disable_web_page_preview=True,
    )

    # 异步拿 archive 两档大小，回填按钮 label。失败/超时静默跳过，不影响 UX。
    _schedule_eh_size_prefetch(context, placeholder, token, ref)


async def _try_fallback_to_exhentai(
    context: ContextTypes.DEFAULT_TYPE,
    eh_ref: ParsedRef,
    placeholder,
    eh_error_msg: str,
):
    """e-hentai 上 unavailable，尝试 fallback 到 exhentai。

    成功时返回 (ex_ref, gallery, ex_provider)；失败/不可 fallback 时
    向 placeholder 写错误信息并返回 None。
    """
    _, registry, *_ = _ctx(context)
    config = context.bot_data["config"]
    ex_provider = _eh_provider(registry, "exhentai.org")
    ex_cfg = config.collectors.exhentai

    # 不可 fallback 的几种情况：先告诉用户原因再 return
    if ex_provider is None or not ex_cfg.enabled:
        await placeholder.edit_text(
            f"⚠️ e-hentai 上不可用：{eh_error_msg}\n"
            "提示：启用 ExHentai 可能能拿到（/setting set collectors.exhentai.enabled true）"
        )
        return None
    if not (ex_cfg.ipb_pass_hash and ex_cfg.ipb_member_id and ex_cfg.igneous):
        await placeholder.edit_text(
            f"⚠️ e-hentai 上不可用：{eh_error_msg}\n"
            "提示：ExHentai 已启用但 cookie 未配置，无法 fallback"
        )
        return None

    ex_ref = ParsedRef(
        provider="exhentai.org",
        kind="gallery",
        id=eh_ref.id,
        # raw 改一下，便于日志
        raw=eh_ref.raw.replace("e-hentai.org", "exhentai.org", 1),
    )
    try:
        await placeholder.edit_text("📖 e-hentai 不可用，尝试 ExHentai...")
        gallery = await ex_provider.fetch_work(ex_ref)
        return ex_ref, gallery, ex_provider
    except EHGalleryUnavailable as e2:
        await placeholder.edit_text(
            f"⚠️ 双站均不可用：\n  e-hentai: {eh_error_msg}\n  exhentai: {e2}"
        )
        return None
    except EHError as e2:
        await placeholder.edit_text(f"⚠️ ExHentai 解析失败：{e2}")
        return None
    except Exception as e2:
        logger.exception("exhentai fallback fetch_work failed")
        await placeholder.edit_text(f"⚠️ ExHentai 解析失败：{e2}")
        return None


async def _eh_run_with_mode(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    ref: ParsedRef,
    *,
    mode: EHMode | None,
    placeholder=None,
    extra_buttons: list[InlineKeyboardButton] | None = None,
    force_r2: bool = False,
) -> None:
    """实际执行 eh/ex 抓取与发布。

    mode=None 表示用 provider.default_mode（群聊默认场景）。
    placeholder 是已经存在的可 edit 消息（按钮回调已 edit 过状态）。
    没有 placeholder 时（群聊新建一条），自动 reply 一条。

    e-hentai 抓取过程中如果遇到 unavailable，会自动 fallback 到 exhentai。

    extra_buttons：可选，发布完成后挂在 Telegra.ph 链接消息底部的按钮（一行）。
    供 /ehsearch 流程在 telegraph 完成消息上补一个 [归档下载] 入口。
    缓存命中也走这条挂按钮，行为一致。
    """
    config, registry, publisher, tg_cache, _ = _ctx(context)
    provider = _eh_provider(registry, ref.provider)
    if provider is None:
        return

    if mode is None:
        mode = provider.default_mode

    extras_markup = (
        InlineKeyboardMarkup([extra_buttons]) if extra_buttons else None
    )

    # cache_kind 是 telegraph_cache.kind 字段的实际值。新增 provider / 新 mode 时
    # **必须**同步更新 pixivfeed/storage/cache_keymap.py 的反向映射规则，否则
    # R2 LRU / cache_dir cleanup 清掉底层图片后无法联动失效该行 cache，导致用户
    # 重提相同链接命中坏 URL。
    cache_kind = f"{ref.provider}/gallery/{mode.value}"
    cached = await tg_cache.get(cache_kind, ref.id)
    if cached is not None:
        # PR-2 durability gate：force_r2 admin 在非 durable 行视为 miss 重发；
        # 普通用户命中任何状态都返回 URL，保留旧体验。
        # R2 未启用时 force_r2 不绕过 cache（避免空转重发产出仍非 durable）。
        eff_force = _effective_force_r2(context, force_r2)
        if eff_force and not cached.durable:
            logger.info(
                f"force_r2: cache hit for {cache_kind}[{ref.id}] but durable=False "
                f"(reason={cached.fallback_reason or 'legacy'}); treating as miss"
            )
            # 落到下面的重发分支
        else:
            reply = cached.url
            if eff_force and cached.durable:
                reply = reply + "\n（已是 R2 durable 缓存，跳过重发）"
            if placeholder:
                await placeholder.edit_text(
                    reply, disable_web_page_preview=False, reply_markup=extras_markup,
                )
            else:
                msg = update_or_query.effective_message if hasattr(update_or_query, "effective_message") else None
                if msg:
                    await msg.reply_text(reply, reply_markup=extras_markup)
            return

    if placeholder is None:
        msg = update_or_query.effective_message
        placeholder = await msg.reply_text(f"⏳ {ref.provider} 处理中（{mode.label_zh}）...")
    else:
        try:
            await placeholder.edit_text(f"⏳ {ref.provider} 处理中（{mode.label_zh}）...")
        except Exception:
            pass

    p = Progress(placeholder, prefix=f"📦 {ref.provider} {ref.id} · {mode.label_zh}")
    _attach_progress_markup(p, placeholder)
    # archive 模式走 on_status（富文本带 [N线程] / [单流] 后缀，动态超时也已下沉）；
    # page_* 模式走 on_progress（item 计数）。
    if mode.is_archive:
        dl_hook = None
        dl_status: StatusUpdater = p.update
    else:
        dl_hook = make_item_hook(p, f"{ref.provider} 下载图片")
        dl_status = None

    try:
        gallery = await provider.fetch_and_download_with_mode(
            ref, mode, on_progress=dl_hook, on_status=dl_status,
        )
    except EHGalleryUnavailable as e:
        # e-hentai 阶段不可用 → fallback 到 exhentai
        if ref.provider == "e-hentai.org":
            fallback = await _try_fallback_to_exhentai(context, ref, placeholder, str(e))
            if fallback is None:
                return
            ref, _gallery_meta, provider = fallback
            cache_kind = f"{ref.provider}/gallery/{mode.value}"
            cached = await tg_cache.get(cache_kind, ref.id)
            eff_force = _effective_force_r2(context, force_r2)
            if cached is not None and not (eff_force and not cached.durable):
                # 同主入口的 durability gate：force_r2 在非 durable 行 fall-through 重发
                reply = cached.url
                if eff_force and cached.durable:
                    reply = reply + "\n（已是 R2 durable 缓存，跳过重发）"
                await placeholder.edit_text(
                    reply, disable_web_page_preview=False, reply_markup=extras_markup,
                )
                return
            try:
                await placeholder.edit_text(f"⏳ {ref.provider} 处理中（{mode.label_zh}）...")
            except Exception:
                pass
            # fallback 后重建 hook（prefix 里的 provider 名变了）
            p = Progress(placeholder, prefix=f"📦 {ref.provider} {ref.id} · {mode.label_zh}")
            _attach_progress_markup(p, placeholder)
            if mode.is_archive:
                dl_hook = None
                dl_status = p.update
            else:
                dl_hook = make_item_hook(p, f"{ref.provider} 下载图片")
                dl_status = None
            try:
                gallery = await provider.fetch_and_download_with_mode(
                    ref, mode, on_progress=dl_hook, on_status=dl_status,
                )
            except EHError as e2:
                await placeholder.edit_text(f"⚠️ ExHentai（{mode.label_zh}）失败：{e2}")
                return
            except Exception as e2:
                logger.exception("exhentai fallback fetch_and_download failed")
                await placeholder.edit_text(f"⚠️ ExHentai（{mode.label_zh}）失败：{e2}")
                return
        else:
            await placeholder.edit_text(f"⚠️ {ref.provider}（{mode.label_zh}）失败：{e}")
            return
    except EHError as e:
        await placeholder.edit_text(f"⚠️ {ref.provider}（{mode.label_zh}）失败：{e}")
        return
    except Exception as e:
        logger.exception(f"{ref.provider} fetch_and_download_with_mode({mode}) failed for {ref.id}")
        await placeholder.edit_text(f"⚠️ {ref.provider}（{mode.label_zh}）失败：{e}")
        return

    page_title, page_header, page_footer = _resolve_templates(config, ref.provider)

    await _drop_cancel_button(placeholder)
    pub_hook = make_item_hook(p, "发布 Telegra.ph 页面")
    try:
        pub = await publisher.publish_gallery(
            gallery,
            page_title_template=page_title,
            page_header_template=page_header,
            page_footer_template=page_footer,
            on_progress=pub_hook,
            on_status=p.update,
            force_r2=force_r2,
        )
    except Exception as e:
        logger.exception(f"{ref.provider} publish_gallery failed for {ref.id}")
        await placeholder.edit_text(f"⚠️ 发布失败：{e}")
        await _log_usage(
            context, update_or_query,
            kind=KIND_EH_PAGE if not (mode and mode.is_archive) else KIND_EH_ARCHIVE,
            provider=ref.provider, ref_id=ref.id, status="failed",
        )
        return

    await tg_cache.put(
        cache_kind, ref.id, pub.primary_url,
        page_count=pub.page_count,
        durable=pub.durable,
        r2_image_count=pub.r2_image_count,
        fallback_image_count=pub.fallback_image_count,
        fallback_reason=pub.fallback_reason,
    )
    suffix = _r2_skipped_suffix(pub, r2_enabled=config.storage.r2.enabled)
    await placeholder.edit_text(
        pub.primary_url + suffix,
        disable_web_page_preview=False,
        reply_markup=extras_markup,
        parse_mode=ParseMode.HTML if suffix else None,
    )
    # 用量：消息流走 telegraph 发布。bytes_in 估为图片合计，bytes_out 为 0（没回发文件）。
    total_bytes = 0
    for img in gallery.images:
        try:
            total_bytes += img.local_path.stat().st_size
        except OSError:
            pass
    await _log_usage(
        context, update_or_query,
        kind=KIND_EH_PAGE if not (mode and mode.is_archive) else KIND_EH_ARCHIVE,
        provider=ref.provider, ref_id=ref.id,
        bytes_in=total_bytes,
    )


# ---------------------------------------------------------------------------
# 按钮回调
# ---------------------------------------------------------------------------


async def _drop_cancel_button(placeholder, progress=None) -> None:
    """进入不可取消阶段（上传 / Telegraph 发布）时调用，去掉占位消息上的取消按钮。

    progress 非 None 时同时把它持有的 markup 清掉，避免下次 update 把按钮加回来。
    """
    if placeholder is not None:
        _PLACEHOLDER_MARKUPS.pop(getattr(placeholder, "message_id", -1), None)
    if progress is not None:
        progress.set_markup(None)
    try:
        await placeholder.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def _handle_job_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理 jc:<token> 取消按钮回调。仅 owner 与 admin 可点。"""
    config, *_ = _ctx(context)
    query = update.callback_query
    if query is None or not query.data:
        return
    parts = query.data.split(":", 1)
    if len(parts) != 2 or parts[0] != "jc":
        return
    token = parts[1]
    entry = _CANCEL_TOKENS.get(token)
    if entry is None:
        await query.answer("⚠️ 任务已结束或选项已过期", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    owner_id = entry.get("owner_id", 0)  # type: ignore[union-attr]
    user_id = query.from_user.id if query.from_user else 0
    is_admin = user_id in set(config.auth.admin_users)
    if user_id != owner_id and not is_admin:
        await query.answer("⚠️ 仅任务发起者或 admin 可以取消", show_alert=True)
        return
    handle = entry.get("handle")  # type: ignore[union-attr]
    try:
        status = await handle.cancel()  # type: ignore[union-attr]
    except Exception as e:
        logger.exception("job cancel failed")
        await query.answer(f"⚠️ 取消失败：{e}", show_alert=True)
        return
    _CANCEL_TOKENS.pop(token, None)
    await query.answer({
        "queued": "已取消（排队中）",
        "running": "已请求取消（正在运行）",
        "finished": "任务已结束",
        "already-cancelled": "任务已被取消",
    }.get(status, "已取消"))
    try:
        await query.edit_message_text(
            "❌ 已取消" if status in ("queued", "running") else "任务已结束"
        )
    except Exception:
        pass
    _schedule_delete_after_cancel(context, query.message)


# ---------------------------------------------------------------------------
# 取消按钮 → 延迟清理触发消息 + bot 回复
# ---------------------------------------------------------------------------
#
# 只有"用户主动点取消按钮"才走这条删除路径，避免误删其它历史消息。
# 私聊里 bot 没权限删用户消息 → 一律不删（避免只剩半截的"孤儿"提示）。
# 群组里 bot 必须是 admin 且持 can_delete_messages，否则也跳过。
# 删 bot 自己的消息没权限要求，但配合"全有或全无"的策略，一致跳过。
#
# 删除延迟默认 5s（让用户看到"已取消"反馈）。

_CANCEL_DELETE_DELAY_S = 5.0


def _schedule_delete_after_cancel(
    context: ContextTypes.DEFAULT_TYPE, bot_message,
) -> None:
    """点取消按钮后调一次。bot_message 是带取消按钮的 placeholder。

    会从 bot_message.reply_to_message 推出用户原始触发消息（所有 placeholder
    都是 reply_text 创建的，保证 reply_to_message 是原消息），之后异步等待
    _CANCEL_DELETE_DELAY_S 秒再尝试删除两条。任何权限/状态不足都静默退出。
    """
    if bot_message is None:
        return
    chat = getattr(bot_message, "chat", None)
    if chat is None:
        return
    user_msg = getattr(bot_message, "reply_to_message", None)
    user_msg_id = getattr(user_msg, "message_id", None) if user_msg is not None else None
    if user_msg_id is None:
        return  # 没有原始触发消息可以一起删，按"全有或全无"放弃
    asyncio.create_task(_delete_pair_after_cancel(
        context,
        chat_id=chat.id,
        chat_type=getattr(chat, "type", "") or "",
        bot_msg_id=bot_message.message_id,
        user_msg_id=user_msg_id,
    ))


async def _delete_pair_after_cancel(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    chat_type: str,
    bot_msg_id: int,
    user_msg_id: int,
) -> None:
    """执行"等 N 秒 → 校验权限 → 删两条"。任何步骤失败都静默退出。"""
    # 私聊：bot 永远删不掉用户消息（TG API 限制）→ 直接跳过。
    if chat_type not in ("group", "supergroup"):
        return
    # 群组：要 bot 是 admin 且 can_delete_messages
    try:
        me = await context.bot.get_chat_member(chat_id, context.bot.id)
    except Exception as e:
        logger.debug(f"cancel-delete: get_chat_member failed for chat={chat_id}: {e}")
        return
    status = getattr(me, "status", "")
    if status not in ("administrator", "creator"):
        return
    if status == "administrator" and not getattr(me, "can_delete_messages", False):
        return

    try:
        await asyncio.sleep(_CANCEL_DELETE_DELAY_S)
    except asyncio.CancelledError:
        raise
    except Exception:
        return

    # 走到这里：两条都要尝试删；其中之一失败也继续删另一条
    for mid in (user_msg_id, bot_msg_id):
        try:
            await context.bot.delete_message(chat_id, mid)
        except Exception as e:
            logger.debug(f"cancel-delete: delete_message({chat_id}, {mid}) failed: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """所有 inline button 都进这里。"""
    query = update.callback_query
    if query is None or not query.data:
        return

    # jc: 前缀（job cancel）只有 2 段
    if query.data.startswith("jc:"):
        await _handle_job_cancel(update, context)
        return

    # stg: 前缀（/setting 切换按钮）格式 stg:<key>:<value>，key 含点不含冒号
    if query.data.startswith("stg:"):
        from .setting import handle_setting_callback
        await handle_setting_callback(update, context)
        return

    # ehs_* 前缀（/ehsearch 流程）：按钮数据形态多样，单独分发
    if query.data.startswith("ehs_open:"):
        await _handle_ehs_open(update, context)
        return
    if query.data.startswith("ehs_more:"):
        await _handle_ehs_more(update, context)
        return
    if query.data.startswith("ehs_next:"):
        await _handle_ehs_next(update, context)
        return
    if query.data.startswith("ehs_prev:"):
        await _handle_ehs_prev(update, context)
        return
    if query.data.startswith("ehs_arch_menu:"):
        await _handle_ehs_arch_menu(update, context)
        return
    if query.data.startswith("ehs_back2list:"):
        await _handle_ehs_back2list(update, context)
        return
    if query.data.startswith("ehs_back2det:"):
        await _handle_ehs_back2det(update, context)
        return
    if query.data.startswith("ehs_arch:"):
        # 0.7.0 旧 telegraph 完成消息上挂的 [归档下载] 按钮——保留 backward compat
        await _handle_ehs_arch(update, context)
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer()
        return
    if parts[0] == "eha":
        # 委托给 /archive 流程
        await handle_callback_archive(update, context)
        return
    if parts[0] == "pix":
        # 委托给 pixiv 详情卡回调
        await _handle_pixiv_callback(update, context)
        return
    if parts[0] == "nh":
        # 委托给 nhentai 详情卡回调
        await _handle_nhentai_callback(update, context)
        return
    if parts[0] != "eh":
        await query.answer()
        return

    _, token, mode_str = parts
    pending = _PENDING.get(token)
    if pending is None:
        await query.answer("⚠️ 选项已过期，请重新发送链接", show_alert=True)
        return

    # 防抢按：只有触发者可点
    if query.from_user.id != pending.user_id:
        await query.answer("⚠️ 这个选择来自其他用户", show_alert=True)
        return

    # cancel
    if mode_str == "cancel":
        _PENDING.pop(token, None)
        await query.answer("已取消")
        try:
            await query.edit_message_text("已取消")
        except Exception:
            pass
        _schedule_delete_after_cancel(context, query.message)
        return

    try:
        mode = EHMode(mode_str)
    except ValueError:
        await query.answer("⚠️ 未知模式", show_alert=True)
        return

    _PENDING.pop(token, None)
    await query.answer(f"使用 {mode.label_zh}")

    # 复用原 placeholder（带按钮的那条消息）
    msg = query.message
    try:
        await msg.edit_text(f"⏳ 已收到（{mode.label_zh}），准备处理...")
    except Exception:
        pass

    if not await _gate_disk_space(context, msg):
        return

    user_id = pending.user_id
    # archive_* 模式归 archive_zip 队列；page_* 走 telegraph_publish
    category = "archive_zip" if mode.is_archive else "telegraph_publish"

    async def _do() -> None:
        await _eh_run_with_mode(
            update, context, pending.ref, mode=mode, placeholder=msg,
            force_r2=pending.force_r2,
        )

    await _enqueue(
        context,
        category=category,
        user_id=user_id,
        placeholder=msg,
        work_label=f"{pending.ref.provider} 处理中（{mode.label_zh}）...",
        coro_factory=_do,
    )


# ---------------------------------------------------------------------------
# pixiv illust 分支
# ---------------------------------------------------------------------------


async def _handle_pixiv_illust(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ref: ParsedRef,
    *,
    mode: str,
    force_r2: bool = False,
) -> None:
    config, registry, publisher, tg_cache, _ = _ctx(context)
    pixiv = _pixiv_provider(registry)
    if pixiv is None:
        return
    pid = ref.id

    try:
        work = await pixiv.fetch_illust(pid)
    except PixivNotFoundError:
        await update.message.reply_text(f"⚠️ 作品 {pid} 不存在或已删除")
        return
    except PixivAuthError as e:
        await update.message.reply_text(f"⚠️ 需要登录才能查看（PHPSESSID 可能失效）：{e}")
        return
    except PixivAPIError as e:
        logger.exception(f"fetch_illust({pid}) failed")
        await update.message.reply_text(f"⚠️ 拉取作品失败：{e}")
        return

    if work.is_ugoira:
        await update.message.reply_text(
            f"⚠️ 暂不支持动图（ugoira）：https://www.pixiv.net/artworks/{pid}"
        )
        return

    if mode == "auto":
        final_mode = "direct" if work.page_count <= config.publish.direct_threshold else "ph"
    else:
        final_mode = mode

    if final_mode == "direct" and work.page_count > 10:
        logger.info(
            f"[{pid}] direct mode requested but page_count={work.page_count} > 10, fallback to ph"
        )
        final_mode = "ph"

    if final_mode == "ph":
        # 拉队列；pixiv illust 通过 telegraph_publish 类别走
        placeholder = await update.message.reply_text(f"⏳ 已收到（pixiv {pid}），准备处理...")
        if not await _gate_disk_space(context, placeholder):
            return
        user_id = update.effective_user.id if update.effective_user else 0

        async def _do_ph() -> None:
            await _send_pixiv_illust_via_telegraph(
                update, context, pid, placeholder=placeholder, force_r2=force_r2,
            )

        await _enqueue(
            context,
            category="telegraph_publish",
            user_id=user_id,
            placeholder=placeholder,
            work_label=f"pixiv {pid} 处理中...",
            coro_factory=_do_ph,
        )
    else:
        # 直发图片走 direct_image 队列
        placeholder = await update.message.reply_text(f"⏳ 已收到（pixiv {pid}），准备处理...")
        if not await _gate_disk_space(context, placeholder):
            return
        user_id = update.effective_user.id if update.effective_user else 0

        async def _do_direct() -> None:
            await _send_pixiv_illust_direct(update, context, pid, placeholder=placeholder)

        await _enqueue(
            context,
            category="direct_image",
            user_id=user_id,
            placeholder=placeholder,
            work_label=f"pixiv {pid} 下载图片中...",
            coro_factory=_do_direct,
        )


# ---------------------------------------------------------------------------
# pixiv illust 私聊详情卡：标题 / 张数 / x_restrict / ~XX MB + 模式按钮
# ---------------------------------------------------------------------------
#
# 触发条件：私聊 + mode == "auto"。其它路径（群聊 / /pixiv_direct / /pixiv_telegraph）
# 全部跳过详情卡，照旧行为。
#
# 按钮：
#   - page_count == 1                  → [直发图片] [Telegra.ph] [取消]
#   - 1 < page_count <= direct_threshold → [直发图片] [Telegra.ph] [取消]
#   - page_count > 10                  → [Telegra.ph] [取消]（direct 强制不可用）
#   - 否则                              → [直发图片] [Telegra.ph] [取消]
#
# callback_data：pix:{token}:{action}，action ∈ {direct, ph, cancel}。
# `_Pending` 复用 eh/ex 那套，ref.provider == "pixiv"。


def _render_pixiv_detail_card(
    work, *, total_bytes: int | None,
) -> str:
    """私聊 Pixiv 详情卡正文。

    `total_bytes is None` 时不渲染大小行（prefetch 失败 / disabled / 还没回来）；
    `>= 0` 时渲染 "~XX MB"（0 也渲染，表示估算到了 0，调用方一般不会传 0）。

    title/author/tag 全部走 `_html_escape`（处理 `&` `<` `>`）；裸 `&` 会让
    Telegram HTML parse_mode 报 Bad Request: can't parse entities。
    """
    title = _html_escape(work.title or "")
    author = _html_escape(work.author or "")
    lines = [
        f"<b>{title}</b>",
        f"画师：<a href=\"https://www.pixiv.net/users/{work.user_id}\">{author}</a>",
    ]
    pages_line = f"张数：{work.page_count}"
    if total_bytes is not None and total_bytes > 0:
        pages_line += f" · 预估约 {fmt_bytes(total_bytes)}"
    lines.append(pages_line)
    badges: list[str] = []
    if work.x_restrict_label:
        badges.append(_html_escape(work.x_restrict_label))
    if work.ai_type_label:
        badges.append(_html_escape(work.ai_type_label))
    if badges:
        lines.append("标记：" + " · ".join(badges))
    if work.tags:
        tag_text = " ".join(f"#{_html_escape(t)}" for t in work.tags[:8])
        lines.append(f"标签：{tag_text}")
    lines.append("")
    lines.append(f"<a href=\"https://www.pixiv.net/artworks/{work.pid}\">在 Pixiv 查看原作</a>")
    lines.append("")
    lines.append("选择处理方式：")
    return "\n".join(lines)


def _make_pixiv_keyboard(
    token: str, work, config: Config,
) -> InlineKeyboardMarkup:
    """根据 page_count + direct_threshold 决定显示哪些按钮。"""
    rows: list[list[InlineKeyboardButton]] = []
    # page_count > 10 时强制不发 direct（与 _handle_pixiv_illust 内部一致）
    can_direct = work.page_count <= 10
    btn_row: list[InlineKeyboardButton] = []
    if can_direct:
        btn_row.append(InlineKeyboardButton("📷 直发图片", callback_data=f"pix:{token}:direct"))
    btn_row.append(InlineKeyboardButton("📰 Telegra.ph", callback_data=f"pix:{token}:ph"))
    rows.append(btn_row)
    rows.append([InlineKeyboardButton("取消", callback_data=f"pix:{token}:cancel")])
    return InlineKeyboardMarkup(rows)


def _schedule_pixiv_size_prefetch(
    context: ContextTypes.DEFAULT_TYPE,
    placeholder,
    token: str,
    work,
) -> None:
    """异步采样 i.pximg.net 头几张原图，估算总字节数后回填详情卡正文。"""
    config, *_ = _ctx(context)
    sp = config.size_prefetch
    if not sp.enabled or not sp.pixiv:
        return
    # 没图片 URL 的情况（fetch_illust 已派发 pages，但保险起见）
    if not work.images:
        return
    urls = [img.original for img in work.images if getattr(img, "original", None)]
    if not urls:
        return

    async def _run() -> None:
        try:
            # i.pximg.net 严格校验 Referer = www.pixiv.net；其它 host 可能无 HEAD/Range，
            # estimate_total_bytes 内部会 HEAD → Range fallback。
            import httpx
            async with httpx.AsyncClient(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
                timeout=sp.timeout,
                follow_redirects=True,
            ) as client:
                total = await estimate_total_bytes(
                    client, urls,
                    referer="https://www.pixiv.net/",
                    sample_count=sp.sample_count,
                    timeout=float(sp.timeout),
                )
        except Exception as e:
            logger.debug(f"pixiv size prefetch {work.pid} failed: {e}")
            return
        if total is None or total <= 0:
            return
        new_text = _render_pixiv_detail_card(work, total_bytes=total)
        new_markup = _make_pixiv_keyboard(token, work, config)
        await _safe_update_card(placeholder, token, new_text, new_markup)

    asyncio.create_task(_run())


async def _pixiv_offer_modes(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ref: ParsedRef,
    *, force_r2: bool = False,
) -> None:
    """私聊粘 pixiv illust 链接 + mode=='auto' 时弹详情卡。

    与 `_eh_offer_modes` 对齐：fetch 元数据 → 写卡 + 按钮 → 异步 prefetch 大小。
    """
    config, registry, *_ = _ctx(context)
    pixiv = _pixiv_provider(registry)
    if pixiv is None:
        return
    pid = ref.id

    placeholder = await update.message.reply_text("📖 解析中...")
    try:
        work = await pixiv.fetch_illust(pid)
    except PixivNotFoundError:
        await placeholder.edit_text(f"⚠️ 作品 {pid} 不存在或已删除")
        return
    except PixivAuthError as e:
        await placeholder.edit_text(f"⚠️ 需要登录才能查看（PHPSESSID 可能失效）：{e}")
        return
    except PixivAPIError as e:
        logger.exception(f"fetch_illust({pid}) failed")
        await placeholder.edit_text(f"⚠️ 拉取作品失败：{e}")
        return

    if work.is_ugoira:
        await placeholder.edit_text(
            f"⚠️ 暂不支持动图（ugoira）：https://www.pixiv.net/artworks/{pid}"
        )
        return

    _gc_pending()
    token = uuid.uuid4().hex[:10]
    orig_msg = update.effective_message
    _PENDING[token] = _Pending(
        ref=ref,
        chat_id=placeholder.chat.id,
        msg_id=placeholder.message_id,
        user_id=update.effective_user.id,
        created_at=time.time(),
        force_r2=force_r2,
        orig_chat_id=orig_msg.chat.id if orig_msg else None,
        orig_msg_id=orig_msg.message_id if orig_msg else None,
    )

    text = _render_pixiv_detail_card(work, total_bytes=None)
    keyboard = _make_pixiv_keyboard(token, work, config)
    await placeholder.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    _schedule_pixiv_size_prefetch(context, placeholder, token, work)


async def _handle_pixiv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """pix:{token}:{action} 回调入口。"""
    query = update.callback_query
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer()
        return
    _, token, action = parts

    pending = _PENDING.get(token)
    if pending is None:
        await query.answer("⚠️ 选项已过期，请重新发送链接", show_alert=True)
        return
    if query.from_user.id != pending.user_id:
        await query.answer("⚠️ 这个选择来自其他用户", show_alert=True)
        return

    if action == "cancel":
        _PENDING.pop(token, None)
        await query.answer("已取消")
        try:
            await query.edit_message_text("已取消")
        except Exception:
            pass
        _schedule_delete_after_cancel(context, query.message)
        return

    if action not in ("direct", "ph"):
        await query.answer("⚠️ 未知操作", show_alert=True)
        return

    _PENDING.pop(token, None)
    label = "直发图片" if action == "direct" else "Telegra.ph"
    await query.answer(f"使用 {label}")

    # 复用同一条消息（详情卡）做 placeholder。
    msg = query.message
    try:
        await msg.edit_text(f"⏳ 已收到（pixiv {pending.ref.id} · {label}），准备处理...")
    except Exception:
        pass

    if not await _gate_disk_space(context, msg):
        return

    pid = pending.ref.id
    user_id = pending.user_id
    force_r2 = pending.force_r2
    orig_chat_id = pending.orig_chat_id
    orig_msg_id = pending.orig_msg_id

    if action == "ph":
        async def _do_ph() -> None:
            await _send_pixiv_illust_via_telegraph(
                update, context, pid, placeholder=msg, force_r2=force_r2,
            )

        await _enqueue(
            context,
            category="telegraph_publish",
            user_id=user_id,
            placeholder=msg,
            work_label=f"pixiv {pid} 处理中...",
            coro_factory=_do_ph,
        )
    else:
        # direct：图片 reply 到用户原始消息，而不是即将被 delete 的详情卡 placeholder
        async def _do_direct() -> None:
            await _send_pixiv_illust_direct(
                update, context, pid, placeholder=msg,
                reply_to_message_id=orig_msg_id,
                reply_to_chat_id=orig_chat_id,
            )

        await _enqueue(
            context,
            category="direct_image",
            user_id=user_id,
            placeholder=msg,
            work_label=f"pixiv {pid} 下载图片中...",
            coro_factory=_do_direct,
        )


# ---------------------------------------------------------------------------
# nhentai 私聊详情卡：标题 / 张数 / ~XX MB / tags + [开始下载] [取消]
# ---------------------------------------------------------------------------
#
# nhentai 只有"发 Telegra.ph"一种处理路径（没有直发选项），所以按钮只有
# [开始下载] / [取消] 两个。callback_data：nh:{token}:{action}，action ∈ {go, cancel}。
#
# 与 Pixiv 一致，size prefetch 写在正文里。开关在 size_prefetch.nhentai。


def _render_nhentai_detail_card(
    album: NHentaiAlbum, *, total_bytes: int | None,
) -> str:
    """nhentai 详情卡正文。total_bytes is None 表示不显示大小行。

    title/tag 走 `_html_escape`（处理 `&` `<` `>`）；裸 `&` 会让 TG HTML
    parse_mode 报 Bad Request。
    """
    title = _html_escape(album.title or "")
    lines = [
        f"<b>{title}</b>",
    ]
    pages_line = f"张数：{album.num_pages}"
    if total_bytes is not None and total_bytes > 0:
        pages_line += f" · 预估约 {fmt_bytes(total_bytes)}"
    lines.append(pages_line)
    if album.tags:
        # tags 太多就截前 12 个，TG 4096 字符不会塞太满。
        # tag 里的空格转下划线（hashtag 习惯）后还要 escape 处理 & 等字符。
        tag_text = " ".join(
            f"#{_html_escape(t.replace(' ', '_'))}" for t in album.tags[:12]
        )
        lines.append(f"标签：{tag_text}")
    lines.append("")
    lines.append(f"<a href=\"https://nhentai.net/g/{album.gallery_id}\">原作品</a>")
    lines.append("")
    lines.append("选择操作：")
    return "\n".join(lines)


def _make_nhentai_keyboard(token: str) -> InlineKeyboardMarkup:
    # plan v3 Phase 4：文案统一为"开始下载"（与 eh 详情卡的下载按钮口径一致），
    # nhentai 只有 telegraph 一条路径，所以不暴露发布渠道细节。
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 开始下载", callback_data=f"nh:{token}:go")],
        [InlineKeyboardButton("取消", callback_data=f"nh:{token}:cancel")],
    ])


def _nhentai_sample_urls(album: NHentaiAlbum, sample_count: int) -> list[str]:
    """生成前 N 张图的代表 URL（主 CDN 第一项）用于 HEAD/Range 采样。

    nhentai CDN i1~i4 同一张图同 path，HEAD 失败率较高 → 用 estimate_total_bytes
    内部的 Range fallback 兜底。
    """
    n = min(sample_count, album.num_pages)
    if n <= 0:
        return []
    urls: list[str] = []
    base = NHENTAI_CDNS[0]
    for idx in range(1, n + 1):
        t = album.page_types[idx - 1] if idx - 1 < len(album.page_types) else "j"
        ext = {"j": ".jpg", "p": ".png", "g": ".gif", "w": ".webp"}.get(t, ".jpg")
        urls.append(f"{base}/{album.media_id}/{idx}{ext}")
    return urls


def _schedule_nhentai_size_prefetch(
    context: ContextTypes.DEFAULT_TYPE,
    placeholder,
    token: str,
    album: NHentaiAlbum,
) -> None:
    """异步采样 nhentai CDN 头几张图，估算总字节数后回填详情卡正文。"""
    config, *_ = _ctx(context)
    sp = config.size_prefetch
    if not sp.enabled or not sp.nhentai:
        return
    sample_urls = _nhentai_sample_urls(album, sp.sample_count)
    if not sample_urls:
        return

    async def _run() -> None:
        try:
            import httpx
            async with httpx.AsyncClient(
                timeout=sp.timeout,
                follow_redirects=True,
            ) as client:
                avg = await estimate_total_bytes(
                    client, sample_urls,
                    sample_count=len(sample_urls),
                    timeout=float(sp.timeout),
                )
        except Exception as e:
            logger.debug(f"nhentai size prefetch {album.gallery_id} failed: {e}")
            return
        if avg is None or avg <= 0:
            return
        # estimate_total_bytes 给的 total 是按 len(urls) 算的，这里 urls 只是采样池，
        # 需要用单张均值 * 总页数才是估算值
        per_image = avg / len(sample_urls)
        total = int(per_image * album.num_pages)
        if total <= 0:
            return
        new_text = _render_nhentai_detail_card(album, total_bytes=total)
        new_markup = _make_nhentai_keyboard(token)
        await _safe_update_card(placeholder, token, new_text, new_markup)

    asyncio.create_task(_run())


async def _nhentai_offer_modes(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ref: ParsedRef,
    *, force_r2: bool = False,
) -> None:
    """私聊粘 nhentai 链接时弹详情卡。"""
    config, registry, *_ = _ctx(context)
    provider = registry.find_by_name("nhentai")
    if not isinstance(provider, NHentaiProvider):
        # nhentai 未启用：回退到泛型 telegraph 路径
        placeholder = await update.message.reply_text(f"⏳ 已收到（{ref.provider}），准备处理...")
        if not await _gate_disk_space(context, placeholder):
            return
        user_id = update.effective_user.id if update.effective_user else 0

        async def _do_generic() -> None:
            await _send_via_telegraph_generic(
                update, context, ref, placeholder=placeholder, force_r2=force_r2,
            )

        await _enqueue(
            context,
            category="telegraph_publish",
            user_id=user_id,
            placeholder=placeholder,
            work_label=f"{ref.provider} 处理中...",
            coro_factory=_do_generic,
        )
        return

    placeholder = await update.message.reply_text("📖 解析中...")
    try:
        album = await provider.fetch_work(ref)
    except NHentaiError as e:
        await placeholder.edit_text(f"⚠️ 解析失败：{e}")
        return
    except Exception as e:
        logger.exception(f"nhentai fetch_work({ref.id}) failed")
        await placeholder.edit_text(f"⚠️ 解析失败：{e}")
        return

    _gc_pending()
    token = uuid.uuid4().hex[:10]
    orig_msg = update.effective_message
    _PENDING[token] = _Pending(
        ref=ref,
        chat_id=placeholder.chat.id,
        msg_id=placeholder.message_id,
        user_id=update.effective_user.id,
        created_at=time.time(),
        force_r2=force_r2,
        orig_chat_id=orig_msg.chat.id if orig_msg else None,
        orig_msg_id=orig_msg.message_id if orig_msg else None,
    )

    text = _render_nhentai_detail_card(album, total_bytes=None)
    keyboard = _make_nhentai_keyboard(token)
    await placeholder.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    _schedule_nhentai_size_prefetch(context, placeholder, token, album)


async def _handle_nhentai_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """nh:{token}:{action} 回调入口。"""
    query = update.callback_query
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer()
        return
    _, token, action = parts

    pending = _PENDING.get(token)
    if pending is None:
        await query.answer("⚠️ 选项已过期，请重新发送链接", show_alert=True)
        return
    if query.from_user.id != pending.user_id:
        await query.answer("⚠️ 这个选择来自其他用户", show_alert=True)
        return

    if action == "cancel":
        _PENDING.pop(token, None)
        await query.answer("已取消")
        try:
            await query.edit_message_text("已取消")
        except Exception:
            pass
        _schedule_delete_after_cancel(context, query.message)
        return

    if action != "go":
        await query.answer("⚠️ 未知操作", show_alert=True)
        return

    _PENDING.pop(token, None)
    await query.answer("开始处理")
    msg = query.message
    try:
        await msg.edit_text(f"⏳ 已收到（nhentai {pending.ref.id}），准备处理...")
    except Exception:
        pass

    if not await _gate_disk_space(context, msg):
        return

    ref = pending.ref
    user_id = pending.user_id
    force_r2 = pending.force_r2

    async def _do() -> None:
        await _send_via_telegraph_generic(
            update, context, ref, placeholder=msg, force_r2=force_r2,
        )

    await _enqueue(
        context,
        category="telegraph_publish",
        user_id=user_id,
        placeholder=msg,
        work_label=f"nhentai {ref.id} 处理中...",
        coro_factory=_do,
    )


# ---------------------------------------------------------------------------
# 通用 telegraph（nhentai 等）
# ---------------------------------------------------------------------------


async def _send_via_telegraph_generic(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ref: ParsedRef,
    placeholder=None,
    *,
    force_r2: bool = False,
) -> None:
    config, registry, publisher, tg_cache, _ = _ctx(context)

    cache_kind = f"{ref.provider}/{ref.kind}"
    cached = await tg_cache.get(cache_kind, ref.id)
    eff_force = _effective_force_r2(context, force_r2)
    if cached is not None and not (eff_force and not cached.durable):
        reply = cached.url
        if eff_force and cached.durable:
            reply = reply + "\n（已是 R2 durable 缓存，跳过重发）"
        if placeholder is not None:
            await placeholder.edit_text(reply)
        else:
            await update.message.reply_text(reply)
        return

    if placeholder is None:
        placeholder = await update.message.reply_text(f"⏳ {ref.provider} 处理中...")
    provider = registry.find_by_name(ref.provider)
    if provider is None:
        await placeholder.edit_text(f"⚠️ Provider {ref.provider!r} 未启用")
        return

    p = Progress(placeholder, prefix=f"📦 {ref.provider} {ref.id}")
    _attach_progress_markup(p, placeholder)
    dl_hook = make_item_hook(p, f"{ref.provider} 下载图片")

    try:
        gallery = await provider.fetch_and_download(ref, on_progress=dl_hook)
    except Exception as e:
        logger.exception(f"{ref.provider} fetch_and_download failed for {ref.id}")
        await placeholder.edit_text(f"⚠️ 解析/下载失败：{e}")
        await _log_usage(
            context, update,
            kind=KIND_NHENTAI if ref.provider == "nhentai" else KIND_EH_PAGE,
            provider=ref.provider, ref_id=ref.id, status="failed",
        )
        return

    page_title, page_header, page_footer = _resolve_templates(config, ref.provider)

    await _drop_cancel_button(placeholder)
    pub_hook = make_item_hook(p, "发布 Telegra.ph 页面")
    try:
        pub = await publisher.publish_gallery(
            gallery,
            page_title_template=page_title,
            page_header_template=page_header,
            page_footer_template=page_footer,
            on_progress=pub_hook,
            on_status=p.update,
            force_r2=force_r2,
        )
    except Exception as e:
        logger.exception(f"{ref.provider} publish_gallery failed for {ref.id}")
        await placeholder.edit_text(f"⚠️ 发布失败：{e}")
        await _log_usage(
            context, update,
            kind=KIND_NHENTAI if ref.provider == "nhentai" else KIND_EH_PAGE,
            provider=ref.provider, ref_id=ref.id, status="failed",
        )
        return

    await tg_cache.put(
        cache_kind, ref.id, pub.primary_url,
        page_count=pub.page_count,
        durable=pub.durable,
        r2_image_count=pub.r2_image_count,
        fallback_image_count=pub.fallback_image_count,
        fallback_reason=pub.fallback_reason,
    )
    suffix = _r2_skipped_suffix(pub, r2_enabled=config.storage.r2.enabled)
    await placeholder.edit_text(
        pub.primary_url + suffix,
        parse_mode=ParseMode.HTML if suffix else None,
    )
    total_bytes = 0
    for img in gallery.images:
        try:
            total_bytes += img.local_path.stat().st_size
        except OSError:
            pass
    await _log_usage(
        context, update,
        kind=KIND_NHENTAI if ref.provider == "nhentai" else KIND_EH_PAGE,
        provider=ref.provider, ref_id=ref.id,
        bytes_in=total_bytes,
    )


def _resolve_templates(config: Config, provider_name: str) -> tuple[str, str, str]:
    if provider_name == "pixiv":
        t = config.templates.illust
    else:
        t = config.templates.gallery
    return t.page_title, t.page_header, t.page_footer


# ---------------------------------------------------------------------------
# pixiv 专属 telegraph / 直发 / novel
# ---------------------------------------------------------------------------


async def _send_pixiv_illust_via_telegraph(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pid: str,
    placeholder=None,
    *,
    force_r2: bool = False,
) -> None:
    """走 Telegra.ph 流程发布 pixiv illust。

    注意：这个函数不需要 reply_to_message_id —— 它不发新消息，只 `placeholder.edit_text`
    把 telegra.ph URL 写进 placeholder。详情卡按钮回调里直接复用同一条 placeholder
    （即详情卡）就行；reply_to 仅是 direct sender 的痛点。
    """
    config, registry, publisher, tg_cache, _ = _ctx(context)
    pixiv = _pixiv_provider(registry)
    assert pixiv is not None

    cached = await tg_cache.get("pixiv/illust", pid)
    eff_force = _effective_force_r2(context, force_r2)
    if cached is not None and not (eff_force and not cached.durable):
        reply = cached.url
        if eff_force and cached.durable:
            reply = reply + "\n（已是 R2 durable 缓存，跳过重发）"
        if placeholder is not None:
            await placeholder.edit_text(reply)
        else:
            await update.message.reply_text(reply)
        return

    if placeholder is None:
        placeholder = await update.message.reply_text("⏳ 处理中...")
    p = Progress(placeholder, prefix=f"🖼️ pixiv {pid}")
    _attach_progress_markup(p, placeholder)
    dl_hook = make_item_hook(p, "下载图片")
    try:
        ref = ParsedRef(provider="pixiv", kind="illust", id=pid, raw=pid)
        gallery = await pixiv.fetch_and_download(ref, on_progress=dl_hook)
    except PixivAPIError as e:
        await placeholder.edit_text(f"⚠️ {e}")
        await _log_usage(
            context, update, kind=KIND_PIXIV_TELEGRAPH, provider="pixiv",
            ref_id=pid, status="failed",
        )
        return
    except Exception as e:
        logger.exception(f"pixiv fetch_and_download({pid}) failed")
        await placeholder.edit_text(f"⚠️ 处理失败：{e}")
        await _log_usage(
            context, update, kind=KIND_PIXIV_TELEGRAPH, provider="pixiv",
            ref_id=pid, status="failed",
        )
        return

    t = config.templates.illust
    await _drop_cancel_button(placeholder)
    pub_hook = make_item_hook(p, "发布 Telegra.ph 页面")
    try:
        pub = await publisher.publish_gallery(
            gallery,
            page_title_template=t.page_title,
            page_header_template=t.page_header,
            page_footer_template=t.page_footer,
            on_progress=pub_hook,
            on_status=p.update,
            force_r2=force_r2,
        )
    except Exception as e:
        logger.exception(f"publish pixiv illust({pid}) failed")
        await placeholder.edit_text(f"⚠️ 发布失败：{e}")
        await _log_usage(
            context, update, kind=KIND_PIXIV_TELEGRAPH, provider="pixiv",
            ref_id=pid, status="failed",
        )
        return

    await tg_cache.put(
        "pixiv/illust", pid, pub.primary_url,
        page_count=pub.page_count,
        durable=pub.durable,
        r2_image_count=pub.r2_image_count,
        fallback_image_count=pub.fallback_image_count,
        fallback_reason=pub.fallback_reason,
    )
    suffix = _r2_skipped_suffix(pub, r2_enabled=config.storage.r2.enabled)
    await placeholder.edit_text(
        pub.primary_url + suffix,
        parse_mode=ParseMode.HTML if suffix else None,
    )
    total_bytes = 0
    for img in gallery.images:
        try:
            total_bytes += img.local_path.stat().st_size
        except OSError:
            pass
    await _log_usage(
        context, update, kind=KIND_PIXIV_TELEGRAPH, provider="pixiv",
        ref_id=pid, bytes_in=total_bytes,
    )


async def _send_pixiv_novel(
    update: Update, context: ContextTypes.DEFAULT_TYPE, nid: str,
    placeholder=None,
    *,
    force_r2: bool = False,
) -> None:
    config, registry, publisher, tg_cache, _ = _ctx(context)
    pixiv = _pixiv_provider(registry)
    if pixiv is None:
        return

    cached = await tg_cache.get("pixiv/novel", nid)
    eff_force = _effective_force_r2(context, force_r2)
    if cached is not None and not (eff_force and not cached.durable):
        # 同主入口的 durability gate：force_r2 在非 durable 行 fall-through 重发
        reply = cached.url
        if eff_force and cached.durable:
            reply = reply + "\n（已是 R2 durable 缓存，跳过重发）"
        if placeholder is not None:
            await placeholder.edit_text(reply)
        else:
            await update.message.reply_text(reply)
        return

    if placeholder is None:
        placeholder = await update.message.reply_text("⏳ 处理小说中（可能需要较长时间）...")
    progress = Progress(placeholder, prefix=f"📖 pixiv novel {nid}")
    # novel 流程包含创建多页 telegraph，半路取消会留半成品。整段不可取消。
    await _drop_cancel_button(placeholder)
    try:
        novel, pub = await publish_novel(
            config, publisher, pixiv, nid, progress=progress, force_r2=force_r2,
        )
    except PixivNotFoundError:
        await placeholder.edit_text(f"⚠️ 小说 {nid} 不存在或已删除")
        await _log_usage(context, update, kind=KIND_PIXIV_NOVEL, provider="pixiv",
                         ref_id=nid, status="failed")
        return
    except PixivAuthError as e:
        await placeholder.edit_text(f"⚠️ 需要登录：{e}")
        await _log_usage(context, update, kind=KIND_PIXIV_NOVEL, provider="pixiv",
                         ref_id=nid, status="failed")
        return
    except PixivAPIError as e:
        await placeholder.edit_text(f"⚠️ {e}")
        await _log_usage(context, update, kind=KIND_PIXIV_NOVEL, provider="pixiv",
                         ref_id=nid, status="failed")
        return
    except Exception as e:
        logger.exception(f"publish_novel({nid}) failed")
        await placeholder.edit_text(f"⚠️ 发布失败：{e}")
        await _log_usage(context, update, kind=KIND_PIXIV_NOVEL, provider="pixiv",
                         ref_id=nid, status="failed")
        return

    # PR-3 接通后 novel publisher 也走共享 resolver，cache 行带真实 durability 元数据
    await tg_cache.put(
        "pixiv/novel", nid, pub.primary_url,
        page_count=pub.page_count,
        durable=pub.durable,
        r2_image_count=pub.r2_image_count,
        fallback_image_count=pub.fallback_image_count,
        fallback_reason=pub.fallback_reason,
    )
    suffix = _r2_skipped_suffix(pub, r2_enabled=config.storage.r2.enabled)
    await placeholder.edit_text(
        pub.primary_url + suffix,
        parse_mode=ParseMode.HTML if suffix else None,
    )
    await _log_usage(context, update, kind=KIND_PIXIV_NOVEL, provider="pixiv",
                     ref_id=nid)


def _render_direct_caption(template: str, vars: dict) -> str:
    if not template:
        return ""
    try:
        text = template.format(**vars)
    except (KeyError, IndexError, ValueError):
        text = template
    if len(text) > 1024:
        text = text[:1020] + "..."
    return text.strip()


async def _send_pixiv_illust_direct(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pid: str,
    placeholder=None,
    *,
    reply_to_message_id: int | None = None,
    reply_to_chat_id: int | None = None,
) -> None:
    """直发 pixiv illust 图片。

    `reply_to_message_id`/`reply_to_chat_id`：覆盖默认的 reply_to。
      - 经过详情卡按钮回调进来时，`update.effective_message` 是 bot 的详情卡，
        不是用户原始消息——而且函数尾部会 `placeholder.delete()`，图片就 reply
        到一条即将被删的消息上。caller 显式传 `pending.orig_chat_id` /
        `pending.orig_msg_id` 覆盖。
      - 直接命令路径（/pixiv_direct）或 `mode=='auto'` 不弹卡时，
        `reply_to_message_id=None`，照原行为用 `update.effective_message.message_id`。
    """
    config, registry, _, _, _ = _ctx(context)
    pixiv = _pixiv_provider(registry)
    assert pixiv is not None

    if placeholder is None:
        placeholder = await update.message.reply_text("⏳ 下载图片中...")
    p = Progress(placeholder, prefix=f"🖼️ pixiv {pid}")
    _attach_progress_markup(p, placeholder)
    dl_hook = make_item_hook(p, "下载图片")
    try:
        illust = await pixiv.fetch_and_download_illust(pid, on_progress=dl_hook)
    except PixivAPIError as e:
        await placeholder.edit_text(f"⚠️ {e}")
        return
    except Exception as e:
        logger.exception(f"fetch_and_download_illust({pid}) failed")
        await placeholder.edit_text(f"⚠️ 下载失败：{e}")
        return

    caption = _render_direct_caption(
        config.templates.illust.direct_caption, illust.work.template_vars()
    )
    # 默认走 update.effective_message（与历史行为一致），caller 想覆盖时传 kw 参数。
    chat_id = reply_to_chat_id if reply_to_chat_id is not None else update.effective_chat.id
    if reply_to_message_id is not None:
        reply_to = reply_to_message_id
    else:
        reply_to = update.effective_message.message_id
    # R-18 / R-18G 在群聊里默认加 spoiler 遮罩，私聊不加（私聊就是为了直接看）。
    # x_restrict 0=全年龄, 1=R-18, 2=R-18G
    chat = update.effective_chat
    is_group = chat is not None and chat.type in ("group", "supergroup")
    use_spoiler = is_group and (illust.work.x_restrict or 0) >= 1

    try:
        if len(illust.images) == 1:
            img = illust.images[0]
            with open(img.tgphoto_path, "rb") as f:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=caption or None,
                    parse_mode=ParseMode.HTML if caption else None,
                    reply_to_message_id=reply_to,
                    has_spoiler=use_spoiler,
                )
        else:
            media: list[InputMediaPhoto] = []
            files = []
            try:
                for i, img in enumerate(illust.images):
                    f = open(img.tgphoto_path, "rb")
                    files.append(f)
                    if i == 0 and caption:
                        media.append(InputMediaPhoto(
                            media=f, caption=caption, parse_mode=ParseMode.HTML,
                            has_spoiler=use_spoiler,
                        ))
                    else:
                        media.append(InputMediaPhoto(media=f, has_spoiler=use_spoiler))
                await context.bot.send_media_group(
                    chat_id=chat_id, media=media, reply_to_message_id=reply_to,
                )
            finally:
                for f in files:
                    f.close()
    except Exception as e:
        logger.exception(f"send_direct({pid}) failed")
        try:
            await placeholder.edit_text(f"⚠️ 发送失败：{e}")
        except Exception:
            pass
        await _log_usage(
            context, update,
            kind="pixiv_direct", provider="pixiv", ref_id=pid, status="failed",
        )
        return

    try:
        await placeholder.delete()
    except Exception:
        pass

    # 记录用量：直发 = 仅外发字节，无 GP 消耗
    bytes_out = 0
    for img in illust.images:
        try:
            bytes_out += img.tgphoto_path.stat().st_size
        except OSError:
            pass
    await _log_usage(
        context, update,
        kind="pixiv_direct", provider="pixiv", ref_id=pid,
        bytes_out=bytes_out,
    )


# ---------------------------------------------------------------------------
# /zip2tph：把上传的 zip 图片包发布为 Telegra.ph
# ---------------------------------------------------------------------------
#
# 触发方式：
#   1) 私聊/群聊：直接给 bot 发送 zip 文件，并把 caption 写成 /zip2tph 或在 zip
#      reply 一条 /zip2tph 命令
#   2) 命令本身（无附件）回复 zip 也支持
#
# zip 内只接受图片文件（jpg/jpeg/png/gif/webp）；图片按文件名字典序排序。
# 上传到 cache_dir 下独立子目录，交给 Nginx 暴露给 Telegra.ph。

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _is_zip(document) -> bool:
    """文档是否像一个 zip。"""
    if document is None:
        return False
    name = (document.file_name or "").lower()
    mime = (document.mime_type or "").lower()
    if name.endswith(".zip"):
        return True
    if mime in ("application/zip", "application/x-zip-compressed", "application/octet-stream"):
        return name.endswith(".zip") or mime.startswith("application/zip")
    return False


async def cmd_zip2tph(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/zip2tph`：处理回复或带 caption 的 zip。

    可选 admin flag `--r2` 强制把图传 R2（绕过 max_upload_size_gb 护栏）。
    """
    config, registry, publisher, _, allowlist = _ctx(context)
    if not await is_authorized(update, allowlist):
        return
    await _track_user(update, context)

    user_id = update.effective_user.id if update.effective_user else 0
    raw_args = list(context.args or [])
    _, force_r2 = _parse_r2_flag(raw_args, user_id, config.auth.admin_users)

    msg = update.effective_message
    target_msg = msg.reply_to_message if msg.reply_to_message else msg
    document = target_msg.document if target_msg else None
    if not _is_zip(document):
        await msg.reply_text(
            "用法：把图片 zip 发给我并在 caption 里写 /zip2tph，"
            "或对 zip 消息回复 /zip2tph"
        )
        return
    await _enqueue_zip_to_telegraph(update, context, target_msg, force_r2=force_r2)


async def handle_zip_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """监听 Document：caption 含 /zip2tph 时自动处理。"""
    config, _, _, _, allowlist = _ctx(context)
    if not await is_authorized(update, allowlist):
        return
    await _track_user(update, context)
    msg = update.effective_message
    if not _is_zip(msg.document):
        return
    caption = (msg.caption or "").strip()
    if not caption.lower().startswith("/zip2tph"):
        return
    # caption 形如 "/zip2tph --r2"；admin 才生效
    user_id = update.effective_user.id if update.effective_user else 0
    parts = caption.split()
    _, force_r2 = _parse_r2_flag(parts[1:], user_id, config.auth.admin_users)
    await _enqueue_zip_to_telegraph(update, context, msg, force_r2=force_r2)


async def _enqueue_zip_to_telegraph(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    zip_msg,
    *,
    force_r2: bool = False,
) -> None:
    """把 zip2tph 包成队列任务。"""
    document = zip_msg.document
    file_size = document.file_size or 0
    placeholder = await update.effective_message.reply_text(
        f"⏳ 已收到 zip ({fmt_bytes(file_size)})，准备处理..."
    )
    # zip 接收阶段会落盘 file_size 字节；解压 + 拷贝到 cache_dir 还需要一份。
    # 取 2×file_size 作为保守预估（实际峰值 ≈ 下载 + 解压同时存在的那段时间）。
    extra_required = file_size * 2 if file_size > 0 else 0
    if not await _gate_disk_space(context, placeholder, extra_required=extra_required):
        return
    user_id = update.effective_user.id if update.effective_user else 0
    work_label = f"接收 zip ({fmt_bytes(file_size)})..."

    async def _do() -> None:
        await _process_zip_to_telegraph(update, context, zip_msg, placeholder, force_r2=force_r2)

    await _enqueue(
        context,
        category="zip2tph",
        user_id=user_id,
        placeholder=placeholder,
        work_label=work_label,
        coro_factory=_do,
    )


async def _process_zip_to_telegraph(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    zip_msg,
    placeholder=None,
    *,
    force_r2: bool = False,
) -> None:
    config, _, publisher, _, _ = _ctx(context)
    document = zip_msg.document
    file_size = document.file_size or 0

    # Telegram 标准 Bot API getFile 只支持 ≤20MB 下载；本地 Bot API 可放宽。
    # 这里直接尝试，失败时给清晰的报错。
    if placeholder is None:
        placeholder = await update.effective_message.reply_text(
            f"⏳ 接收 zip ({fmt_bytes(file_size)})..."
        )
    progress = Progress(placeholder, prefix=f"📦 {document.file_name or 'archive.zip'}")
    _attach_progress_markup(progress, placeholder)

    tmpdir = Path(tempfile.mkdtemp(prefix="zip2tph_"))
    zip_path = tmpdir / "input.zip"
    try:
        # 下载并验证大小：超时但落盘字节数等于 file_size 仍视为成功。
        download_ok = False
        for attempt in range(2):
            try:
                tg_file = await context.bot.get_file(
                    document.file_id,
                    read_timeout=TG_UPLOAD_TIMEOUT, write_timeout=TG_UPLOAD_TIMEOUT,
                    connect_timeout=60, pool_timeout=60,
                )
                await progress.status("⏳ 下载 zip 中...")
                # PTB download_to_drive 不暴露分段回调；用旁路 task 周期采 zip_path 大小推 ETA
                tracker = ByteRateTracker(file_size)
                stop_watch = asyncio.Event()

                async def _watch_size() -> None:
                    while not stop_watch.is_set():
                        try:
                            cur = zip_path.stat().st_size if zip_path.exists() else 0
                        except OSError:
                            cur = 0
                        delta = cur - tracker.done
                        if delta > 0:
                            tracker.add(delta)
                        if tracker.done > 0:
                            await progress.update(tracker.format("下载 zip"))
                        try:
                            await asyncio.wait_for(stop_watch.wait(), timeout=1.0)
                        except TimeoutError:
                            pass

                watch_task = asyncio.create_task(_watch_size())
                try:
                    await tg_file.download_to_drive(
                        custom_path=str(zip_path),
                        read_timeout=TG_UPLOAD_TIMEOUT, write_timeout=TG_UPLOAD_TIMEOUT,
                        connect_timeout=60, pool_timeout=60,
                    )
                finally:
                    stop_watch.set()
                    try:
                        await watch_task
                    except Exception:
                        pass
                download_ok = True
                break
            except Exception as e:
                actual = zip_path.stat().st_size if zip_path.exists() else 0
                if file_size > 0 and actual == file_size:
                    # 落盘已完整 —— 即便抛了超时也算成功
                    logger.info(
                        f"zip2tph download '超时' but file complete "
                        f"({actual}=={file_size}); treating as success"
                    )
                    download_ok = True
                    break
                if _is_timeout_exc(e) and attempt == 0:
                    logger.warning(
                        f"zip2tph download timed out (attempt {attempt+1}/2): "
                        f"{type(e).__name__}: {e}; will retry once"
                    )
                    if zip_path.exists():
                        try:
                            zip_path.unlink()
                        except OSError:
                            pass
                    await progress.status("⏳ 下载超时，重试中...")
                    continue
                await progress.finish(
                    f"⚠️ 下载 zip 失败：{e}\n"
                    "（>20MB 必须使用本地 Bot API 且打开 telegram.local_mode=true；"
                    "Permission denied 见 README 'local Bot API 文件读权限'）"
                )
                return
        if not download_ok:
            await progress.finish("⚠️ 下载 zip 失败：连续两次超时未完成")
            return

        # 解压：放到线程池避免阻塞 event loop；同时旁路 watcher 监听输出目录推进度。
        await progress.status("⏳ 解析 zip 中（统计文件数）...")
        try:
            total_imgs = await asyncio.to_thread(_count_zip_images, zip_path)
        except zipfile.BadZipFile as e:
            await progress.finish(f"⚠️ 不是有效 zip：{e}")
            return
        if total_imgs == 0:
            await progress.finish("⚠️ zip 内没有可识别的图片")
            return

        extract_dir = tmpdir / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        await progress.status(f"⏳ 解压中 (0/{total_imgs})...")
        stop_extract = asyncio.Event()

        async def _watch_extract() -> None:
            t0 = time.monotonic()
            while not stop_extract.is_set():
                try:
                    done = sum(
                        1 for p in extract_dir.iterdir()
                        if p.suffix.lower() in _IMAGE_EXTS
                    )
                except OSError:
                    done = 0
                elapsed = time.monotonic() - t0
                eta = ""
                if done > 0 and total_imgs > done and elapsed >= 2.0:
                    rate = done / elapsed
                    if rate > 0:
                        eta = f" · ~{fmt_duration((total_imgs - done) / rate)}剩余"
                await progress.update(f"⏳ 解压中 {done}/{total_imgs}{eta}")
                try:
                    await asyncio.wait_for(stop_extract.wait(), timeout=1.0)
                except TimeoutError:
                    pass

        watch_extract_task = asyncio.create_task(_watch_extract())
        try:
            try:
                images = await asyncio.to_thread(_extract_zip_images, zip_path, extract_dir)
            except ArchiveError as e:
                await progress.finish(f"⚠️ zip 解析失败：{e}")
                return
            except zipfile.BadZipFile as e:
                await progress.finish(f"⚠️ 不是有效 zip：{e}")
                return
        finally:
            stop_extract.set()
            try:
                await watch_extract_task
            except Exception:
                pass

        if not images:
            await progress.finish("⚠️ zip 内没有可识别的图片")
            return

        # 拷贝到 cache_dir 让 Nginx 暴露。这是 R2 不可用 / R2 上传失败 / R2 被
        # 护栏跳过 时的 fallback 源——必须存在，所以这一步对所有路径都执行。
        # 双份磁盘占用按 cache_days（7 天）清掉。
        token = uuid.uuid4().hex[:10]
        cache_dir = Path(config.storage.cache_dir)
        public_dir = cache_dir / f"zip_{token}"
        public_dir.mkdir(parents=True, exist_ok=True)
        gallery_images: list[GalleryImage] = []
        ctr = ImageCounter(total=len(images), progress=progress, label="拷贝图片")
        for i, src in enumerate(images):
            ext = src.suffix.lower() or ".jpg"
            dest = public_dir / f"p{i:04d}{ext}"
            shutil.copy2(src, dest)
            rel = dest.resolve().relative_to(cache_dir.resolve())
            public_url = f"{config.publish.base_url.rstrip('/')}/{rel.as_posix()}"
            # R2 key 用 zip_{token}/pN.ext 跟 cache_dir 相对路径完全对齐——便于
            # /stats system 看占用时跟 nginx 上的 zip_* 目录对得上号
            gallery_images.append(GalleryImage(
                page_index=i,
                local_path=dest,
                public_url=public_url,
                r2_key=rel.as_posix(),
            ))
            await ctr.tick()

        # 标题：去掉扩展名
        raw_name = document.file_name or "archive.zip"
        title = re.sub(r"\.zip$", "", raw_name, flags=re.IGNORECASE).strip() or "图片包"

        await _drop_cancel_button(placeholder)
        await progress.status("⏳ 发布到 Telegra.ph...")

        # 构造 GalleryWork 走通用 publish_gallery 路径——R2 上传 + size guard +
        # 进度条 + nginx fallback 一起接管。
        work = GalleryWork(
            provider="zip2tph",
            kind="gallery",
            work_id=document.file_unique_id,
            source_url="",         # zip2tph 没有外部原作链接
            title=title,
            images=gallery_images,
            extra_vars={"page_count": len(gallery_images)},
        )
        # zip2tph 没有自定义模板段（不像 illust/novel/gallery），用一个最小 header
        header_template = (
            f"<p>共 {len(gallery_images)} 张 · 来自上传 zip：{_html_escape(raw_name)}</p>"
        )

        pub_hook = make_item_hook(progress, "发布 Telegra.ph 页面")
        try:
            pub = await publisher.publish_gallery(
                work,
                page_title_template=title,
                page_header_template=header_template,
                page_footer_template="",
                on_progress=pub_hook,
                on_status=progress.update,
                force_r2=force_r2,
            )
        except Exception as e:
            logger.exception("zip2tph publish_gallery failed")
            await progress.finish(f"⚠️ 发布失败：{e}")
            await _log_usage(
                context, update, kind=KIND_ZIP2TPH,
                ref_id=document.file_unique_id, status="failed",
            )
            return

        suffix = _r2_skipped_suffix(pub, r2_enabled=config.storage.r2.enabled)
        if suffix:
            # progress.finish 直接 edit_text 不支持 parse_mode；用 placeholder 兜底
            await placeholder.edit_text(
                pub.primary_url + suffix, parse_mode=ParseMode.HTML,
            )
        else:
            await progress.finish(pub.primary_url)
        await _log_usage(
            context, update,
            kind=KIND_ZIP2TPH, ref_id=document.file_unique_id,
            bytes_in=file_size,
        )
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def _count_zip_images(zip_path: Path) -> int:
    """快速统计 zip 内可识别的图片数（不解压）。"""
    n = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            if Path(name).suffix.lower() in _IMAGE_EXTS:
                n += 1
    return n


def _extract_zip_images(zip_path: Path, dest_dir: Path) -> list[Path]:
    """解压 zip，仅保留图片，按字典序返回路径列表。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = sorted(n for n in zf.namelist() if not n.endswith("/"))
        extracted: list[Path] = []
        for name in names:
            ext = Path(name).suffix.lower()
            if ext not in _IMAGE_EXTS:
                continue
            target = dest_dir / Path(name).name
            # 防 zip slip
            if target.parent.resolve() != dest_dir.resolve():
                continue
            with zf.open(name) as src, target.open("wb") as dst:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            extracted.append(target)
    extracted.sort(key=lambda p: p.name)
    return extracted


# ---------------------------------------------------------------------------
# /archive：直接返回压缩包
# ---------------------------------------------------------------------------
#
# 行为：
#   - eh / ex 链接：弹四模式按钮（与原 message handler 行为一致），用户选定模式
#                   后产出 zip 直接 sendDocument。同时保留缓存（沿用原下载路径）。
#   - pixiv illust / nhentai：把图片打包为临时 zip 直接 sendDocument。
#   - pixiv novel：报错（small text only，不打包意义不大）。
#
# 文件 > TG_DOCUMENT_LIMIT (50MB) 且未配置本地 Bot API 时直接报错。


async def cmd_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, registry, publisher, _, allowlist = _ctx(context)
    if not await is_authorized(update, allowlist):
        return
    await _track_user(update, context)

    text = " ".join(context.args or []) or (update.effective_message.text or "")
    refs = registry.extract_all_refs(text)
    if not refs:
        await update.message.reply_text("用法：/archive <链接>")
        return

    for ref in refs:
        await _archive_one_ref(update, context, ref)


async def _archive_one_ref(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ref: ParsedRef,
) -> None:
    config, registry, *_ = _ctx(context)

    # eh/ex：复用 _eh_offer_modes 流程，但回调走 archive 通道（按按钮后才入队）
    if ref.provider in ("e-hentai.org", "exhentai.org"):
        await _eh_offer_modes_for_archive(update, context, ref)
        return

    if ref.provider == "pixiv" and ref.kind == "novel":
        await update.message.reply_text(
            "⚠️ /archive 不支持 pixiv novel（纯文本无意义）"
        )
        return

    placeholder = await update.message.reply_text(
        f"⏳ 已收到 /archive 请求（{ref.provider} {ref.id}），准备处理..."
    )
    if not await _gate_disk_space(context, placeholder):
        return
    user_id = update.effective_user.id if update.effective_user else 0

    async def _do() -> None:
        await _archive_one_ref_run(update, context, ref, placeholder)

    await _enqueue(
        context,
        category="archive_zip",
        user_id=user_id,
        placeholder=placeholder,
        work_label=f"处理 {ref.provider} {ref.id}...",
        coro_factory=_do,
    )


async def _archive_one_ref_run(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ref: ParsedRef,
    placeholder,
) -> None:
    config, registry, *_ = _ctx(context)
    progress = Progress(placeholder, prefix=f"📦 {ref.provider} {ref.id}")
    _attach_progress_markup(progress, placeholder)
    try:
        provider = registry.find_by_name(ref.provider)
        if provider is None:
            await progress.finish(f"⚠️ Provider {ref.provider!r} 未启用")
            return

        if ref.provider == "pixiv":
            await progress.status("⏳ 拉取作品并下载图片...")
            from ...provider.pixiv import PixivProvider as _Pixiv  # noqa: F401
            pixiv = _pixiv_provider(registry)
            assert pixiv is not None
            dl_hook = make_item_hook(progress, "下载图片")
            illust = await pixiv.fetch_and_download_illust(ref.id, on_progress=dl_hook)
            local_paths = [img.original_path for img in illust.images]
            title = illust.work.title or f"pixiv-{ref.id}"
        else:
            await progress.status("⏳ 拉取作品并下载图片...")
            dl_hook = make_item_hook(progress, f"{ref.provider} 下载图片")
            gallery = await provider.fetch_and_download(ref, on_progress=dl_hook)
            local_paths = [img.local_path for img in gallery.images]
            title = gallery.title or f"{ref.provider}-{ref.id}"

        if not local_paths:
            await progress.finish("⚠️ 没有可打包的图片")
            await _log_usage(context, update, kind=KIND_ARCHIVE_CMD,
                             provider=ref.provider, ref_id=ref.id, status="failed")
            return

        await _zip_and_send(
            update, context, progress,
            files=local_paths,
            stem=_safe_zip_name(title) or _safe_zip_name(f"{ref.provider}_{ref.id}"),
            caption=f"{title}\n来源：{ref.provider} {ref.id}",
        )
        total_bytes = 0
        for p in local_paths:
            try:
                total_bytes += p.stat().st_size
            except OSError:
                pass
        await _log_usage(
            context, update, kind=KIND_ARCHIVE_CMD,
            provider=ref.provider, ref_id=ref.id,
            bytes_in=total_bytes, bytes_out=total_bytes,
        )
    except Exception as e:
        logger.exception(f"/archive {ref.provider}/{ref.id} failed")
        await progress.finish(f"⚠️ 处理失败：{e}")
        await _log_usage(context, update, kind=KIND_ARCHIVE_CMD,
                         provider=ref.provider, ref_id=ref.id, status="failed")


async def _eh_offer_modes_for_archive(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ref: ParsedRef
) -> None:
    """eh/ex /archive：弹四模式按钮，回调走 archive 分支。"""
    placeholder = await update.message.reply_text("📖 解析中...")
    orig_msg = update.effective_message
    await _eh_offer_archive_modes_on_placeholder(
        context, ref, placeholder=placeholder, user_id=update.effective_user.id,
        orig_chat_id=orig_msg.chat.id if orig_msg else None,
        orig_msg_id=orig_msg.message_id if orig_msg else None,
    )


async def _eh_offer_archive_modes_on_placeholder(
    context: ContextTypes.DEFAULT_TYPE,
    ref: ParsedRef,
    *,
    placeholder,
    user_id: int,
    orig_chat_id: int | None = None,
    orig_msg_id: int | None = None,
) -> None:
    """复用版本：接现成 placeholder，供 /ehsearch [归档下载] callback 调用。

    与 _eh_offer_modes_for_archive 行为等价，仅入口不同（callback 没有 update.message）。
    搜索流没有用户原始消息，orig_chat_id/orig_msg_id 保持 None。
    """
    config, registry, *_ = _ctx(context)
    provider = _eh_provider(registry, ref.provider)
    if provider is None:
        return

    try:
        gallery = await provider.fetch_work(ref)
    except EHGalleryUnavailable as e:
        if ref.provider == "e-hentai.org":
            fallback = await _try_fallback_to_exhentai(context, ref, placeholder, str(e))
            if fallback is None:
                return
            ref, gallery, provider = fallback
        else:
            await placeholder.edit_text(f"⚠️ 解析失败：{e}")
            return
    except EHError as e:
        await placeholder.edit_text(f"⚠️ 解析失败：{e}")
        return
    except Exception as e:
        logger.exception(f"{ref.provider} fetch_work failed for {ref.id}")
        await placeholder.edit_text(f"⚠️ 解析失败：{e}")
        return

    _gc_pending()
    token = uuid.uuid4().hex[:10]
    _PENDING[token] = _Pending(
        ref=ref,
        chat_id=placeholder.chat.id,
        msg_id=placeholder.message_id,
        user_id=user_id,
        created_at=time.time(),
        orig_chat_id=orig_chat_id,
        orig_msg_id=orig_msg_id,
    )

    # title 转义在 _render_eh_detail_card 内部统一走 _html_escape
    text = _render_eh_detail_card(
        title=gallery.title,
        host=ref.provider,
        category=gallery.category,
        pages=gallery.page_count,
        tags=gallery.tags,
        ehtagdb=_get_ehtagdb(context),
        footer_prompt="选择下载模式（产出压缩包）：",
    )
    # 用 eha: 前缀区分回调
    keyboard = _make_eh_keyboard(token, prefix="eha")
    await placeholder.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

    # 异步拿 archive 两档大小，回填按钮 label。失败/超时静默跳过。
    _schedule_eh_size_prefetch(context, placeholder, token, ref, prefix="eha")


async def _eh_archive_with_mode(
    context: ContextTypes.DEFAULT_TYPE,
    ref: ParsedRef,
    *,
    mode: EHMode,
    placeholder,
    user_id: int,
    chat_id: int | None = None,
) -> None:
    """eh/ex 选定模式后产出 zip：
    - archive_* 模式：直接走 archiver.php 拿 zip 直链下载并送回
    - page_* 模式：走 fetch_and_download_with_mode 下载图片后打 zip
    """
    config, registry, *_ = _ctx(context)
    provider = _eh_provider(registry, ref.provider)
    if provider is None:
        return

    # 用于结束时统一打 usage_log（不论成功/失败/取消都至少有一条）
    usage_logged = {"done": False}
    final_kind = KIND_EH_ARCHIVE if mode.is_archive else KIND_EH_PAGE
    eff_chat_id = chat_id if chat_id is not None else placeholder.chat.id

    async def _emit_usage(*, status: str, gp: int = 0, bin_: int = 0, bout: int = 0) -> None:
        if usage_logged["done"]:
            return
        usage_logged["done"] = True
        store = _usage_store(context)
        if store is None:
            return
        try:
            await store.log(
                user_id=user_id,
                chat_id=eff_chat_id,
                kind=final_kind,
                provider=ref.provider,
                ref_id=ref.id,
                gp_cost=gp,
                bytes_in=bin_,
                bytes_out=bout,
                status=status,
            )
        except Exception:
            pass
    if provider is None:
        return

    progress = Progress(placeholder, prefix=f"📦 {ref.provider} {ref.id} · {mode.label_zh}")
    _attach_progress_markup(progress, placeholder)
    gid, token = ref.id.split("/", 1)
    host = provider.HOST

    try:
        if mode.is_archive:
            # 直接调底层 _archive 流水线，把 zip 整个落盘后 sendDocument
            await progress.status("⏳ 解析画廊页与 archive 入口...")
            gallery_meta = await provider.fetch_work(ref)
            album_url = f"https://{host}/g/{gid}/{token}"

            tmpdir = Path(tempfile.mkdtemp(prefix="eh_archive_"))
            zip_stem = _safe_zip_name(f"{gallery_meta.title}_{mode.value}") \
                or f"{host}_{gid}_{token}_{mode.value}"
            zip_path = tmpdir / f"{zip_stem}.zip"
            try:
                cookies = provider._cookies_for(mode)
                import httpx
                async with httpx.AsyncClient(
                    headers=EH_BASE_HEADERS,
                    cookies=cookies,
                    timeout=provider._shared_cfg.timeout,
                    http2=True,
                    follow_redirects=True,
                ) as client:
                    archiver_token = await fetch_archiver_token(client, album_url)
                    await progress.status("⏳ 申请 archive 链接...")
                    zip_url, estimated_bytes, gp_cost = await request_archive(
                        client, host, gid, token, archiver_token, mode,
                    )
                    if gp_cost > 0:
                        logger.info(
                            f"[{ref.provider}/{ref.id}] archive will cost {gp_cost} GP "
                            f"(mode={mode.value})"
                        )
                    # eh/ex 给 bot 派的链接经常是 H@H 节点（xxxx.hath.network/archive/...）。
                    # archive 链接的 path 在两个 host 下通用，主站本地路径作为 fallback。
                    # 优先走 hath.network（带宽更好且是 eh 的设计意图），失败再用主站。
                    candidate_urls = [zip_url]
                    if "hath.network/archive/" in zip_url:
                        from urllib.parse import urlparse
                        parsed = urlparse(zip_url)
                        local_url = f"https://{host}{parsed.path}"
                        if parsed.query:
                            local_url += "?" + parsed.query
                        candidate_urls = [zip_url, local_url]
                        logger.info(
                            f"[{ref.provider}/{ref.id}] zip link is hath.network "
                            f"({zip_url[:80]}...), main host fallback: {local_url[:80]}..."
                        )
                    # 动态超时：用 _archive.py 共享 helper（5min + 5s/MB，封顶 1h，
                    # config 的 archive_timeout 作为下限）。解析不到 estimated 时退回配置值。
                    dyn_timeout = compute_archive_timeout(provider.archive_timeout, estimated_bytes)
                    if estimated_bytes > 0:
                        logger.info(
                            f"[{ref.provider}/{ref.id}] estimated archive size "
                            f"{fmt_bytes(estimated_bytes)}; using dynamic timeout {dyn_timeout}s"
                        )
                    await progress.status(
                        f"⏳ 下载 zip（预估 {fmt_bytes(estimated_bytes) if estimated_bytes else '?'}，"
                        f"超时 {dyn_timeout}s）..."
                    )
                    last_err: Exception | None = None
                    download_done = False
                    for cand_idx, cand_url in enumerate(candidate_urls):
                        try:
                            if cand_idx > 0:
                                logger.warning(
                                    f"[{ref.provider}/{ref.id}] candidate {cand_idx} URL "
                                    f"failed ({last_err}); trying next: {cand_url[:80]}..."
                                )
                                await progress.status(
                                    f"⏳ 切换到备用下载链接 ({cand_idx + 1}/{len(candidate_urls)})..."
                                )
                                # 旧 .part 清掉
                                tmp = zip_path.with_suffix(zip_path.suffix + ".part")
                                if tmp.exists():
                                    try:
                                        tmp.unlink()
                                    except OSError:
                                        pass
                            await download_archive_with_timeout(
                                client, cand_url, zip_path, dyn_timeout,
                                on_status=progress.update,
                            )
                            download_done = True
                            break
                        except ArchiveLockedError:
                            # session 已锁，所有 candidate 共用同一 session、refresh
                            # 也救不了 —— 立即向外抛，由外层提示用户重新提交。
                            raise
                        except ArchiveError as e:
                            last_err = e
                            continue

                    if not download_done:
                        # candidate_urls 都失败，尝试 refresh 一次（不重新 POST、不消耗配额）
                        msg = str(last_err) if last_err else ""
                        if last_err and ("not ready" in msg.lower() or "HTTP 404" in msg):
                            logger.warning(
                                f"[{ref.provider}/{ref.id}] all candidates failed ({msg}); "
                                "refreshing download link and retrying once"
                            )
                            await progress.status("⏳ 链接失效，刷新中...")
                            new_url = await refresh_download_link(client, host, gid, token)
                            if new_url and new_url not in candidate_urls:
                                logger.info(
                                    f"[{ref.provider}/{ref.id}] refreshed link: {new_url[:160]}"
                                )
                                await progress.status("⏳ 用新链接重新下载 zip...")
                                tmp = zip_path.with_suffix(zip_path.suffix + ".part")
                                if tmp.exists():
                                    try:
                                        tmp.unlink()
                                    except OSError:
                                        pass
                                await download_archive_with_timeout(
                                    client, new_url, zip_path, dyn_timeout,
                                    on_status=progress.update,
                                )
                            else:
                                raise last_err  # type: ignore[misc]
                        else:
                            assert last_err is not None
                            raise last_err
                await _send_zip_file(
                    context, placeholder.chat.id, zip_path, progress,
                    caption=f"{gallery_meta.title}\n来源：{ref.provider} {ref.id}\n模式：{mode.label_zh}",
                    reply_to=placeholder.message_id,
                )
                # 成功：记录 GP + 实际 zip 大小
                try:
                    actual_size = zip_path.stat().st_size
                except OSError:
                    actual_size = estimated_bytes
                await _emit_usage(status="ok", gp=gp_cost, bin_=actual_size, bout=actual_size)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            # page_* 模式：先把图片下完，再打 zip
            await progress.status("⏳ 下载图片中...")
            dl_hook = make_item_hook(progress, f"{ref.provider} 下载图片")
            gallery = await provider.fetch_and_download_with_mode(
                ref, mode, on_progress=dl_hook,
            )
            local_paths = [img.local_path for img in gallery.images]
            if not local_paths:
                await progress.finish("⚠️ 没有可打包的图片")
                await _emit_usage(status="failed")
                return
            await _zip_and_send_to_chat(
                context, placeholder.chat.id, progress,
                files=local_paths,
                stem=_safe_zip_name(f"{gallery.title}_{mode.value}")
                    or f"{ref.provider}_{gid}_{token}_{mode.value}",
                caption=f"{gallery.title}\n来源：{ref.provider} {ref.id}\n模式：{mode.label_zh}",
                reply_to=placeholder.message_id,
            )
            total_bytes = 0
            for p in local_paths:
                try:
                    total_bytes += p.stat().st_size
                except OSError:
                    pass
            await _emit_usage(status="ok", bin_=total_bytes, bout=total_bytes)
    except ArchiveLockedError:
        # session 被锁：尝试调 invalidate_sessions 让用户下次重新提交能拿干净链接。
        # 不自动重试本次任务 —— session 已废，重试同样会失败。
        try:
            import httpx as _httpx

            from ...provider.ehentai._archive import invalidate_archive_session
            async with _httpx.AsyncClient(
                headers=EH_BASE_HEADERS,
                cookies=provider._cookies_for(mode),
                timeout=provider._shared_cfg.timeout,
                follow_redirects=True,
            ) as _client:
                await invalidate_archive_session(_client, host, gid, token)
        except Exception as inv_e:
            logger.warning(f"invalidate_archive_session failed: {inv_e}")
        await progress.finish(
            "⚠️ archive session 已被锁定（多 IP 滥用风控）。\n"
            "已自动取消旧的下载链接，请稍后重新提交本画廊以获取新链接。"
        )
        await _emit_usage(status="failed")
    except ArchiveError as e:
        await progress.finish(f"⚠️ archive 失败：{e}")
        await _emit_usage(status="failed")
    except EHError as e:
        await progress.finish(f"⚠️ {e}")
        await _emit_usage(status="failed")
    except Exception as e:
        logger.exception(f"/archive eh {ref.id} mode={mode} failed")
        await progress.finish(f"⚠️ 处理失败：{e}")
        await _emit_usage(status="failed")


def _safe_zip_name(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", s)
    s = s.strip().strip(".")
    if len(s) > 120:
        s = s[:120]
    return s or "archive"


async def _zip_and_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    progress: Progress,
    *,
    files: list[Path],
    stem: str,
    caption: str,
) -> None:
    chat_id = update.effective_chat.id
    reply_to = update.effective_message.message_id
    await _zip_and_send_to_chat(
        context, chat_id, progress,
        files=files, stem=stem, caption=caption, reply_to=reply_to,
    )


async def _zip_and_send_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    progress: Progress,
    *,
    files: list[Path],
    stem: str,
    caption: str,
    reply_to: int | None,
) -> None:
    """把 files 打成临时 zip 并发回给 chat。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="archive_zip_"))
    zip_path = tmpdir / f"{stem}.zip"
    try:
        await progress.status(f"⏳ 打包 {len(files)} 张图片...")
        ctr = ImageCounter(total=len(files), progress=progress, label="打包")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for src in files:
                arcname = src.name
                zf.write(src, arcname=arcname)
                await ctr.tick()
        await _send_zip_file(
            context, chat_id, zip_path, progress,
            caption=caption, reply_to=reply_to,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _send_zip_file(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    zip_path: Path,
    progress: Progress,
    *,
    caption: str,
    reply_to: int | None,
) -> None:
    size = zip_path.stat().st_size
    config: Config = context.bot_data["config"]
    using_local = bool(config.telegram.base_url) and config.telegram.local_mode
    limit = LOCAL_BOT_API_DOCUMENT_LIMIT if using_local else TG_DOCUMENT_LIMIT
    if size > limit:
        await progress.finish(
            f"⚠️ 压缩包 {fmt_bytes(size)} 超过 Bot 上传上限 {fmt_bytes(limit)}"
            + ("（已配置本地 Bot API + local_mode）" if using_local
               else "（未启用本地 Bot API local_mode；上限 50MB）")
        )
        return

    await progress.status(f"⏳ 上传 zip ({fmt_bytes(size)})...")
    # 进入上传阶段：按你的要求，上传过程不可取消（PTB 把 fd 交给 httpx 后 cancel
    # 也无法让 telegram-bot-api 停止往 TG 主网传输）。直接把按钮去掉。
    await _drop_cancel_button(progress._msg, progress)
    logger.info(f"send_document start: {zip_path.name} ({fmt_bytes(size)}, {size}B)")
    # 上传重试：RetryAfter（TG 限频）按 retry_after 秒等候后再试；最多 3 次。
    upload_attempts = 0
    while True:
        upload_attempts += 1
        # 上传心跳：PTB send_document 不暴露上传进度，开旁路 task 每 5s 推一次"已上传中 Ns"
        upload_t0 = time.monotonic()
        stop_heartbeat = asyncio.Event()

        async def _heartbeat() -> None:
            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=5.0)
                    return
                except TimeoutError:
                    pass
                elapsed = int(time.monotonic() - upload_t0)
                await progress.update(
                    f"⏳ 上传 zip ({fmt_bytes(size)})... 已 {fmt_duration(elapsed)}（本地 Bot API → TG 主网）"
                )

        hb_task = asyncio.create_task(_heartbeat())
        try:
            with open(zip_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=zip_path.name,
                    caption=caption[:1024] if caption else None,
                    reply_to_message_id=reply_to,
                    # 大文件：HTTP 层不应该在传输中超时（数据本身在本机走 local_mode）。
                    # 用极大 read_timeout 让 telegram-bot-api 自然完成传输到 TG 主网。
                    read_timeout=TG_UPLOAD_TIMEOUT,
                    write_timeout=TG_UPLOAD_TIMEOUT,
                    connect_timeout=60,
                    pool_timeout=60,
                )
            elapsed = time.monotonic() - upload_t0
            logger.info(
                f"send_document done: {zip_path.name} in {elapsed:.1f}s "
                f"(attempt {upload_attempts})"
            )
            break
        except TGRetryAfter as e:
            # TG 限频：等 retry_after 秒后重试。e.retry_after 是 PTB 提供的属性。
            wait_s = int(getattr(e, "retry_after", 30)) + 1
            if upload_attempts >= 3:
                await progress.finish(
                    f"⚠️ 上传 zip 被 TG 限频（已重试 {upload_attempts} 次）：{e}\n"
                    "请稍后手动重发。"
                )
                return
            logger.warning(
                f"send_document hit RetryAfter (attempt {upload_attempts}): "
                f"waiting {wait_s}s for {zip_path.name}"
            )
            await progress.status(
                f"⏳ TG 限频中，{wait_s}s 后重试 ({upload_attempts}/3)..."
            )
            try:
                await asyncio.sleep(wait_s)
            except asyncio.CancelledError:
                raise
            continue
        except Exception as e:
            if _is_timeout_exc(e):
                # 大文件常见：HTTP 超时但 telegram-bot-api 仍在后台完成上传到 TG 主网。
                logger.warning(
                    f"send_document timed out for {zip_path.name} "
                    f"({type(e).__name__}: {e}); upload may still complete in background"
                )
                await progress.finish(
                    f"⏳ 上传超时（{fmt_bytes(size)}）。本地 Bot API 仍在向 TG 主网传输，"
                    "请稍候 1-2 分钟查看是否已收到文件；如未收到再重试。"
                )
                return
            logger.exception("send_document failed")
            await progress.finish(f"⚠️ 发送 zip 失败：{e}")
            return
        finally:
            stop_heartbeat.set()
            try:
                await hb_task
            except Exception:
                pass

    try:
        await progress.finish(f"✅ 已发送 {zip_path.name} ({fmt_bytes(size)})")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 扩展 handle_callback：处理 eha: 前缀（/archive 的 eh/ex 模式选择）
# ---------------------------------------------------------------------------


async def handle_callback_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """eha: 前缀的回调（来自 /archive eh|ex 弹的按钮）。"""
    query = update.callback_query
    if query is None or not query.data:
        return
    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] != "eha":
        return  # 不是我们的回调

    _, token, mode_str = parts
    pending = _PENDING.get(token)
    if pending is None:
        await query.answer("⚠️ 选项已过期，请重新发送 /archive", show_alert=True)
        return
    if query.from_user.id != pending.user_id:
        await query.answer("⚠️ 这个选择来自其他用户", show_alert=True)
        return
    if mode_str == "cancel":
        _PENDING.pop(token, None)
        await query.answer("已取消")
        try:
            await query.edit_message_text("已取消")
        except Exception:
            pass
        _schedule_delete_after_cancel(context, query.message)
        return
    try:
        mode = EHMode(mode_str)
    except ValueError:
        await query.answer("⚠️ 未知模式", show_alert=True)
        return

    _PENDING.pop(token, None)
    await query.answer(f"使用 {mode.label_zh}")

    placeholder = query.message
    try:
        await placeholder.edit_text(
            f"⏳ 已收到（{mode.label_zh}），准备处理..."
        )
    except Exception:
        pass

    if not await _gate_disk_space(context, placeholder):
        return

    async def _do() -> None:
        await _eh_archive_with_mode(
            context, pending.ref, mode=mode, placeholder=placeholder,
            user_id=pending.user_id, chat_id=pending.chat_id,
        )

    await _enqueue(
        context,
        category="archive_zip",
        user_id=pending.user_id,
        placeholder=placeholder,
        work_label=f"处理 {pending.ref.provider} {pending.ref.id}（{mode.label_zh}）...",
        coro_factory=_do,
    )


# ---------------------------------------------------------------------------
# /stats：admin 用量统计
# ---------------------------------------------------------------------------
#
# 用法：
#   /stats                    总览（24h，所有用户聚合 + 前 10 用户排行）
#   /stats user @username     指定用户（24h）
#   /stats user 12345         指定 user_id（24h）
#   /stats chat -100123       指定群组（24h）
#   /stats user @x 7d         指定时间窗口（默认 24h，支持 1h/24h/7d/30d）
#   /stats system             缓存大小 + 磁盘剩余


def _parse_window(token: str) -> int | None:
    """解析 '24h' / '7d' / '30d' / '1h' → 秒数；解析失败返回 None。"""
    token = token.strip().lower()
    if not token:
        return None
    if token.endswith("h"):
        try:
            return int(token[:-1]) * 3600
        except ValueError:
            return None
    if token.endswith("d"):
        try:
            return int(token[:-1]) * 86400
        except ValueError:
            return None
    return None


def _fmt_window(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds}s"


def _dir_size(path: Path) -> int:
    """递归计算目录占用字节。任何 OSError 都跳过。"""
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _disk_usage(path: Path) -> tuple[int, int, int]:
    """返回挂载点上的 (total, used, free) 字节。失败返回 (0,0,0)。"""
    try:
        usage = shutil.disk_usage(str(path))
        return usage.total, usage.used, usage.free
    except OSError:
        return 0, 0, 0


async def _resolve_chat_arg(store, ident: str) -> tuple[int | None, str | None]:
    """把用户传给 /stats chat <ident> 的字符串解析成 chat_id。

    接受形式：
      - 负数 `-1001838275879` / `-12345`：原样
      - 正数 `1838275879`：当 supergroup 短 id，自动补 `-100`
        （Telegram 内部 chat_id 不会是正数，正数等价于"短形式"）
      - `@username`：在 chats 表里查
    返回 (chat_id, 错误信息)。chat_id 为 None 时 err 给出原因。
    """
    ident = ident.strip()
    if not ident:
        return None, "chat 参数为空"
    if ident.startswith("@"):
        cid = await store.get_chat_by_username(ident)
        if cid is None:
            return None, f"找不到 @{ident.lstrip('@')}（仅能查曾经触发过 bot 的群/频道）"
        return cid, None
    # 数字
    if ident.lstrip("-").isdigit():
        n = int(ident)
        if n >= 0:
            # 短形式，按 supergroup/channel 处理：补 -100 前缀
            return int(f"-100{n}"), None
        return n, None
    return None, f"无法识别 {ident!r}：用 -1001838275879 / 1838275879 / @username 任一形式"


# ---------------------------------------------------------------------------
# /ehsearch —— e-hentai / exhentai 关键词搜索
# ---------------------------------------------------------------------------
#
# UX 三层：
#   1. /ehsearch <keyword> → 默认 10 条结果 + [展开] / [下一页]
#   2. 点 [打开] → 走 PAGE_SAMPLE 模式发 Telegra.ph，完成消息挂 [归档下载]
#   3. 点 [归档下载] → 复用 /archive 的 4 模式按钮
#
# 站点选择：ex 优先（账号 cookie 可用时），EHSearchAuthError 时回退到 e-hentai。
# 状态通过 _SEARCH_STATES dict 维护（TTL 与 _PENDING 共用 PENDING_TTL）。


_EHSEARCH_DEFAULT_VISIBLE = 10            # 默认展示前 10 条
_EHSEARCH_MAX_VISIBLE = 25                # eh 一页就 25 条；展开时全显
_EHSEARCH_TITLE_BTN_MAX = 30              # [打开] 按钮里标题截断长度
_EHSEARCH_TAG_PRIORITY = ("parody:", "artist:", "character:")
_EHSEARCH_MAX_TAGS_PER_ITEM = 3           # 每条结果消息文本里展示的 tag 数上限
_EHSEARCH_MAX_TAGS_DETAIL = 6             # 详情卡上展示的 tag 数上限

# 详情卡 blockquote 分组顺序（eh 详情页的 #taglist 排版顺序）
_EH_NAMESPACE_ORDER = (
    "language", "parody", "character", "group", "artist", "cosplayer",
    "female", "male", "mixed", "other", "reclass", "temp",
)
# ehtagdb 没加载完时的 namespace 中文兜底
_EH_NAMESPACE_ZH_FALLBACK = {
    "language": "语言",
    "parody": "原作",
    "character": "角色",
    "group": "社团",
    "artist": "作者",
    "cosplayer": "扮演者",
    "female": "♀",
    "male": "♂",
    "mixed": "混合",
    "other": "其它",
    "reclass": "重分类",
    "temp": "临时",
}
# 详情页头部 category（"Doujinshi" / "Manga" / ...）→ 中文，硬编码够稳，无需翻译库
_EH_CATEGORY_ZH = {
    "Doujinshi": "同人志",
    "Manga": "漫画",
    "Artist CG": "画师 CG",
    "Game CG": "游戏 CG",
    "Western": "西方",
    "Non-H": "非 H",
    "Image Set": "图集",
    "Cosplay": "Cosplay",
    "Asian Porn": "亚洲",
    "Misc": "杂项",
}
_EH_NS_MAX_VALUES_IN_BLOCKQUOTE = 12       # 每个 namespace 最多展示 12 个 value（防 TG 4096 上限）


def _get_ehtagdb(context: ContextTypes.DEFAULT_TYPE | None):
    """从 bot_data 拿 EHTagDB 实例；不存在/未配置时返回 None。"""
    if context is None:
        return None
    return context.bot_data.get("ehtagdb")


def _eh_translate_ns(ns: str, ehtagdb) -> str:
    if ehtagdb is not None and getattr(ehtagdb, "loaded", False):
        zh = ehtagdb.translate_namespace(ns)
        if zh and zh != ns:
            return zh
    return _EH_NAMESPACE_ZH_FALLBACK.get(ns, ns)


def _eh_translate_value(ns: str, value: str, ehtagdb) -> str:
    if ehtagdb is not None and getattr(ehtagdb, "loaded", False):
        return ehtagdb.translate(ns, value)
    return value


def _eh_translate_category(category: str) -> str:
    if not category:
        return "—"
    return _EH_CATEGORY_ZH.get(category, category)


def _group_tags(tags: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for t in tags:
        if ":" not in t:
            continue
        ns, v = t.split(":", 1)
        grouped.setdefault(ns, []).append(v)
    return grouped


def _render_eh_detail_card(
    *,
    title: str,
    host: str,
    category: str,
    pages: int,
    tags: list[str],
    ehtagdb,
    footer_prompt: str = "",
) -> str:
    """共享 eh/ex 详情卡 HTML：链接流（粘 eh 链接 / /archive）+ 搜索流 L2 / L3 共用。

    布局：
        📖 <b>标题</b>
        🌐 host · <i>类型: X · 语言: Y · N 页</i>

        <blockquote expandable>
        <b>语言</b>: chinese、translated
        <b>原作</b>: VOICEROID
        ...
        </blockquote>

        {footer_prompt}（"选择下载模式：" 之类）

    tags 空时不渲染 blockquote。footer_prompt 空时不附底部。
    """
    lang = _extract_language(tags)
    lang_zh = _eh_translate_value("language", lang, ehtagdb)
    cat_zh = _eh_translate_category(category)

    lines = [
        f"📖 <b>{_html_escape(title)}</b>",
        f"🌐 {host} · <i>类型: {_html_escape(cat_zh)} · "
        f"语言: {_html_escape(lang_zh)} · {pages} 页</i>",
    ]

    grouped = _group_tags(tags)
    bq_lines: list[str] = []
    seen: set[str] = set()

    def _emit(ns: str) -> None:
        values = grouped.get(ns)
        if not values:
            return
        seen.add(ns)
        truncated = values[:_EH_NS_MAX_VALUES_IN_BLOCKQUOTE]
        more = len(values) - len(truncated)
        ns_label = _eh_translate_ns(ns, ehtagdb)
        translated = [_eh_translate_value(ns, v, ehtagdb) for v in truncated]
        body = "、".join(_html_escape(v) for v in translated)
        if more > 0:
            body += f" <i>(+{more})</i>"
        bq_lines.append(f"<b>{_html_escape(ns_label)}</b>: {body}")

    for ns in _EH_NAMESPACE_ORDER:
        _emit(ns)
    # 兜底未在 ORDER 里的 namespace
    for ns in grouped:
        if ns not in seen:
            _emit(ns)

    if bq_lines:
        lines.append("")
        lines.append("<blockquote expandable>" + "\n".join(bq_lines) + "</blockquote>")

    if footer_prompt:
        lines.append("")
        lines.append(footer_prompt)

    text = "\n".join(lines)
    # TG 消息上限 4096 char。极端长 tag 列表兜底截断。
    if len(text) > 4000:
        text = text[:3950] + "\n<i>...(标签过多，已截断)</i>"
    return text


def _eh_search_providers(registry: ProviderRegistry) -> tuple[EHFamilyBase | None, EHFamilyBase | None]:
    """返回 (ex_provider, eh_provider)；任一不存在时给 None。"""
    ex = registry.find_by_name("exhentai.org")
    eh = registry.find_by_name("e-hentai.org")
    return (
        ex if isinstance(ex, EHFamilyBase) else None,
        eh if isinstance(eh, EHFamilyBase) else None,
    )


async def _ehsearch_dispatch(
    registry: ProviderRegistry, keyword: str, *,
    next_param: int | None = None, prev_param: int | None = None,
    force_host: str | None = None,
) -> SearchResultPage:
    """ex 优先 + 退 eh。force_host 指定时跳过 fallback（翻页时用，保证同站连续）。"""
    ex, eh = _eh_search_providers(registry)
    if force_host == "e-hentai.org" and eh:
        return await search_eh(eh, keyword, next_param=next_param, prev_param=prev_param)
    if force_host == "exhentai.org" and ex:
        return await search_eh(ex, keyword, next_param=next_param, prev_param=prev_param)
    # 默认：先 ex，cookie 失效回退 eh
    if ex is not None:
        try:
            return await search_eh(ex, keyword, next_param=next_param, prev_param=prev_param)
        except EHSearchAuthError as e:
            logger.info(f"ehsearch ex auth failed, falling back to eh: {e}")
    if eh is None:
        raise EHSearchError("eh/ex provider 未注册")
    return await search_eh(eh, keyword, next_param=next_param, prev_param=prev_param)


def _eh_short_host(host: str) -> str:
    return "x" if host == "exhentai.org" else "e"


def _eh_host_from_short(short: str) -> str:
    return "exhentai.org" if short == "x" else "e-hentai.org"


def _select_display_tags(tags: list[str], *, max_n: int = _EHSEARCH_MAX_TAGS_PER_ITEM) -> list[str]:
    """从一堆 tags 里挑展示用的少量代表 tag。

    优先 parody / artist / character；不足 max_n 个时用顺序补。
    输出去掉 namespace 前缀，纯 value。**调用方应预先剔除 language: 项**（语言
    在 meta_bits 单独显示）。
    """
    return [v for _ns, v in _pick_display_tag_pairs(tags, max_n=max_n)]


def _pick_display_tag_pairs(
    tags: list[str], *, max_n: int = _EHSEARCH_MAX_TAGS_PER_ITEM,
) -> list[tuple[str, str]]:
    """同 _select_display_tags，但返回 (namespace, value) 对，便于按 ns 查翻译。"""
    chosen: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(t: str) -> None:
        if t in seen or ":" not in t:
            return
        ns, value = t.split(":", 1)
        chosen.append((ns, value))
        seen.add(t)

    for prefix in _EHSEARCH_TAG_PRIORITY:
        for t in tags:
            if len(chosen) >= max_n:
                break
            if t.startswith(prefix):
                _add(t)
        if len(chosen) >= max_n:
            break
    if len(chosen) < max_n:
        for t in tags:
            if len(chosen) >= max_n:
                break
            _add(t)
    return chosen


def _extract_language(tags: list[str]) -> str:
    """eh 没标 language tag 的画廊默认是日文（站点本质就是日本同人）。"""
    for t in tags:
        if t.startswith("language:"):
            value = t.split(":", 1)[1]
            # 跳过 "translated"、"rewrite" 这种伪 language —— 它们是修饰符
            if value in ("translated", "rewrite", "speechless"):
                continue
            return value
    return "japanese"


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ellipsize(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _render_search_message(
    seid: str, state: _SearchState, ehtagdb=None,
) -> tuple[str, InlineKeyboardMarkup]:
    """渲染搜索结果消息文本 + 按钮键盘。

    返回 (HTML 文本, 键盘)。文本控制在 TG 4096 字符内（25 条 × ~150 字 ≤ 4000）。
    传 ehtagdb 时，category / language / 显示 tag 都尝试翻译为中文。
    """
    visible = (
        _EHSEARCH_MAX_VISIBLE if state.expanded else _EHSEARCH_DEFAULT_VISIBLE
    )
    items = state.page.items[:visible]
    host_label = state.host

    head = (
        f"🔍 <b>{_html_escape(state.keyword)}</b> · {host_label}\n"
        f"共 {state.page.total_count:,} 条，本页显示 {len(items)} / {len(state.page.items)}\n"
    )
    lines = [head]
    for i, it in enumerate(items, 1):
        title_safe = _html_escape(_ellipsize(it.title, 90))
        lang_raw = _extract_language(it.tags)
        lang_zh = _eh_translate_value("language", lang_raw, ehtagdb)
        cat_zh = _eh_translate_category(it.category)
        other_tags = [t for t in it.tags if not t.startswith("language:")]
        tag_values = _select_display_tags(other_tags)
        # _select_display_tags 已经剥了 namespace；要翻译得有原始 (ns, value)。
        # 简化：从 other_tags 顺序挑前 N 个对应的，原 ns 重建 → 翻译 value。
        tag_pairs = _pick_display_tag_pairs(other_tags, max_n=_EHSEARCH_MAX_TAGS_PER_ITEM)
        tag_translated = [_eh_translate_value(ns, v, ehtagdb) for ns, v in tag_pairs]
        tags_disp = " · ".join(tag_translated)
        # 顺序：类型 · 语言 · 页数 · 优选 tag（去掉 language）
        meta_bits = [cat_zh, lang_zh, f"{it.pages} 页"]
        if tags_disp:
            meta_bits.append(_html_escape(tags_disp))
        lines.append(f"\n<b>{i}.</b> {title_safe}\n   <i>{' · '.join(meta_bits)}</i>")
    text = "".join(lines)

    rows: list[list[InlineKeyboardButton]] = []
    for idx, it in enumerate(items):
        label = f"打开 #{idx + 1} · {_ellipsize(it.title, _EHSEARCH_TITLE_BTN_MAX)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"ehs_open:{seid}:{idx}")])

    nav: list[InlineKeyboardButton] = []
    if state.page.prev_url:
        nav.append(InlineKeyboardButton("◀ 上一页", callback_data=f"ehs_prev:{seid}"))
    if not state.expanded and len(state.page.items) > _EHSEARCH_DEFAULT_VISIBLE:
        nav.append(InlineKeyboardButton("展开全部", callback_data=f"ehs_more:{seid}"))
    if state.page.next_url:
        nav.append(InlineKeyboardButton("下一页 ▶", callback_data=f"ehs_next:{seid}"))
    if nav:
        rows.append(nav)

    return text, InlineKeyboardMarkup(rows)


async def cmd_ehsearch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """关键词搜索 eh/ex 画廊。

    admin 可加 `--r2` flag，让本次搜索后续点 [打开] 的发布强制走 R2（绕过
    max_upload_size_gb 护栏）。
    """
    config, registry, _, _, allowlist = _ctx(context)
    if not await is_authorized(update, allowlist):
        return
    await _track_user(update, context)

    user_id = update.effective_user.id if update.effective_user else 0
    raw_args = list(context.args or [])
    args, force_r2 = _parse_r2_flag(raw_args, user_id, config.auth.admin_users)
    keyword = " ".join(args).strip()
    if not keyword:
        await update.message.reply_text(
            "用法：/ehsearch <关键词> [--r2]\n"
            "示例：/ehsearch language:chinese translated"
        )
        return

    placeholder = await update.message.reply_text(f"🔍 搜索 “{keyword}” ...")

    try:
        page = await _ehsearch_dispatch(registry, keyword)
    except EHSearchAuthError as e:
        await placeholder.edit_text(f"⚠️ 搜索失败（认证）：{e}")
        return
    except EHSearchBlockedError as e:
        await placeholder.edit_text(f"⚠️ {e}\n稍后再试。")
        return
    except EHSearchError as e:
        await placeholder.edit_text(f"⚠️ 搜索失败：{e}")
        return
    except Exception as e:
        logger.exception(f"/ehsearch {keyword!r} unexpected error")
        await placeholder.edit_text(f"⚠️ 搜索失败：{e}")
        return

    if not page.items:
        await placeholder.edit_text(f"🔍 “{keyword}” 未找到结果")
        return

    _gc_pending()
    seid = uuid.uuid4().hex[:10]
    state = _SearchState(
        host=page.host,
        keyword=keyword,
        page=page,
        chat_id=placeholder.chat.id,
        msg_id=placeholder.message_id,
        user_id=update.effective_user.id,
        expanded=False,
        created_at=time.time(),
        force_r2=force_r2,
    )
    _SEARCH_STATES[seid] = state

    text, kb = _render_search_message(seid, state, _get_ehtagdb(context))
    await placeholder.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
        disable_web_page_preview=True,
    )

    # 计入 stats（/stats 会自动出现一行 "eh/ex 搜索"）
    await _log_usage(
        context, update, kind=KIND_EH_SEARCH,
        provider=page.host, ref_id=keyword[:64],
    )


def _ehs_get_state(seid: str) -> _SearchState | None:
    """快速查 state；调用方负责 user_id 校验与已过期提示。"""
    return _SEARCH_STATES.get(seid)


def _render_detail_card(
    state: _SearchState, idx: int, ptoken: str, seid: str, ehtagdb,
    *, sizes: dict[EHMode, int] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """L2 详情卡：标题 + 类型/语言/页数 + 折叠 tag 区 + 6 按钮（4 模式 + 归档下载 + 返回）。

    4 模式按钮走现有 `eh:{ptoken}:<mode>` 回调（Telegra.ph 发布）。
    [归档下载] 进 L3 zip 选单。
    `sizes` 由 size prefetch 完成后回填；首次渲染时为 None（裸 label）。
    """
    it = state.page.items[idx]
    text = _render_eh_detail_card(
        title=it.title,
        host=state.host,
        category=it.category,
        pages=it.pages,
        tags=it.tags,
        ehtagdb=ehtagdb,
        footer_prompt="选择处理方式（前 4 个发 Telegra.ph，归档下载产出 zip）：",
    )
    # 复用 _eh_mode_buttons：sizes 非 None 时按 mode 在 label 后拼 ' ~XX MB'
    rows = _eh_mode_buttons(ptoken, prefix="eh", sizes=sizes)
    rows.append([InlineKeyboardButton("📦 归档下载（zip）", callback_data=f"ehs_arch_menu:{ptoken}:{seid}:{idx}")])
    rows.append([InlineKeyboardButton("⬅ 返回搜索结果", callback_data=f"ehs_back2list:{seid}")])
    return text, InlineKeyboardMarkup(rows)


def _render_archive_menu(
    state: _SearchState, idx: int, ptoken: str, seid: str, ehtagdb,
    *, sizes: dict[EHMode, int] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """L3 zip 选单：4 模式 + 返回详情。4 模式按钮走现有 `eha:{ptoken}:<mode>` 回调。

    `sizes` 由 size prefetch 完成后回填；首次渲染时为 None（裸 label）。
    """
    it = state.page.items[idx]
    text = _render_eh_detail_card(
        title=it.title,
        host=state.host,
        category=it.category,
        pages=it.pages,
        tags=it.tags,
        ehtagdb=ehtagdb,
        footer_prompt="选择下载模式（产出压缩包）：",
    )
    rows = _eh_mode_buttons(ptoken, prefix="eha", sizes=sizes)
    rows.append([InlineKeyboardButton("⬅ 返回详情", callback_data=f"ehs_back2det:{seid}:{idx}")])
    return text, InlineKeyboardMarkup(rows)


def _search_invalidate_active_ptoken(state: _SearchState) -> None:
    """切层/翻页前调用：把当前 L2/L3 的 ptoken 从 _PENDING 立刻清掉，
    让晚到的 size prefetch 在 `_safe_update_buttons` 第一关就失败。

    没有 active_ptoken（L1 列表页）时是 no-op。
    """
    old = state.active_ptoken
    state.active_ptoken = None
    if old is not None:
        _PENDING.pop(old, None)


def _make_pending_for_item(state: _SearchState, idx: int, query) -> str:
    """为 L2/L3 的 eh:/eha: 按钮生成新的 _PENDING token + ref。返回 ptoken。

    每次进 L2 或返回 L2 都新生成；调用前必须先 _search_invalidate_active_ptoken
    把旧 ptoken 从 _PENDING 清掉，避免旧 prefetch 把按钮覆盖回旧详情卡按钮。
    """
    it = state.page.items[idx]
    ptoken = uuid.uuid4().hex[:10]
    ref = ParsedRef(
        provider=state.host, kind="gallery",
        id=f"{it.gid}/{it.token}", raw=it.url,
    )
    _PENDING[ptoken] = _Pending(
        ref=ref,
        chat_id=query.message.chat.id,
        msg_id=query.message.message_id,
        user_id=state.user_id,
        created_at=time.time(),
        force_r2=state.force_r2,
    )
    state.active_ptoken = ptoken
    return ptoken


async def _handle_ehs_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """搜索结果列表 → L2 详情卡：edit 同条消息。"""
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer()
        return
    seid, idx_str = parts[1], parts[2]
    state = _SEARCH_STATES.get(seid)
    if state is None:
        await query.answer("⚠️ 搜索已过期，请重新 /ehsearch", show_alert=True)
        return
    if query.from_user.id != state.user_id:
        await query.answer("⚠️ 这个搜索来自其他用户", show_alert=True)
        return
    try:
        idx = int(idx_str)
        _ = state.page.items[idx]
    except (ValueError, IndexError):
        await query.answer("⚠️ 无效条目", show_alert=True)
        return

    # 进 L2 前先把可能存在的旧 L2/L3 ptoken 失效，否则旧 prefetch 完成时
    # 仍会把按钮覆盖为旧条目的按钮（lambda 捕获 idx 是不变的，但 state.page
    # 和 active_ptoken 已经变了，需要在 _PENDING 这一关把旧 ptoken 拦掉）。
    _search_invalidate_active_ptoken(state)

    ptoken = _make_pending_for_item(state, idx, query)
    text, kb = _render_detail_card(state, idx, ptoken, seid, _get_ehtagdb(context))
    await query.answer()
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"ehs_open edit failed: {e}")
        return
    # 异步拿 archive 两档大小，回填按钮 label。L2 详情卡有"归档下载"+"返回"两条额外行，
    # 用 keyboard_builder 复用 _render_detail_card 自己的渲染逻辑。
    # 显式快照本次 idx：lambda 捕获的是绑定时的局部 idx；但 keyboard_builder
    # 即便用错 idx，最终 _safe_update_buttons 也会因 ptoken 已被清而拒绝写入。
    ehtagdb = _get_ehtagdb(context)
    pending = _PENDING.get(ptoken)
    ref = pending.ref if pending else None
    if ref is not None:
        snapshot_idx = idx
        _schedule_eh_size_prefetch(
            context, query.message, ptoken, ref, prefix="eh",
            keyboard_builder=lambda sizes: _render_detail_card(
                state, snapshot_idx, ptoken, seid, ehtagdb, sizes=sizes,
            )[1],
        )


async def _handle_ehs_arch_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """L2 详情卡 → L3 zip 选单：edit 同条消息。"""
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) != 4:
        await query.answer()
        return
    _ptoken_in_cb, seid, idx_str = parts[1], parts[2], parts[3]
    state = _SEARCH_STATES.get(seid)
    if state is None:
        await query.answer("⚠️ 搜索已过期，请重新 /ehsearch", show_alert=True)
        return
    if query.from_user.id != state.user_id:
        await query.answer("⚠️ 这个搜索来自其他用户", show_alert=True)
        return
    try:
        idx = int(idx_str)
        _ = state.page.items[idx]
    except (ValueError, IndexError):
        await query.answer("⚠️ 无效条目", show_alert=True)
        return

    # 进 L3 前先把 L2 的 active ptoken 失效，再为 L3 挂新 ptoken。
    # 否则 L2 起的 size prefetch 可能在 L3 已显示后回填，把 L3 按钮覆盖回 L2 模板。
    # callback_data 里的旧 ptoken 不复用——_make_pending_for_item 会把 active_ptoken
    # 替换为新 ptoken；下一步 _render_archive_menu 用新 ptoken 渲染按钮。
    _search_invalidate_active_ptoken(state)
    ptoken = _make_pending_for_item(state, idx, query)

    text, kb = _render_archive_menu(state, idx, ptoken, seid, _get_ehtagdb(context))
    await query.answer()
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"ehs_arch_menu edit failed: {e}")
        return
    # L3 zip 选单异步拿大小回填
    ehtagdb = _get_ehtagdb(context)
    pending = _PENDING.get(ptoken)
    ref = pending.ref if pending else None
    if ref is not None:
        snapshot_idx = idx
        _schedule_eh_size_prefetch(
            context, query.message, ptoken, ref, prefix="eha",
            keyboard_builder=lambda sizes: _render_archive_menu(
                state, snapshot_idx, ptoken, seid, ehtagdb, sizes=sizes,
            )[1],
        )


async def _handle_ehs_back2list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """L2/L3 → L1 搜索结果列表：edit 同条消息。"""
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) != 2:
        await query.answer()
        return
    seid = parts[1]
    state = _SEARCH_STATES.get(seid)
    if state is None:
        await query.answer("⚠️ 搜索已过期，请重新 /ehsearch", show_alert=True)
        return
    if query.from_user.id != state.user_id:
        await query.answer("⚠️ 这个搜索来自其他用户", show_alert=True)
        return
    # 回 L1 列表后没有 active ptoken；旧 L2/L3 起的 prefetch 不应再覆盖按钮。
    _search_invalidate_active_ptoken(state)
    text, kb = _render_search_message(seid, state, _get_ehtagdb(context))
    await query.answer()
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"ehs_back2list edit failed: {e}")


async def _handle_ehs_back2det(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """L3 → L2 详情卡：edit 同条消息（重新生成 ptoken）。"""
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer()
        return
    seid, idx_str = parts[1], parts[2]
    state = _SEARCH_STATES.get(seid)
    if state is None:
        await query.answer("⚠️ 搜索已过期，请重新 /ehsearch", show_alert=True)
        return
    if query.from_user.id != state.user_id:
        await query.answer("⚠️ 这个搜索来自其他用户", show_alert=True)
        return
    try:
        idx = int(idx_str)
        _ = state.page.items[idx]
    except (ValueError, IndexError):
        await query.answer("⚠️ 无效条目", show_alert=True)
        return
    # L3→L2 切层：把 L3 的 ptoken 立刻失效，再为 L2 挂新 ptoken。
    _search_invalidate_active_ptoken(state)
    ptoken = _make_pending_for_item(state, idx, query)
    text, kb = _render_detail_card(state, idx, ptoken, seid, _get_ehtagdb(context))
    await query.answer()
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"ehs_back2det edit failed: {e}")
        return
    # L3 → L2 同样异步拿大小回填
    ehtagdb = _get_ehtagdb(context)
    pending = _PENDING.get(ptoken)
    ref = pending.ref if pending else None
    if ref is not None:
        snapshot_idx = idx
        _schedule_eh_size_prefetch(
            context, query.message, ptoken, ref, prefix="eh",
            keyboard_builder=lambda sizes: _render_detail_card(
                state, snapshot_idx, ptoken, seid, ehtagdb, sizes=sizes,
            )[1],
        )


async def _handle_ehs_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) != 2:
        await query.answer()
        return
    seid = parts[1]
    state = _SEARCH_STATES.get(seid)
    if state is None:
        await query.answer("⚠️ 搜索已过期，请重新 /ehsearch", show_alert=True)
        return
    if query.from_user.id != state.user_id:
        await query.answer("⚠️ 这个搜索来自其他用户", show_alert=True)
        return
    state.expanded = True
    await query.answer("展开全部")
    # 展开本身没切 L2/L3，但稳妥起见：active_ptoken 不应该在 L1 时还活着；
    # 万一上一次 back2list 漏清，这里再保一次。
    _search_invalidate_active_ptoken(state)
    text, kb = _render_search_message(seid, state, _get_ehtagdb(context))
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"ehs_more edit failed: {e}")


async def _ehs_navigate(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, direction: str,
) -> None:
    """ehs_next / ehs_prev 共享的翻页逻辑。direction ∈ {"next", "prev"}。"""
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) != 2:
        await query.answer()
        return
    seid = parts[1]
    state = _SEARCH_STATES.get(seid)
    if state is None:
        await query.answer("⚠️ 搜索已过期，请重新 /ehsearch", show_alert=True)
        return
    if query.from_user.id != state.user_id:
        await query.answer("⚠️ 这个搜索来自其他用户", show_alert=True)
        return

    if direction == "next":
        nav_url = state.page.next_url
        edge_msg = "已经是最后一页"
    else:
        nav_url = state.page.prev_url
        edge_msg = "已经是第一页"
    if not nav_url:
        await query.answer(edge_msg)
        return

    # 从 ?next=<gid> 或 ?prev=<gid> 抠出 gid
    nm = re.search(rf"[?&]{direction}=(\d+)", nav_url)
    if not nm:
        await query.answer("⚠️ 翻页 URL 异常", show_alert=True)
        return
    gid = int(nm.group(1))

    await query.answer("加载中...")
    _, registry, *_ = _ctx(context)
    kwargs = (
        {"next_param": gid} if direction == "next" else {"prev_param": gid}
    )
    try:
        new_page = await _ehsearch_dispatch(
            registry, state.keyword, force_host=state.host, **kwargs,
        )
    except EHSearchError as e:
        await query.answer(f"⚠️ 翻页失败：{e}", show_alert=True)
        return
    except Exception as e:
        logger.exception(f"ehs_{direction} dispatch failed")
        await query.answer(f"⚠️ 翻页失败：{e}", show_alert=True)
        return

    if not new_page.items:
        await query.answer(f"{'下一页' if direction == 'next' else '上一页'}没有结果")
        return

    state.page = new_page
    state.expanded = False
    state.created_at = time.time()   # 翻页续命，避免长时间浏览过期
    # 翻页后 page.items 已变；旧 L2/L3 ptoken 对应的 idx 现在指向不同条目，
    # 必须立刻把它从 _PENDING 清掉，不让旧 prefetch 把按钮覆盖到错误条目上。
    _search_invalidate_active_ptoken(state)

    text, kb = _render_search_message(seid, state, _get_ehtagdb(context))
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"ehs_{direction} edit failed: {e}")


async def _handle_ehs_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ehs_navigate(update, context, direction="next")


async def _handle_ehs_prev(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ehs_navigate(update, context, direction="prev")


async def _handle_ehs_arch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[归档下载] 按钮：用 gid+token 触发 /archive 同款 4 模式选择。"""
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) != 4:
        await query.answer()
        return
    _, gid, token, host_short = parts
    host = _eh_host_from_short(host_short)
    await query.answer()

    placeholder = await query.message.reply_text("📖 解析中...")
    ref = ParsedRef(
        provider=host, kind="gallery", id=f"{gid}/{token}",
        raw=f"https://{host}/g/{gid}/{token}/",
    )
    await _eh_offer_archive_modes_on_placeholder(
        context, ref, placeholder=placeholder, user_id=query.from_user.id,
    )


_TZ_UTC8 = _dt.timezone(_dt.timedelta(hours=8))


def _fmt_utc8(ts: _dt.datetime) -> str:
    """tz-aware UTC datetime → 北京时间 (UTC+8) 字符串。"""
    return ts.astimezone(_TZ_UTC8).strftime("%Y-%m-%d %H:%M")


# /cache stats 展示用：FallbackReason 枚举 → 中文一句话解释。
# 枚举值定义在 pixivfeed/publisher/_resolver.py:FallbackReason。
# 新增 reason 时同步加一行；缺失项渲染时退化为只显示英文。
_FALLBACK_REASON_ZH: dict[str, str] = {
    "r2_disabled":        "R2 未启用",
    "size_guard_skipped": "画廊 >1GB 跳过 R2",
    "r2_batch_failed":    "整批 R2 上传失败",
    "r2_partial":         "部分图上传失败",
    "local_file_missing": "本地图片文件缺失",
}


async def cmd_cache(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cache <子命令> —— admin only。telegraph_cache 表管理。

    子命令：
        /cache invalidate <kind> <id>   按精确 kind / SQL LIKE pattern 失效
        /cache stats                    查看 cache 总览（durable 渗透率等）
        /cache help                     本帮助

    典型用例（用户报"页面打不开了"时）：
        /cache invalidate pixiv/illust 12345
        /cache invalidate ehentai/gallery/page_sample 3936793
        /cache invalidate ehentai/gallery/% 3936793     ← 同 gid 全 mode 一起删
        /cache invalidate exhentai/% 3936793
        /cache invalidate pixiv/novel 67890

    失效仅删一行 cache 索引，不删 telegra.ph 实际页面 / R2 / nginx 文件。
    下次任何用户提交相同链接 → cache miss → 走完整下载发布流程拿新 URL。
    """
    config: Config = context.bot_data["config"]
    allowlist: AllowList = context.bot_data["allowlist"]
    if not is_admin(update, allowlist):
        # 静默：避免暴露命令存在
        return
    await _track_user(update, context)

    args = list(context.args or [])
    if not args:
        await _cmd_cache_help(update)
        return

    sub = args[0].lower()
    tg_cache: TelegraphCache = context.bot_data["telegraph_cache"]

    if sub in ("help", "?"):
        await _cmd_cache_help(update)
        return

    if sub == "stats":
        try:
            cs = await tg_cache.stats()
        except Exception as e:
            logger.exception("/cache stats failed")
            await update.message.reply_text(f"⚠️ 读取失败：{e}")
            return
        lines = [
            "telegraph_cache 总览",
            "─────────",
            f"总条目：{cs.total}",
            f"  durable（图在 R2，长期可用）：{cs.durable}",
            f"  legacy（升级前条目，元数据缺失）：{cs.legacy}",
            f"  fallback（存于服务器中，到期失效）：{cs.total - cs.durable - cs.legacy}",
        ]
        if cs.fallback_breakdown:
            lines.append("\nfallback 原因分布：")
            for reason, cnt in sorted(cs.fallback_breakdown.items(), key=lambda x: -x[1]):
                zh = _FALLBACK_REASON_ZH.get(reason, "")
                label = f"{reason} ({zh})" if zh else reason
                lines.append(f"  {label}: {cnt}")
        await update.message.reply_text("\n".join(lines))
        return

    if sub == "invalidate":
        if len(args) < 3:
            await update.message.reply_text(
                "用法：/cache invalidate <kind> <id>\n"
                "kind 支持 SQL LIKE 通配符 %，例：\n"
                "  /cache invalidate pixiv/illust 12345\n"
                "  /cache invalidate ehentai/gallery/page_sample 3936793\n"
                "  /cache invalidate ehentai/gallery/% 3936793   ← 同 gid 全 mode 一起删\n"
                "  /cache invalidate exhentai/% 3936793\n"
                "  /cache invalidate pixiv/novel 67890"
            )
            return
        kind_pattern = args[1]
        pixiv_id = args[2]
        try:
            n = await tg_cache.invalidate_by_pattern(kind_pattern, pixiv_id)
        except Exception as e:
            logger.exception(f"/cache invalidate {kind_pattern} {pixiv_id} failed")
            await update.message.reply_text(f"⚠️ 失效失败：{e}")
            return
        if n == 0:
            await update.message.reply_text(
                f"（没有匹配的 cache 行：kind={kind_pattern}, id={pixiv_id}）"
            )
        else:
            await update.message.reply_text(
                f"✓ 已失效 {n} 行 cache（kind={kind_pattern}, id={pixiv_id}）\n"
                "下次用户提交相同链接时会重新发布。"
            )
        return

    await update.message.reply_text(
        f"未知子命令 {sub!r}。试试 /cache help"
    )


async def _cmd_cache_help(update: Update) -> None:
    await update.message.reply_text(
        "/cache invalidate <kind> <id>   失效一条/一组 cache（kind 支持 % 通配）\n"
        "/cache stats                    查看 cache 总览（durable 渗透率）\n"
        "/cache help                     本帮助\n\n"
        "示例：\n"
        "  /cache invalidate pixiv/illust 12345\n"
        "  /cache invalidate ehentai/gallery/% 3936793\n\n"
        "（注：R2 LRU 与 cache_dir 清理已自动联动失效，本命令仅做兜底）"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """admin only。用法见上面注释。"""
    config: Config = context.bot_data["config"]
    if update.effective_user is None:
        return
    if update.effective_user.id not in set(config.auth.admin_users):
        # 静默：避免暴露命令存在
        return
    await _track_user(update, context)
    store = _usage_store(context)
    if store is None:
        await update.message.reply_text("⚠️ usage_store 未初始化")
        return

    args = list(context.args or [])

    # /stats system
    if args and args[0].lower() == "system":
        cache_dir = Path(config.storage.cache_dir)
        cache_size = await asyncio.to_thread(_dir_size, cache_dir)
        total, used, free = _disk_usage(cache_dir if cache_dir.exists() else Path("/"))
        lines = [
            "用量统计 · system",
            f"缓存目录：{cache_dir}",
            f"缓存占用：{fmt_bytes(cache_size)}",
            f"磁盘总量：{fmt_bytes(total)}",
            f"已用：{fmt_bytes(used)}",
            f"剩余：{fmt_bytes(free)}",
        ]

        # R2 占用（仅在启用时）。读 bot_data["r2_stats"] 缓存——后台 LRU loop
        # 每 lru_check_interval_minutes 分钟扫一次 list_all 时顺手填的，不每次
        # /stats 都重扫（list_all 在 80GB 量级要 ~200 次 API 调用很贵）。
        r2_client = context.bot_data.get("r2_client")
        r2_cfg = config.storage.r2
        if r2_client is not None and r2_cfg.enabled:
            cap_bytes = r2_cfg.capacity_gb * 1024 ** 3
            high_at = int(cap_bytes * 0.9)
            stats = context.bot_data.get("r2_stats")
            meta = context.bot_data.get("r2_stats_meta") or {}
            stale = bool(meta.get("stale"))
            failures_dq = meta.get("failures_deque")
            failures_24h = 0
            if failures_dq is not None:
                # 读侧也裁一刀，避免显示陈旧 24h 窗口外的失败
                now_ts = time.time()
                cutoff = now_ts - 86400
                while failures_dq and failures_dq[0] < cutoff:
                    failures_dq.popleft()
                failures_24h = len(failures_dq)
            if stats is None:
                lines.extend([
                    "",
                    f"R2 bucket：{r2_cfg.bucket}（启用，但首次扫描尚未完成）",
                    f"R2 prefix：{r2_cfg.prefix or '(整 bucket)'}",
                    f"配置容量：{r2_cfg.capacity_gb} GB",
                    f"扫描间隔：{r2_cfg.lru_check_interval_minutes} 分钟",
                ])
                if failures_24h:
                    lines.append(f"扫描失败次数（rolling 24h, since process start）：{failures_24h}")
            else:
                pct = (stats.total_bytes / cap_bytes * 100) if cap_bytes > 0 else 0
                age_min = (
                    int((time.time() - stats.scanned_at.timestamp()) / 60)
                )
                lines.extend([
                    "",
                    f"R2 bucket：{r2_cfg.bucket}",
                    f"R2 prefix：{r2_cfg.prefix or '(整 bucket)'}",
                    f"R2 占用：{fmt_bytes(stats.total_bytes)} / {r2_cfg.capacity_gb} GB ({pct:.1f}%)",
                    f"R2 对象数：{stats.object_count:,}",
                    f"LRU 触发阈值：{fmt_bytes(high_at)}（90%，清到 70%）",
                ])
                if stats.oldest_at and stats.newest_at:
                    lines.extend([
                        f"最旧对象：{_fmt_utc8(stats.oldest_at)}（UTC+8）",
                        f"最新对象：{_fmt_utc8(stats.newest_at)}（UTC+8）",
                    ])
                if stale:
                    fail_cause = meta.get("last_scan_failed_cause") or "unknown"
                    lines.append(
                        f"<i>⚠️ 上次扫描失败，数据 stale（{age_min} 分钟前）。原因：{fail_cause}</i>"
                    )
                else:
                    lines.append(f"<i>数据扫描于 {age_min} 分钟前</i>")
                if failures_24h:
                    lines.append(
                        f"<i>扫描失败次数（rolling 24h, since process start）：{failures_24h}</i>"
                    )
        elif r2_cfg.enabled and r2_client is None:
            lines.extend(["", "⚠️ R2 已配置 enabled=true 但 client 未初始化"])
        else:
            lines.extend(["", "R2：未启用（storage.r2.enabled=false）"])

        # Telegraph cache durability 渗透率（PR-2 引入；监测修复是否真的起作用）
        tg_cache = context.bot_data.get("telegraph_cache")
        if tg_cache is not None:
            try:
                cs = await tg_cache.stats()
            except Exception:
                logger.exception("/stats system: tg_cache.stats() failed; skipping")
                cs = None
            if cs is not None and cs.total > 0:
                lines.extend([
                    "",
                    f"Telegraph cache：总 {cs.total:,} 条 / "
                    f"durable {cs.durable:,} / legacy {cs.legacy:,}",
                ])
                if cs.fallback_breakdown:
                    parts = ", ".join(
                        f"{r or '(empty)'}={c}"
                        for r, c in sorted(cs.fallback_breakdown.items())
                    )
                    lines.append(f"fallback_reason 分布：{parts}")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    # /stats r2_evict：手工触发一次 R2 扫描 + LRU 清理（admin 调试用）。
    # 行为：发"⏳ 扫描 R2..."占位 → list_all → 把新 snapshot 塞 bot_data["r2_stats"]
    #      （顺带刷新 /stats system 看到的缓存）→ 用同一份 objects 跑 LRU 清理 →
    #      edit 占位为最终结果（含清理摘要 + 当前用量），不需要再敲 /stats system 查。
    if args and args[0].lower() == "r2_evict":
        r2_client = context.bot_data.get("r2_client")
        r2_cfg = config.storage.r2
        if r2_client is None or not r2_cfg.enabled:
            await update.message.reply_text("⚠️ R2 未启用")
            return
        if r2_cfg.capacity_gb <= 0:
            await update.message.reply_text("⚠️ storage.r2.capacity_gb=0，LRU 已禁用")
            return
        cap_bytes = r2_cfg.capacity_gb * 1024 ** 3
        high = int(cap_bytes * 0.9)
        low = int(cap_bytes * 0.7)

        placeholder = await update.message.reply_text("⏳ 扫描 R2 bucket...")
        try:
            objects = await r2_client.list_all()
        except R2ListIncomplete as e:
            logger.warning(
                f"/stats r2_evict: list_all incomplete after {e.scanned_pages} page(s); "
                f"refusing to evict on partial data. cause={e.cause}"
            )
            # 与后台 LRU loop 一致：失败也要计入 24h rolling deque + 写 stale meta，
            # 否则 admin 手动触发的失败会让 /stats system 显示"无失败"误导排查。
            _record_r2_scan_failure(context, e)
            await placeholder.edit_text(
                f"⚠️ R2 扫描不完整（共扫到 {e.scanned_pages} 页就中断），本次拒绝在部分数据上跑 LRU。\n"
                f"原因：{str(e.cause)[:200]}"
            )
            return
        except Exception as e:
            logger.exception("/stats r2_evict: list_all failed")
            await placeholder.edit_text(f"⚠️ R2 扫描失败：{e}")
            return

        snapshot_before = stats_from_objects(objects)
        # 即使没触发清理也刷新缓存——这是用户 explicit 触发的最新数据
        context.bot_data["r2_stats"] = snapshot_before
        # 同步清掉 stale meta：扫描成功了就别让 /stats system 继续显示 stale。
        _record_r2_scan_success(context, snapshot_before.scanned_at)

        await placeholder.edit_text(
            f"⏳ 扫描完成（{snapshot_before.object_count:,} 对象，"
            f"{fmt_bytes(snapshot_before.total_bytes)}）。"
            f"评估清理..."
        )

        try:
            removed, freed, deleted_keys = await lru_evict_to_target(
                r2_client,
                high_watermark_bytes=high, low_watermark_bytes=low,
                objects=objects,
            )
        except Exception as e:
            logger.exception("/stats r2_evict: evict failed")
            await placeholder.edit_text(f"⚠️ LRU 清理失败：{e}")
            return

        # 清理完成 → 用 patched snapshot 反映现状
        if removed > 0:
            new_total = snapshot_before.total_bytes - freed
            new_count = snapshot_before.object_count - removed
            patched = R2StatsSnapshot(
                scanned_at=snapshot_before.scanned_at,
                total_bytes=new_total,
                object_count=new_count,
                oldest_at=snapshot_before.oldest_at,   # 删的是最旧——精确值要重扫
                newest_at=snapshot_before.newest_at,
            )
            context.bot_data["r2_stats"] = patched
            # 联动失效 telegraph_cache（与后台 _r2_lru_loop 一致；
            # 这里复用 helper 保持行为一致）
            tg_cache: TelegraphCache = context.bot_data["telegraph_cache"]
            try:
                inval_count = await invalidate_for_r2_keys(
                    tg_cache, deleted_keys, r2_prefix=r2_cfg.prefix,
                )
            except Exception:
                logger.exception(
                    "/stats r2_evict: invalidate_for_r2_keys failed; cache may be stale"
                )
                inval_count = 0
            pct = (new_total / cap_bytes * 100) if cap_bytes > 0 else 0
            text = (
                f"✅ LRU 清理完成\n"
                f"删除对象：{removed:,}\n"
                f"释放空间：{fmt_bytes(freed)}\n"
                f"失效 telegraph cache 行：{inval_count}\n"
                "─────────\n"
                f"R2 当前占用：{fmt_bytes(new_total)} / {r2_cfg.capacity_gb} GB ({pct:.1f}%)\n"
                f"R2 对象数：{new_count:,}"
            )
        else:
            pct = (snapshot_before.total_bytes / cap_bytes * 100) if cap_bytes > 0 else 0
            text = (
                f"未触发清理：当前用量低于 90% 阈值\n"
                "─────────\n"
                f"R2 占用：{fmt_bytes(snapshot_before.total_bytes)} / {r2_cfg.capacity_gb} GB ({pct:.1f}%)\n"
                f"R2 对象数：{snapshot_before.object_count:,}\n"
                f"LRU 触发阈值：{fmt_bytes(high)}（90%）"
            )
            if snapshot_before.oldest_at and snapshot_before.newest_at:
                text += (
                    f"\n最旧对象：{_fmt_utc8(snapshot_before.oldest_at)}（UTC+8）"
                    f"\n最新对象：{_fmt_utc8(snapshot_before.newest_at)}（UTC+8）"
                )
        await placeholder.edit_text(text)
        return

    # /stats chats [window]：列群组活跃排行
    if args and args[0].lower() == "chats":
        window_s = 24 * 3600
        if len(args) >= 2:
            w = _parse_window(args[1])
            if w is None:
                await update.message.reply_text(f"⚠️ 无法识别时间窗口 {args[1]!r}")
                return
            window_s = w
        since_ts = int(time.time()) - window_s
        win_label = _fmt_window(window_s)
        rows = await store.per_chat_summary(since_ts, limit=20, exclude_private=False)
        if not rows:
            await update.message.reply_text(f"近 {win_label} 没有 chat 维度数据")
            return
        lines = [f"群组/私聊活跃排行（最近 {win_label}，前 {len(rows)}）", "─" * 24]
        for i, c in enumerate(rows, 1):
            uname_part = f"@{c.username}" if c.username else ""
            id_show = _friendly_chat_id(c.chat_id, c.type)
            head = f"{i}. {c.display}"
            tail = f" / {id_show}" + (f" / {uname_part}" if uname_part else "")
            lines.append(head + tail)
            lines.append(
                f"   {c.tasks} 任务 · {c.gp_cost} GP · "
                f"↓{fmt_bytes(c.bytes_in)} / ↑{fmt_bytes(c.bytes_out)}"
            )
        await update.message.reply_text("\n".join(lines))
        return

    # 默认窗口 24h
    window_s = 24 * 3600
    target_user_id: int | None = None
    target_chat_id: int | None = None

    # /stats user X [window]
    # /stats chat X [window]
    if args and args[0].lower() in ("user", "chat"):
        kind = args[0].lower()
        if len(args) < 2:
            usage_hint = (
                "用法：/stats user <@username|user_id> [窗口]"
                if kind == "user"
                else "用法：/stats chat <chat_id|@username> [窗口]"
            )
            await update.message.reply_text(usage_hint)
            return
        ident = args[1]
        if kind == "user":
            if ident.isdigit() or (ident.startswith("-") and ident[1:].isdigit()):
                target_user_id = int(ident)
            else:
                target_user_id = await store.get_user_by_username(ident)
                if target_user_id is None:
                    await update.message.reply_text(
                        f"⚠️ 找不到用户 {ident}（仅能查询曾经触发过 bot 的用户）"
                    )
                    return
        else:
            cid, err = await _resolve_chat_arg(store, ident)
            if cid is None:
                await update.message.reply_text(f"⚠️ {err}")
                return
            target_chat_id = cid
        if len(args) >= 3:
            w = _parse_window(args[2])
            if w is None:
                await update.message.reply_text(f"⚠️ 无法识别时间窗口 {args[2]!r}")
                return
            window_s = w
    elif args:
        # /stats <window>
        w = _parse_window(args[0])
        if w is None:
            await update.message.reply_text(
                "用法：\n"
                "  /stats [窗口]                  总览（群里调默认查本群）\n"
                "  /stats chats [窗口]            按群组/私聊活跃度排行\n"
                "  /stats user <@u|id> [窗口]     单用户\n"
                "  /stats chat <id|@u> [窗口]     单群组（接受 -100… / 短 id / @username）\n"
                "  /stats system                  缓存与磁盘 + R2 用量\n"
                "  /stats r2_evict                立刻扫一次 R2 + 触发 LRU 清理\n"
                "窗口示例：1h / 24h / 7d / 30d（默认 24h）"
            )
            return
        window_s = w

    # 在群里裸 /stats，默认 = 本群范围
    if target_user_id is None and target_chat_id is None:
        chat = update.effective_chat
        if chat is not None and chat.type in ("group", "supergroup"):
            target_chat_id = chat.id

    since_ts = int(time.time()) - window_s
    win_label = _fmt_window(window_s)

    if target_user_id is not None:
        # 单用户详情
        s = await store.user_summary(target_user_id, since_ts)
        display, uname = await store.get_user_display(target_user_id)
        breakdown = await store.kind_breakdown(since_ts, user_id=target_user_id)
        lines = [
            f"用量统计 · 用户 {display}",
            f"  username: {uname or '(none)'} | id: {target_user_id}",
            f"  窗口：最近 {win_label}",
            "─" * 24,
            f"任务总数：{s['total']}（成功 {s['ok']} / 失败 {s['failed']} / 取消 {s['cancelled']}）",
            f"GP 消耗：{s['gp_cost']}",
            f"下载流量：{fmt_bytes(s['bytes_in'])}",
            f"上传流量：{fmt_bytes(s['bytes_out'])}",
        ]
        if breakdown:
            lines.append("")
            lines.append("按类别：")
            for kind, count, gp in breakdown:
                kname = KIND_ZH.get(kind, kind)
                lines.append(f"  {kname}: {count} 次" + (f"，{gp} GP" if gp else ""))
        await update.message.reply_text("\n".join(lines))
        return

    # 总览（可选限定 chat）
    s = await store.total_summary(since_ts, chat_id=target_chat_id)
    breakdown = await store.kind_breakdown(since_ts, chat_id=target_chat_id)
    per_user = await store.per_user_summary(since_ts, chat_id=target_chat_id, limit=10)

    title = "用量总览"
    if target_chat_id is not None:
        chat_disp, chat_type, chat_uname = await store.get_chat_display(target_chat_id)
        id_show = _friendly_chat_id(target_chat_id, chat_type)
        title += f" · {chat_disp}（{id_show}）"
    lines = [
        f"{title}（最近 {win_label}）",
        "─" * 24,
        f"任务总数：{s['total']}（成功 {s['ok']} / 失败 {s['failed']} / 取消 {s['cancelled']}）",
        f"GP 消耗：{s['gp_cost']}",
        f"下载流量：{fmt_bytes(s['bytes_in'])}",
        f"上传流量：{fmt_bytes(s['bytes_out'])}",
    ]
    if breakdown:
        lines.append("")
        lines.append("按类别：")
        for kind, count, gp in breakdown:
            kname = KIND_ZH.get(kind, kind)
            lines.append(f"  {kname}: {count} 次" + (f"，{gp} GP" if gp else ""))
    if per_user:
        lines.append("")
        lines.append("按用户排行（前 10）：")
        for i, u in enumerate(per_user, 1):
            uname_part = f"@{u.username}" if u.username else "(no username)"
            lines.append(
                f"{i}. {u.display}  {uname_part} / {u.user_id}\n"
                f"   {u.tasks} 任务 · {u.gp_cost} GP · ↓{fmt_bytes(u.bytes_in)} / ↑{fmt_bytes(u.bytes_out)}"
            )

    # 全局总览（未限定 chat）再补一段"按群组排行"
    if target_chat_id is None:
        per_chat = await store.per_chat_summary(since_ts, limit=10, exclude_private=True)
        if per_chat:
            lines.append("")
            lines.append("按群组排行（前 10）：")
            for i, c in enumerate(per_chat, 1):
                id_show = _friendly_chat_id(c.chat_id, c.type)
                uname_part = f" / @{c.username}" if c.username else ""
                lines.append(
                    f"{i}. {c.display}  {id_show}{uname_part}\n"
                    f"   {c.tasks} 任务 · {c.gp_cost} GP · ↓{fmt_bytes(c.bytes_in)} / ↑{fmt_bytes(c.bytes_out)}"
                )

    await update.message.reply_text("\n".join(lines))


def _friendly_chat_id(chat_id: int, chat_type: str = "") -> str:
    """把 bot api 形式的 chat_id 转成对用户友好的展示：
    - supergroup/channel（-100… 开头）剥掉前缀，前面带 `c/`，对应 t.me/c/<id> 链接里的形式
    - 普通群（负数，无 -100）原样
    - 私聊（正数）原样
    """
    s = str(chat_id)
    if s.startswith("-100"):
        return f"c/{s[4:]}"
    return s


__all__ = [
    "handle_message",
    "handle_callback",
    "handle_callback_archive",
    "cmd_pixiv_telegraph",
    "cmd_pixiv_direct",
    "cmd_archive",
    "cmd_ehsearch",
    "cmd_zip2tph",
    "cmd_cache",
    "cmd_stats",
    "handle_zip_document",
    "cmd_start",
    "cmd_help",
]
