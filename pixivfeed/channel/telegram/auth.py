"""白名单鉴权与管理命令。

设计：
- 群组消息：只要群组在白名单里，所有成员都能用（PT 群常态）
- 私聊：必须用户本人在白名单里
- admin_users：永远直通，且专用于执行 /allow /deny /listallow 命令
- inline mode：单独看 user_id 是否 admin（前面已确认这个策略）
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from ...storage import AllowList
from ...utils import logger


async def is_authorized(update: Update, allowlist: AllowList) -> bool:
    """普通消息/命令鉴权：群组按 chat_id，私聊按 user_id。"""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return False
    # admin 任何场景直通
    if user.id in allowlist.admin_users:
        return True
    if chat.type == "private":
        return await allowlist.is_user_allowed(user.id)
    # 群组：群在白名单 OR 用户在白名单
    if await allowlist.is_chat_allowed(chat.id):
        return True
    return await allowlist.is_user_allowed(user.id)


def is_admin(update: Update, allowlist: AllowList) -> bool:
    user = update.effective_user
    return user is not None and user.id in allowlist.admin_users


# ---------------------------------------------------------------------------
# 管理命令实现
# ---------------------------------------------------------------------------


async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/allow <user_id|chat_id> [user|chat]

    无参数时：把当前聊天加入白名单（群组场景常用）
    带数字参数：把指定 ID 加入白名单。第二参数指定类型，缺省按数字正负判断
    （TG 群组 chat_id 是负数）
    """
    allowlist: AllowList = context.bot_data["allowlist"]
    if not is_admin(update, allowlist):
        return
    args = context.args or []
    admin_user = update.effective_user.id

    if not args:
        # 无参数：放行当前 chat
        chat = update.effective_chat
        if chat.type == "private":
            await allowlist.add_user(chat.id, added_by=admin_user)
            await update.message.reply_text(f"✓ 已放行用户 {chat.id}")
        else:
            await allowlist.add_chat(chat.id, added_by=admin_user)
            await update.message.reply_text(f"✓ 已放行本群 {chat.id}")
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("用法：/allow [user_id 或 chat_id] [user|chat]")
        return

    kind = args[1].lower() if len(args) >= 2 else ("chat" if target_id < 0 else "user")
    if kind == "chat":
        await allowlist.add_chat(target_id, added_by=admin_user)
        await update.message.reply_text(f"✓ 已放行群 {target_id}")
    else:
        await allowlist.add_user(target_id, added_by=admin_user)
        await update.message.reply_text(f"✓ 已放行用户 {target_id}")


async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/deny <user_id|chat_id> [user|chat] —— 移除白名单。"""
    allowlist: AllowList = context.bot_data["allowlist"]
    if not is_admin(update, allowlist):
        return
    args = context.args or []
    if not args:
        chat = update.effective_chat
        if chat.type == "private":
            removed = await allowlist.remove_user(chat.id)
        else:
            removed = await allowlist.remove_chat(chat.id)
        await update.message.reply_text("✓ 已移除" if removed else "（不在白名单中）")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("用法：/deny [user_id 或 chat_id] [user|chat]")
        return
    kind = args[1].lower() if len(args) >= 2 else ("chat" if target_id < 0 else "user")
    if kind == "chat":
        removed = await allowlist.remove_chat(target_id)
    else:
        removed = await allowlist.remove_user(target_id)
    await update.message.reply_text(
        f"✓ 已移除 {kind} {target_id}" if removed else f"（{kind} {target_id} 不在白名单）"
    )


async def cmd_listallow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/listallow —— 列出白名单。仅 admin。"""
    allowlist: AllowList = context.bot_data["allowlist"]
    if not is_admin(update, allowlist):
        return
    users = await allowlist.list_users()
    chats = await allowlist.list_chats()
    lines = [f"管理员（来自配置文件）：{sorted(allowlist.admin_users)}"]
    lines.append(f"\n放行用户（{len(users)}）：")
    for e in users[:50]:
        lines.append(f"  {e.id}  (added_by={e.added_by})")
    lines.append(f"\n放行群组（{len(chats)}）：")
    for e in chats[:50]:
        lines.append(f"  {e.id}  (added_by={e.added_by})")
    await update.message.reply_text("\n".join(lines))


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chatid —— 任何用户可用，回报当前聊天和用户的 ID，便于 admin 确认放行对象。"""
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(
        f"chat_id: {chat.id}\nchat_type: {chat.type}\nyour user_id: {user.id}"
    )


__all__ = [
    "is_authorized",
    "is_admin",
    "cmd_allow",
    "cmd_deny",
    "cmd_listallow",
    "cmd_chatid",
]
