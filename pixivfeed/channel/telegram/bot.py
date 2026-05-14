"""TelegramChannel：Bot 应用构建与启动。"""

from __future__ import annotations

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from ...config import Config
from ...provider import ProviderRegistry
from ...publisher import TelegraphPublisher
from ...storage import AllowList, Database, RuntimeSettings, TelegraphCache, UsageStore
from ...utils import logger
from .auth import cmd_allow, cmd_chatid, cmd_deny, cmd_listallow
from .handlers import (
    cmd_archive,
    cmd_ehsearch,
    cmd_help,
    cmd_pixiv_direct,
    cmd_pixiv_telegraph,
    cmd_start,
    cmd_stats,
    cmd_zip2tph,
    handle_callback,
    handle_message,
    handle_zip_document,
)
from .inline import handle_inline
from .jobqueue import JobQueueManager
from .setting import cmd_setting
from .wiki import cmd_wiki


# ---------------------------------------------------------------------------
# 命令菜单
# ---------------------------------------------------------------------------
#
# 分两组 scope 推给 Telegram 的 commands API：
# - 默认 scope：所有用户在私聊/群组里都看得到。只放对普通用户有意义的命令。
# - admin 私聊 scope：仅 admin_users 在私聊 bot 时看得到，含管理类命令。
#
# 注意：Pixiv 默认就会自动响应链接，/pixiv_telegraph /pixiv_direct 是覆盖默认行为，
# 放在公开命令里方便所有用户使用。

PUBLIC_COMMANDS: list[BotCommand] = [
    BotCommand("start", "查看 bot 简介与命令列表"),
    BotCommand("help", "查看 bot 简介与命令列表"),
    BotCommand("chatid", "查看当前 chat_id（用于白名单设置）"),
    BotCommand("pixiv_telegraph", "强制以 Telegra.ph 模式处理 pixiv 链接"),
    BotCommand("pixiv_direct", "强制直接发图模式处理 pixiv 链接"),
    BotCommand("archive", "对链接产出压缩包（eh/ex 仍弹模式按钮）"),
    BotCommand("ehsearch", "关键词搜索 eh/ex 画廊"),
    BotCommand("zip2tph", "把 zip 图片包发布为 Telegra.ph"),
    BotCommand("wiki", "在中文维基百科中查词条"),
]

ADMIN_COMMANDS: list[BotCommand] = [
    *PUBLIC_COMMANDS,
    BotCommand("allow", "白名单：放行用户/群组"),
    BotCommand("deny", "白名单：移除用户/群组"),
    BotCommand("listallow", "白名单：列出当前所有放行项"),
    BotCommand("setting", "运行时配置（仅私聊）"),
    BotCommand("stats", "用量统计（仅 admin）"),
]


async def install_commands(app: Application, config: Config) -> None:
    """把命令菜单推给 Telegram。

    - 默认 scope（所有人）：PUBLIC_COMMANDS
    - 每个 admin 的私聊 scope：ADMIN_COMMANDS
      （Telegram 不支持"按 user_id 列表设 scope"，只能逐个 chat_id 调用一次，
       所以遍历 admin_users 设置。这里 chat_id == user_id，因为 admin 私聊
       bot 时这俩是一回事。）
    """
    try:
        await app.bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
        logger.info(f"Installed {len(PUBLIC_COMMANDS)} public commands")
    except Exception:
        logger.exception("set_my_commands(default) failed; bot will still work without menu")
        return

    for admin_id in config.auth.admin_users:
        try:
            await app.bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as e:
            # 如果 admin 还没和 bot 私聊过，set_my_commands 可能失败，不致命
            logger.warning(f"set_my_commands(admin={admin_id}) failed: {e}")
        else:
            logger.info(f"Installed {len(ADMIN_COMMANDS)} admin commands for user {admin_id}")


def build_application(
    config: Config,
    db: Database,
    registry: ProviderRegistry,
    publisher: TelegraphPublisher,
    runtime_settings: RuntimeSettings,
) -> Application:
    builder = ApplicationBuilder().token(config.telegram.token)
    if config.telegram.base_url:
        # 本地 Bot API：除了 base_url，PTB 还需要 base_file_url + local_mode(True)，
        # 否则 getFile 返回的本地路径会被当成 https 拼回官方域名，>20MB 仍报
        # "File is too big"。
        builder = builder.base_url(config.telegram.base_url)
        if config.telegram.base_file_url:
            builder = builder.base_file_url(config.telegram.base_file_url)
        if config.telegram.local_mode:
            try:
                builder = builder.local_mode(True)
            except AttributeError:
                logger.warning("PTB ApplicationBuilder.local_mode not available; "
                               "consider upgrading python-telegram-bot")
    # 大文件 sendDocument / 下载需要更长 HTTP 超时，否则 90MB 上传必现 Timed out
    builder = (
        builder
        .connect_timeout(60.0)
        .read_timeout(600.0)
        .write_timeout(600.0)
        .pool_timeout(60.0)
    )
    app = builder.build()

    allowlist = AllowList(db, admin_users=config.auth.admin_users)
    telegraph_cache = TelegraphCache(db)
    usage_store = UsageStore(db)
    job_queue = JobQueueManager(admin_users=set(config.auth.admin_users))
    # 类别并发度按"重活"分级，admin 永远优先。默认值（archive_zip=1 / zip2tph=1
    # / direct_image=2 / telegraph_publish=3）见 JobQueueConfig，可在 yaml 调。
    jq = config.job_queue
    job_queue.register("archive_zip", concurrency=jq.archive_zip, max_per_user_pending=2)
    job_queue.register("zip2tph", concurrency=jq.zip2tph, max_per_user_pending=2)
    job_queue.register("direct_image", concurrency=jq.direct_image, max_per_user_pending=3)
    job_queue.register("telegraph_publish", concurrency=jq.telegraph_publish, max_per_user_pending=3)

    app.bot_data["config"] = config
    app.bot_data["registry"] = registry
    app.bot_data["publisher"] = publisher
    app.bot_data["allowlist"] = allowlist
    app.bot_data["telegraph_cache"] = telegraph_cache
    app.bot_data["runtime_settings"] = runtime_settings
    app.bot_data["usage_store"] = usage_store
    app.bot_data["job_queue"] = job_queue

    # 命令
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("pixiv_telegraph", cmd_pixiv_telegraph))
    app.add_handler(CommandHandler("pixiv_direct", cmd_pixiv_direct))
    app.add_handler(CommandHandler("archive", cmd_archive))
    app.add_handler(CommandHandler("ehsearch", cmd_ehsearch))
    app.add_handler(CommandHandler("zip2tph", cmd_zip2tph))
    app.add_handler(CommandHandler("wiki", cmd_wiki))
    app.add_handler(CommandHandler("allow", cmd_allow))
    app.add_handler(CommandHandler("deny", cmd_deny))
    app.add_handler(CommandHandler("listallow", cmd_listallow))
    app.add_handler(CommandHandler("setting", cmd_setting))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # 按钮回调
    app.add_handler(CallbackQueryHandler(handle_callback))

    # 消息监听
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # 文档监听：caption 含 /zip2tph 时处理
    app.add_handler(MessageHandler(filters.Document.ALL, handle_zip_document))

    # Inline mode
    app.add_handler(InlineQueryHandler(handle_inline))

    enabled = ", ".join(p.name for p in registry.all())
    logger.info(f"Bot handlers registered. Registered providers: {enabled}")
    return app


async def init_bot_async(
    config: Config,
    db: Database,
    registry: ProviderRegistry,
    publisher: TelegraphPublisher,
    runtime_settings: RuntimeSettings,
) -> Application:
    app = build_application(config, db, registry, publisher, runtime_settings)

    allowlist: AllowList = app.bot_data["allowlist"]
    for uid in config.auth.initial_allowed_users:
        await allowlist.add_user(uid)
    for cid in config.auth.initial_allowed_chats:
        await allowlist.add_chat(cid)

    job_queue: JobQueueManager = app.bot_data["job_queue"]
    await job_queue.start_all()

    return app


__all__ = ["build_application", "init_bot_async", "install_commands"]
