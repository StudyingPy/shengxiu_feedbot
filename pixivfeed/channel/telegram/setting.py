"""/setting 命令：admin 私聊修改运行时配置。

子命令：
    /setting list               列出所有可改 key 及当前值
    /setting list <prefix>      只列出某前缀（如 templates / collectors.exhentai）
    /setting get <key>          查看单个值
    /setting set <key> <value>  设置一个值（单行）
    /setting edit <key>         交互式：下一条消息作为 value（用于多行模板）
    /setting unset <key>        删除运行时覆盖（恢复 yaml 值需要重启）
    /setting help               帮助

权限：仅 admin_users 可用，且仅私聊。
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from ...config import RUNTIME_KEYS, SENSITIVE_KEYS, Config
from ...storage import RuntimeSettings
from ...utils import logger
from .auth import is_admin


# 待编辑的 key（用户调用 /setting edit 后，下一条消息进入这个 key）
# 用 user_data 存而不是全局，免得多个 admin 串
_EDIT_PENDING_KEY = "__setting_edit_pending"


def _mask(key: str, value) -> str:
    if value is None or value == "":
        return "(unset)"
    if key in SENSITIVE_KEYS:
        s = str(value)
        if len(s) <= 6:
            return "***"
        return s[:3] + "***" + s[-3:]
    return repr(value)


async def cmd_setting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    runtime: RuntimeSettings = context.bot_data["runtime_settings"]

    # 仅 admin 私聊
    chat = update.effective_chat
    user = update.effective_user
    if user is None or chat is None:
        return
    if not is_admin(update, context.bot_data["allowlist"]):
        return
    if chat.type != "private":
        await update.message.reply_text("⚠️ 为安全起见，/setting 仅限私聊使用")
        return

    args = context.args or []
    if not args:
        await _send_help(update)
        return

    sub = args[0].lower()

    if sub == "help":
        await _send_help(update)
    elif sub == "list":
        prefix = args[1] if len(args) >= 2 else ""
        await _list_settings(update, config, runtime, prefix)
    elif sub == "get":
        if len(args) < 2:
            await update.message.reply_text("用法：/setting get <key>")
            return
        await _get_setting(update, config, runtime, args[1])
    elif sub == "set":
        if len(args) < 3:
            await update.message.reply_text(
                "用法：/setting set <key> <value>\n"
                "多行/含空格的值请用 /setting edit <key>"
            )
            return
        key = args[1]
        # 把 args[2:] 重新拼回（保留空格）
        value = " ".join(args[2:])
        await _set_setting(update, config, key, value, user.id)
    elif sub == "edit":
        if len(args) < 2:
            await update.message.reply_text("用法：/setting edit <key>，然后下一条消息作为新值")
            return
        await _start_edit(update, context, args[1])
    elif sub == "unset":
        if len(args) < 2:
            await update.message.reply_text("用法：/setting unset <key>")
            return
        await _unset_setting(update, config, args[1])
    else:
        await update.message.reply_text(f"未知子命令 {sub!r}。试试 /setting help")


async def _send_help(update: Update) -> None:
    await update.message.reply_text(
        "/setting list [prefix]    列出可改配置\n"
        "/setting get <key>        查看单个值\n"
        "/setting set <key> <val>  设置（单行）\n"
        "/setting edit <key>       下一条消息作为新值（多行/敏感字段用这个）\n"
        "/setting unset <key>      删除 runtime 覆盖（重启后回到 yaml 值）\n"
        "/setting help             本帮助\n\n"
        "示例：\n"
        "  /setting set collectors.exhentai.enabled true\n"
        "  /setting set publish.direct_threshold 8\n"
        "  /setting edit templates.gallery.page_header"
    )


async def _list_settings(
    update: Update, config: Config, runtime: RuntimeSettings, prefix: str
) -> None:
    keys = sorted(RUNTIME_KEYS)
    if prefix:
        keys = [k for k in keys if k.startswith(prefix)]
        if not keys:
            await update.message.reply_text(f"没有匹配前缀 {prefix!r} 的可改配置")
            return

    rt_set = set(runtime.all().keys())
    lines: list[str] = []
    current_section = None
    for k in keys:
        section = k.split(".")[0]
        if section != current_section:
            lines.append(f"\n[{section}]")
            current_section = section
        try:
            value = config.get_field(k)
        except Exception as e:
            value = f"<error: {e}>"
        marker = "★" if k in rt_set else " "
        # 模板字段值通常很长，截断
        rendered = _mask(k, value)
        if len(rendered) > 80:
            rendered = rendered[:77] + "..."
        lines.append(f"  {marker} {k} = {rendered}")
    lines.append("\n（★ 表示该项有 runtime 覆盖，否则用 yaml/默认值）")
    text = "\n".join(lines).strip()
    # Telegram 单条消息 4096 字符限制，超长拆开
    if len(text) <= 4000:
        await update.message.reply_text(text)
    else:
        # 简单按段切
        chunk = ""
        for line in text.splitlines(keepends=True):
            if len(chunk) + len(line) > 4000:
                await update.message.reply_text(chunk)
                chunk = ""
            chunk += line
        if chunk:
            await update.message.reply_text(chunk)


async def _get_setting(
    update: Update, config: Config, runtime: RuntimeSettings, key: str
) -> None:
    if key not in RUNTIME_KEYS:
        await update.message.reply_text(f"⚠️ {key!r} 不在可改配置列表中（/setting list 查看）")
        return
    try:
        value = config.get_field(key)
    except Exception as e:
        await update.message.reply_text(f"⚠️ 读取失败：{e}")
        return
    rt_value = runtime.get(key)
    msg = f"{key} = {_mask(key, value)}\n"
    if rt_value is not None:
        msg += f"（runtime 覆盖中：{_mask(key, rt_value)}）"
    else:
        msg += "（来自 yaml/默认值）"
    await update.message.reply_text(msg)


async def _set_setting(
    update: Update, config: Config, key: str, value: str, user_id: int
) -> None:
    try:
        await config.set_runtime(key, value, updated_by=user_id)
    except KeyError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    except ValueError as e:
        await update.message.reply_text(f"⚠️ 值无效：{e}")
        return
    except Exception as e:
        logger.exception(f"setting set {key}={value!r} failed")
        await update.message.reply_text(f"⚠️ 写入失败：{e}")
        return
    new_value = config.get_field(key)
    await update.message.reply_text(
        f"✓ {key} = {_mask(key, new_value)}\n"
        "已保存到 SQLite 并即时生效。"
    )


async def _unset_setting(update: Update, config: Config, key: str) -> None:
    try:
        removed = await config.unset_runtime(key)
    except Exception as e:
        await update.message.reply_text(f"⚠️ 删除失败：{e}")
        return
    if not removed:
        await update.message.reply_text(f"（{key} 没有 runtime 覆盖）")
        return
    await update.message.reply_text(
        f"✓ 已移除 {key} 的 runtime 覆盖。\n"
        "提示：当前进程内存里仍是覆盖后的值，重启后会恢复 yaml 中的值。"
    )


async def _start_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE, key: str
) -> None:
    if key not in RUNTIME_KEYS:
        await update.message.reply_text(f"⚠️ {key!r} 不在可改配置列表中")
        return
    context.user_data[_EDIT_PENDING_KEY] = key
    await update.message.reply_text(
        f"请在下一条消息中发送 {key} 的新值（支持多行）。\n"
        "发送 /cancel 放弃。"
    )


async def handle_setting_edit_followup(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """检查当前用户是否在 edit 流程中，是则消费这条消息并返回 True。

    必须在 handle_message 之前调用。
    """
    pending_key = context.user_data.get(_EDIT_PENDING_KEY)
    if not pending_key:
        return False

    # 只在私聊处理 edit 流程
    if update.effective_chat is None or update.effective_chat.type != "private":
        return False

    text = update.effective_message.text or ""

    if text.strip() == "/cancel":
        context.user_data.pop(_EDIT_PENDING_KEY, None)
        await update.message.reply_text("已取消编辑")
        return True

    config: Config = context.bot_data["config"]
    user_id = update.effective_user.id
    context.user_data.pop(_EDIT_PENDING_KEY, None)

    try:
        await config.set_runtime(pending_key, text, updated_by=user_id)
    except KeyError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return True
    except Exception as e:
        logger.exception(f"setting edit {pending_key} failed")
        await update.message.reply_text(f"⚠️ 写入失败：{e}")
        return True

    new_value = config.get_field(pending_key)
    await update.message.reply_text(
        f"✓ {pending_key} 已更新（{len(text)} chars）\n"
        f"当前值：{_mask(pending_key, new_value)}"
    )
    return True


__all__ = ["cmd_setting", "handle_setting_edit_followup"]
