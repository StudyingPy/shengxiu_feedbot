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
from ...provider import ParsedRef, ProviderRegistry, StatusUpdater
from ...provider.ehentai import EHError, EHGalleryUnavailable, EHMode
from ...provider.ehentai import _EHFamilyProvider as EHFamilyBase
from ...provider.ehentai._archive import (
    ArchiveError,
    ArchiveLockedError,
    compute_archive_timeout,
    download_archive_with_timeout,
    fetch_archiver_token,
    refresh_download_link,
    request_archive,
)
from ...provider.ehentai._modes import BASE_HEADERS as EH_BASE_HEADERS
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
    KIND_NHENTAI,
    KIND_PIXIV_NOVEL,
    KIND_PIXIV_TELEGRAPH,
    KIND_ZH,
    KIND_ZIP2TPH,
    AllowList,
    TelegraphCache,
)
from ...utils import logger
from .auth import is_authorized
from .jobqueue import JobQueueManager
from .progress import (
    ByteRateTracker,
    ImageCounter,
    Progress,
    fmt_bytes,
    fmt_duration,
    make_item_hook,
)

# Telegram 标准 Bot API sendDocument 上限 50MB；
# 本地 Bot API（telegram.base_url 配置）可放宽到 ~2GB。
TG_DOCUMENT_LIMIT = 50 * 1024 * 1024
LOCAL_BOT_API_DOCUMENT_LIMIT = 2 * 1024 * 1024 * 1024


# ---------------------------------------------------------------------------
# 共享上下文
# ---------------------------------------------------------------------------


def _ctx(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[Config, ProviderRegistry, TelegraphPublisher, TelegraphCache, AllowList]:
    bd = context.bot_data
    return bd["config"], bd["registry"], bd["publisher"], bd["telegraph_cache"], bd["allowlist"]


def _job_queue(context: ContextTypes.DEFAULT_TYPE) -> JobQueueManager:
    return context.bot_data["job_queue"]


def _usage_store(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data.get("usage_store")


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
    chat_id: int
    msg_id: int
    user_id: int                  # 仅这个 user 可点（防群聊抢按）
    created_at: float


_PENDING: dict[str, _Pending] = {}
_PENDING_TTL = 600  # 10 分钟没人点就清


def _gc_pending() -> None:
    now = time.time()
    expired = [k for k, v in _PENDING.items() if now - v.created_at > _PENDING_TTL]
    for k in expired:
        _PENDING.pop(k, None)
    # 顺便清过期 cancel token
    cancel_expired = [
        k for k, v in _CANCEL_TOKENS.items()
        if now - v.get("ts", 0) > 3600  # type: ignore[union-attr]
    ]
    for k in cancel_expired:
        _CANCEL_TOKENS.pop(k, None)


def _make_eh_keyboard(token: str) -> InlineKeyboardMarkup:
    """eh/ex 模式选择键盘。callback_data 只放短 token + mode value。"""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(EHMode.PAGE_SAMPLE.label_zh, callback_data=f"eh:{token}:page_sample"),
                InlineKeyboardButton(EHMode.PAGE_ORIGINAL.label_zh, callback_data=f"eh:{token}:page_original"),
            ],
            [
                InlineKeyboardButton(EHMode.ARCHIVE_RES.label_zh, callback_data=f"eh:{token}:archive_resample"),
                InlineKeyboardButton(EHMode.ARCHIVE_ORG.label_zh, callback_data=f"eh:{token}:archive_original"),
            ],
            [InlineKeyboardButton("取消", callback_data=f"eh:{token}:cancel")],
        ]
    )


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
    for ref in refs:
        await _handle_ref(update, context, ref, mode="auto")


async def cmd_pixiv_telegraph(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, registry, _, _, allowlist = _ctx(context)
    if not await is_authorized(update, allowlist):
        return
    await _track_user(update, context)
    pixiv = _pixiv_provider(registry)
    if pixiv is None:
        await update.message.reply_text("⚠️ 未启用 Pixiv Provider")
        return
    text = " ".join(context.args or []) or (update.effective_message.text or "")
    refs = pixiv.extract_refs(text)
    if not refs:
        await update.message.reply_text("用法：/pixiv_telegraph <Pixiv 链接>")
        return
    for ref in refs:
        await _handle_ref(update, context, ref, mode="ph")


async def cmd_pixiv_direct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, registry, _, _, allowlist = _ctx(context)
    if not await is_authorized(update, allowlist):
        return
    await _track_user(update, context)
    pixiv = _pixiv_provider(registry)
    if pixiv is None:
        await update.message.reply_text("⚠️ 未启用 Pixiv Provider")
        return
    text = " ".join(context.args or []) or (update.effective_message.text or "")
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
        "  /zip2tph                 回复一张 zip 图片包，发布为 Telegra.ph\n"
        "  /wiki <词条>             查中文维基百科\n"
        "  /chatid                  查看当前 chat_id\n"
        "  /setting list            （仅 admin）查看运行时配置\n"
        "  /setting help            （仅 admin）查看 setting 命令帮助\n"
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
) -> None:
    """mode ∈ {auto, ph, direct}"""
    if ref.provider == "pixiv" and ref.kind == "novel":
        placeholder = await update.message.reply_text(f"⏳ 已收到（pixiv novel {ref.id}），准备处理...")
        user_id = update.effective_user.id if update.effective_user else 0

        async def _do_novel() -> None:
            await _send_pixiv_novel(update, context, ref.id, placeholder=placeholder)

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
        await _handle_pixiv_illust(update, context, ref, mode=mode)
        return

    # eh / ex：私聊弹按钮，群聊默认模式
    if ref.provider in ("e-hentai.org", "exhentai.org"):
        chat = update.effective_chat
        if chat is not None and chat.type == "private":
            await _eh_offer_modes(update, context, ref)
        else:
            placeholder = await update.message.reply_text(
                f"⏳ 已收到（{ref.provider}），准备处理..."
            )
            user_id = update.effective_user.id if update.effective_user else 0

            async def _do() -> None:
                await _eh_run_with_mode(update, context, ref, mode=None, placeholder=placeholder)

            await _enqueue(
                context,
                category="telegraph_publish",
                user_id=user_id,
                placeholder=placeholder,
                work_label=f"{ref.provider} 处理中...",
                coro_factory=_do,
            )
        return

    # nhentai 与其它走默认 telegraph
    placeholder = await update.message.reply_text(f"⏳ 已收到（{ref.provider}），准备处理...")
    user_id = update.effective_user.id if update.effective_user else 0

    async def _do_generic() -> None:
        await _send_via_telegraph_generic(update, context, ref, placeholder=placeholder)

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
    update: Update, context: ContextTypes.DEFAULT_TYPE, ref: ParsedRef
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
    _PENDING[token] = _Pending(
        ref=ref,
        chat_id=placeholder.chat.id,
        msg_id=placeholder.message_id,
        user_id=update.effective_user.id,
        created_at=time.time(),
    )

    title = gallery.title.replace("<", "&lt;").replace(">", "&gt;")
    text = (
        f"📖 <b>{title}</b>\n"
        f"共 {gallery.page_count} 页\n"
        f"🌐 {ref.provider}\n\n"
        "选择下载模式："
    )
    await placeholder.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_make_eh_keyboard(token),
        disable_web_page_preview=True,
    )


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
) -> None:
    """实际执行 eh/ex 抓取与发布。

    mode=None 表示用 provider.default_mode（群聊默认场景）。
    placeholder 是已经存在的可 edit 消息（按钮回调已 edit 过状态）。
    没有 placeholder 时（群聊新建一条），自动 reply 一条。

    e-hentai 抓取过程中如果遇到 unavailable，会自动 fallback 到 exhentai。
    """
    config, registry, publisher, tg_cache, _ = _ctx(context)
    provider = _eh_provider(registry, ref.provider)
    if provider is None:
        return

    if mode is None:
        mode = provider.default_mode

    cache_kind = f"{ref.provider}/gallery/{mode.value}"
    cached = await tg_cache.get(cache_kind, ref.id)
    if cached:
        if placeholder:
            await placeholder.edit_text(cached, disable_web_page_preview=False)
        else:
            msg = update_or_query.effective_message if hasattr(update_or_query, "effective_message") else None
            if msg:
                await msg.reply_text(cached)
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
            if cached:
                await placeholder.edit_text(cached, disable_web_page_preview=False)
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

    await tg_cache.put(cache_kind, ref.id, pub.primary_url, pub.page_count)
    await placeholder.edit_text(pub.primary_url, disable_web_page_preview=False)
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

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer()
        return
    if parts[0] == "eha":
        # 委托给 /archive 流程
        await handle_callback_archive(update, context)
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

    user_id = pending.user_id
    # archive_* 模式归 archive_zip 队列；page_* 走 telegraph_publish
    category = "archive_zip" if mode.is_archive else "telegraph_publish"

    async def _do() -> None:
        await _eh_run_with_mode(update, context, pending.ref, mode=mode, placeholder=msg)

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
        user_id = update.effective_user.id if update.effective_user else 0

        async def _do_ph() -> None:
            await _send_pixiv_illust_via_telegraph(update, context, pid, placeholder=placeholder)

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
# 通用 telegraph（nhentai 等）
# ---------------------------------------------------------------------------


async def _send_via_telegraph_generic(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ref: ParsedRef,
    placeholder=None,
) -> None:
    config, registry, publisher, tg_cache, _ = _ctx(context)

    cache_kind = f"{ref.provider}/{ref.kind}"
    cached = await tg_cache.get(cache_kind, ref.id)
    if cached:
        if placeholder is not None:
            await placeholder.edit_text(cached)
        else:
            await update.message.reply_text(cached)
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

    await tg_cache.put(cache_kind, ref.id, pub.primary_url, pub.page_count)
    await placeholder.edit_text(pub.primary_url)
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
) -> None:
    config, registry, publisher, tg_cache, _ = _ctx(context)
    pixiv = _pixiv_provider(registry)
    assert pixiv is not None

    cached = await tg_cache.get("pixiv/illust", pid)
    if cached:
        if placeholder is not None:
            await placeholder.edit_text(cached)
        else:
            await update.message.reply_text(cached)
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
        )
    except Exception as e:
        logger.exception(f"publish pixiv illust({pid}) failed")
        await placeholder.edit_text(f"⚠️ 发布失败：{e}")
        await _log_usage(
            context, update, kind=KIND_PIXIV_TELEGRAPH, provider="pixiv",
            ref_id=pid, status="failed",
        )
        return

    await tg_cache.put("pixiv/illust", pid, pub.primary_url, pub.page_count)
    await placeholder.edit_text(pub.primary_url)
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
) -> None:
    config, registry, publisher, tg_cache, _ = _ctx(context)
    pixiv = _pixiv_provider(registry)
    if pixiv is None:
        return

    cached = await tg_cache.get("pixiv/novel", nid)
    if cached:
        if placeholder is not None:
            await placeholder.edit_text(cached)
        else:
            await update.message.reply_text(cached)
        return

    if placeholder is None:
        placeholder = await update.message.reply_text("⏳ 处理小说中（可能需要较长时间）...")
    progress = Progress(placeholder, prefix=f"📖 pixiv novel {nid}")
    # novel 流程包含创建多页 telegraph，半路取消会留半成品。整段不可取消。
    await _drop_cancel_button(placeholder)
    try:
        novel, pub = await publish_novel(config, publisher, pixiv, nid, progress=progress)
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

    await tg_cache.put("pixiv/novel", nid, pub.primary_url, pub.page_count)
    await placeholder.edit_text(pub.primary_url)
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
) -> None:
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
    chat_id = update.effective_chat.id
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
    """`/zip2tph`：处理回复或带 caption 的 zip。"""
    config, registry, publisher, _, allowlist = _ctx(context)
    if not await is_authorized(update, allowlist):
        return
    await _track_user(update, context)

    msg = update.effective_message
    target_msg = msg.reply_to_message if msg.reply_to_message else msg
    document = target_msg.document if target_msg else None
    if not _is_zip(document):
        await msg.reply_text(
            "用法：把图片 zip 发给我并在 caption 里写 /zip2tph，"
            "或对 zip 消息回复 /zip2tph"
        )
        return
    await _enqueue_zip_to_telegraph(update, context, target_msg)


async def handle_zip_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """监听 Document：caption 含 /zip2tph 时自动处理。"""
    _, _, _, _, allowlist = _ctx(context)
    if not await is_authorized(update, allowlist):
        return
    await _track_user(update, context)
    msg = update.effective_message
    if not _is_zip(msg.document):
        return
    caption = (msg.caption or "").strip()
    if not caption.lower().startswith("/zip2tph"):
        return
    await _enqueue_zip_to_telegraph(update, context, msg)


async def _enqueue_zip_to_telegraph(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    zip_msg,
) -> None:
    """把 zip2tph 包成队列任务。"""
    document = zip_msg.document
    file_size = document.file_size or 0
    placeholder = await update.effective_message.reply_text(
        f"⏳ 已收到 zip ({fmt_bytes(file_size)})，准备处理..."
    )
    user_id = update.effective_user.id if update.effective_user else 0
    work_label = f"接收 zip ({fmt_bytes(file_size)})..."

    async def _do() -> None:
        await _process_zip_to_telegraph(update, context, zip_msg, placeholder)

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
                    read_timeout=3600, write_timeout=3600,
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
                        read_timeout=3600, write_timeout=3600,
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

        # 拷贝到 cache_dir 让 Nginx 暴露
        token = uuid.uuid4().hex[:10]
        cache_dir = Path(config.storage.cache_dir)
        public_dir = cache_dir / f"zip_{token}"
        public_dir.mkdir(parents=True, exist_ok=True)
        public_urls: list[str] = []
        ctr = ImageCounter(total=len(images), progress=progress, label="拷贝图片")
        for i, src in enumerate(images):
            ext = src.suffix.lower() or ".jpg"
            dest = public_dir / f"p{i:04d}{ext}"
            shutil.copy2(src, dest)
            rel = dest.resolve().relative_to(cache_dir.resolve())
            public_urls.append(f"{config.publish.base_url.rstrip('/')}/{rel.as_posix()}")
            await ctr.tick()

        # 标题：去掉扩展名
        raw_name = document.file_name or "archive.zip"
        title = re.sub(r"\.zip$", "", raw_name, flags=re.IGNORECASE).strip() or "图片包"
        if len(title) > 256:
            title = title[:253] + "..."

        await progress.status("⏳ 发布到 Telegra.ph...")
        await _drop_cancel_button(placeholder)
        await publisher.ensure_account()

        max_per_page = config.publish.max_images_per_page
        chunks = [public_urls[i : i + max_per_page] for i in range(0, len(public_urls), max_per_page)]

        page_urls: list[str] = []
        next_url: str | None = None
        for i in range(len(chunks) - 1, -1, -1):
            nodes: list = []
            if i == 0:
                nodes.append(
                    {
                        "tag": "p",
                        "children": [
                            f"共 {len(public_urls)} 张 · 来自上传 zip：{raw_name}"
                        ],
                    }
                )
            else:
                nodes.append({"tag": "p", "children": [f"（续 {i + 1} / {len(chunks)}）"]})
            for url in chunks[i]:
                nodes.append({"tag": "figure", "children": [{"tag": "img", "attrs": {"src": url}}]})
            if next_url:
                nodes.append(
                    {
                        "tag": "p",
                        "children": [
                            {"tag": "a", "attrs": {"href": next_url}, "children": ["下一页 →"]}
                        ],
                    }
                )

            page_title = title if i == 0 else f"{title} ({i + 1}/{len(chunks)})"
            if len(page_title) > 256:
                page_title = page_title[:253] + "..."
            page = await publisher.tg.create_page(
                title=page_title, content=nodes, return_content=False,
            )
            page_urls.append(page["url"])
            next_url = page["url"]
        page_urls.reverse()

        await progress.finish(page_urls[0])
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
    config, registry, *_ = _ctx(context)
    provider = _eh_provider(registry, ref.provider)
    if provider is None:
        return

    placeholder = await update.message.reply_text("📖 解析中...")
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
        user_id=update.effective_user.id,
        created_at=time.time(),
    )

    title = gallery.title.replace("<", "&lt;").replace(">", "&gt;")
    text = (
        f"📦 <b>{title}</b>\n"
        f"共 {gallery.page_count} 页\n"
        f"🌐 {ref.provider}\n\n"
        "选择下载模式（产出压缩包）："
    )
    # 用 eha: 前缀区分回调
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(EHMode.PAGE_SAMPLE.label_zh, callback_data=f"eha:{token}:page_sample"),
                InlineKeyboardButton(EHMode.PAGE_ORIGINAL.label_zh, callback_data=f"eha:{token}:page_original"),
            ],
            [
                InlineKeyboardButton(EHMode.ARCHIVE_RES.label_zh, callback_data=f"eha:{token}:archive_resample"),
                InlineKeyboardButton(EHMode.ARCHIVE_ORG.label_zh, callback_data=f"eha:{token}:archive_original"),
            ],
            [InlineKeyboardButton("取消", callback_data=f"eha:{token}:cancel")],
        ]
    )
    await placeholder.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


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
                    read_timeout=3600,
                    write_timeout=3600,
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
        text = (
            "用量统计 · system\n"
            f"缓存目录：{cache_dir}\n"
            f"缓存占用：{fmt_bytes(cache_size)}\n"
            f"磁盘总量：{fmt_bytes(total)}\n"
            f"已用：{fmt_bytes(used)}\n"
            f"剩余：{fmt_bytes(free)}"
        )
        await update.message.reply_text(text)
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
                "  /stats system                  缓存与磁盘\n"
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
    "cmd_zip2tph",
    "cmd_stats",
    "handle_zip_document",
    "cmd_start",
    "cmd_help",
]
