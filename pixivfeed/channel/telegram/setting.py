"""/setting 命令：admin 私聊修改运行时配置。

推荐直接发送 /setting 打开按钮面板。文字命令保留为快捷方式：
    /setting                    打开设置面板
    /setting <关键词>           搜索设置项
    /setting cookie [ex|pixiv]  粘贴整段 Cookie 并自动识别
    /setting list [prefix]      列出可改配置
    /setting get <key>          查看单项（附按钮）
    /setting set <key> <value>  设置单行值
    /setting edit <key>         下一条消息作为新值
    /setting unset <key>        删除 runtime 覆盖并立即恢复 yaml/默认值

权限：仅 admin_users 可用，且仅私聊。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ...config import (
    RUNTIME_KEYS,
    RUNTIME_SETTING_GROUPS,
    RUNTIME_SETTINGS,
    RUNTIME_SETTINGS_BY_KEY,
    RUNTIME_SETTINGS_BY_SID,
    SENSITIVE_KEYS,
    Config,
    RuntimeSettingSpec,
)
from ...storage import RuntimeSettings
from ...utils import logger
from .auth import is_admin

# 用 user_data 存而不是全局，免得多个 admin 串
_EDIT_PENDING_KEY = "__setting_edit_pending"
_EDIT_TTL_SECONDS = 600
_GROUP_LABELS = dict(RUNTIME_SETTING_GROUPS)
_EX_COOKIE_KEYS = {
    "ipb_member_id": "collectors.exhentai.ipb_member_id",
    "ipb_pass_hash": "collectors.exhentai.ipb_pass_hash",
    "igneous": "collectors.exhentai.igneous",
}
_PIXIV_COOKIE_KEY = "pixiv.phpsessid"
_COOKIE_TARGETS = {"ex": "ExHentai", "px": "Pixiv"}
_COOKIE_PAIR_RE = re.compile(r"(?:^|[;\s])([A-Za-z_][\w.-]*)\s*(?:=|:|\t)\s*([^;\s]+)", re.MULTILINE)


def _clean_cookie_value(value: Any) -> str:
    return str(value).strip().strip("\"'")


def parse_cookie_pairs(text: str) -> dict[str, str]:
    """从 Cookie 请求头、扩展导出的 JSON 或 DevTools 表格文本中提取 name/value。"""
    raw = text.strip()
    pairs: dict[str, str] = {}
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        data = data["cookies"]
    if isinstance(data, dict):
        for name, value in data.items():
            if isinstance(value, (str, int)):
                pairs.setdefault(str(name).strip().lower(), _clean_cookie_value(value))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "name" in item and "value" in item:
                pairs.setdefault(str(item["name"]).strip().lower(), _clean_cookie_value(item["value"]))
    if pairs:
        return pairs

    normalized = re.sub(r"(?im)^\s*cookie\s*:\s*", "", raw)
    for name, value in _COOKIE_PAIR_RE.findall(normalized):
        pairs.setdefault(name.lower(), _clean_cookie_value(value))
    return pairs


def parse_exhentai_cookie(text: str) -> dict[str, str]:
    """返回 runtime key -> value；至少识别到一项，否则抛 ValueError。"""
    pairs = parse_cookie_pairs(text)
    values = {key: pairs[name] for name, key in _EX_COOKIE_KEYS.items() if pairs.get(name)}
    if not values:
        raise ValueError("没有识别到 ipb_member_id / ipb_pass_hash / igneous")
    member_id = values.get("collectors.exhentai.ipb_member_id")
    if member_id is not None and not member_id.isdigit():
        raise ValueError("ipb_member_id 应该是纯数字")
    return values


def parse_pixiv_cookie(text: str) -> dict[str, str]:
    """支持整段 Cookie、JSON，或直接发送 PHPSESSID 的值。"""
    pairs = parse_cookie_pairs(text)
    value = pairs.get("phpsessid")
    raw = text.strip()
    if not value and raw and not re.search(r"[\s;=:]", raw):
        value = _clean_cookie_value(raw)
    if not value:
        raise ValueError("没有识别到 PHPSESSID")
    return {_PIXIV_COOKIE_KEY: value}


def _mask(key: str, value: Any) -> str:
    if value is None or value == "":
        return "(未设置)"
    if key in SENSITIVE_KEYS:
        text = str(value)
        return "***" if len(text) <= 6 else f"{text[:3]}***{text[-3:]}"
    return repr(value)


def _short(text: str, limit: int = 40) -> str:
    text = text.replace("\n", "⏎")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _display(spec: RuntimeSettingSpec, value: Any) -> str:
    if spec.kind == "secret":
        return "未设置" if not value else f"已设置（{_mask(spec.key, value)}）"
    if spec.kind == "bool":
        return "启用" if value else "关闭"
    if spec.choices:
        for choice, label in spec.choices:
            if str(value).lower() == choice.lower():
                return label
    if spec.kind in ("text", "template"):
        return "(空)" if value == "" else _short(str(value))
    return str(value)


def _is_current(value: Any, choice: str) -> bool:
    if isinstance(value, bool):
        return value == (choice.lower() in ("true", "1", "yes", "on"))
    return str(value).lower() == choice.lower()


def _is_overridden(runtime: RuntimeSettings, key: str) -> bool:
    return runtime.get(key) is not None


def _ex_cookie_status(config: Config) -> str:
    cfg = config.collectors.exhentai
    names = {"ipb_member_id": cfg.ipb_member_id, "ipb_pass_hash": cfg.ipb_pass_hash, "igneous": cfg.igneous}
    missing = [name for name, value in names.items() if not value]
    if len(missing) == len(names):
        return "未设置"
    if missing:
        return f"缺少 {', '.join(missing)}"
    if cfg.igneous.lower() == "mystery":
        return "igneous=mystery（账号可能没有 ExHentai 权限）"
    return "三项已设置"


def _find_spec(token: str) -> RuntimeSettingSpec | None:
    return RUNTIME_SETTINGS_BY_KEY.get(token) or RUNTIME_SETTINGS_BY_SID.get(token)


def _rows(buttons: list[InlineKeyboardButton], width: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[index : index + width] for index in range(0, len(buttons), width)]


_CLOSE_BUTTON = InlineKeyboardButton("✖️ 关闭", callback_data="stg:x")
_HOME_BUTTON = InlineKeyboardButton("🏠 主菜单", callback_data="stg:m")


def render_main_menu(config: Config, runtime: RuntimeSettings) -> tuple[str, InlineKeyboardMarkup]:
    overridden = set(runtime.all()) & RUNTIME_KEYS
    pixiv_status = "已设置" if config.pixiv.phpsessid else "未设置"
    text = (
        "⚙️ 运行时设置\n"
        "点击分类查看和修改，保存后立即生效；★ 表示分类中有运行时覆盖。\n\n"
        f"Pixiv Cookie：{pixiv_status}\n"
        f"ExHentai Cookie：{_ex_cookie_status(config)}"
    )
    rows = [
        [InlineKeyboardButton("🔑 替换 ExHentai Cookie", callback_data="stg:ck:ex")],
        [InlineKeyboardButton("🔑 替换 Pixiv Cookie", callback_data="stg:ck:px")],
    ]
    group_buttons = []
    for group, label in RUNTIME_SETTING_GROUPS:
        has_override = any(spec.group == group and spec.key in overridden for spec in RUNTIME_SETTINGS)
        group_buttons.append(InlineKeyboardButton(f"{label}{' ★' if has_override else ''}", callback_data=f"stg:g:{group}"))
    rows.extend(_rows(group_buttons))
    rows.append([_CLOSE_BUTTON])
    return text, InlineKeyboardMarkup(rows)


def render_group(group: str, config: Config, runtime: RuntimeSettings) -> tuple[str, InlineKeyboardMarkup]:
    specs = [spec for spec in RUNTIME_SETTINGS if spec.group == group]
    lines = [f"⚙️ {_GROUP_LABELS.get(group, group)}", ""]
    buttons = []
    for spec in specs:
        marker = " ★" if _is_overridden(runtime, spec.key) else ""
        lines.append(f"• {spec.label}：{_display(spec, config.get_field(spec.key))}{marker}")
        buttons.append(InlineKeyboardButton(f"{spec.label}{marker}", callback_data=f"stg:v:{spec.sid}"))
    rows = []
    if group == "ex":
        rows.append([InlineKeyboardButton("🔑 一次替换 ExHentai Cookie", callback_data="stg:ck:ex")])
    rows.extend(_rows(buttons))
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="stg:m"), _CLOSE_BUTTON])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _item_text(spec: RuntimeSettingSpec, config: Config, runtime: RuntimeSettings) -> str:
    value = config.get_field(spec.key)
    overridden = _is_overridden(runtime, spec.key)
    lines = [
        f"{'🔐' if spec.kind == 'secret' else '⚙️'} {spec.label}",
        f"key：{spec.key}",
        f"当前：{_display(spec, value)}",
        f"来源：{'运行时覆盖 ★' if overridden else 'yaml/默认值'}",
    ]
    if overridden:
        lines.append(f"恢复后：{_display(spec, config.runtime_baseline(spec.key))}")
    if spec.minimum is not None and spec.maximum is not None:
        lines.append(f"范围：{spec.minimum} – {spec.maximum}")
    if spec.description:
        lines.append(f"说明：{spec.description}")
    if spec.kind in ("text", "template") and value:
        full = str(value)
        if len(full) > 1500:
            full = full[:1500] + "\n…（已截断）"
        lines.extend(["", "完整内容：", full])
    return "\n".join(lines)


def _number_steps(spec: RuntimeSettingSpec) -> tuple[int, ...]:
    if spec.minimum is None or spec.maximum is None:
        return (-1, 1)
    return (-10, -1, 1, 10) if spec.maximum - spec.minimum >= 100 else (-1, 1)


def render_item(
    spec: RuntimeSettingSpec, config: Config, runtime: RuntimeSettings, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    value = config.get_field(spec.key)
    rows: list[list[InlineKeyboardButton]] = []
    if spec.choices:
        choice_buttons = [
            InlineKeyboardButton(
                f"● {label}" if _is_current(value, choice) else label,
                callback_data=f"stg:s:{spec.sid}:{index}",
            )
            for index, (choice, label) in enumerate(spec.choices)
        ]
        rows.extend(_rows(choice_buttons))
    elif spec.kind == "int":
        rows.append([
            InlineKeyboardButton(f"{step:+d}", callback_data=f"stg:n:{spec.sid}:{step}")
            for step in _number_steps(spec)
        ])
        rows.append([InlineKeyboardButton("✏️ 输入数值", callback_data=f"stg:e:{spec.sid}")])
    elif spec.kind == "secret":
        if spec.key in _EX_COOKIE_KEYS.values():
            rows.append([InlineKeyboardButton("🔑 一次替换三项 Cookie", callback_data="stg:ck:ex")])
        label = "🔑 粘贴 Pixiv Cookie" if spec.key == _PIXIV_COOKIE_KEY else "🔁 替换这一项"
        callback = "stg:ck:px" if spec.key == _PIXIV_COOKIE_KEY else f"stg:e:{spec.sid}"
        rows.append([InlineKeyboardButton(label, callback_data=callback)])
    else:
        edit_row = [InlineKeyboardButton("✏️ 编辑", callback_data=f"stg:e:{spec.sid}")]
        if value:
            edit_row.append(InlineKeyboardButton("🧹 清空", callback_data=f"stg:z:{spec.sid}"))
        rows.append(edit_row)
    if _is_overridden(runtime, spec.key):
        rows.append([InlineKeyboardButton("↩️ 恢复 yaml/默认值", callback_data=f"stg:r:{spec.sid}")])
    rows.append([
        InlineKeyboardButton("⬅️ 返回", callback_data=f"stg:g:{spec.group}"),
        _HOME_BUTTON,
        _CLOSE_BUTTON,
    ])
    text = _item_text(spec, config, runtime)
    if notice:
        text = f"{notice}\n\n{text}"
    return text, InlineKeyboardMarkup(rows)


_CANCEL_MARKUP = InlineKeyboardMarkup([[InlineKeyboardButton("取消", callback_data="stg:c")]])


def render_edit_prompt(spec: RuntimeSettingSpec) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"✏️ 请发送「{spec.label}」的新值", f"key：{spec.key}"]
    if spec.kind == "int" and spec.minimum is not None:
        lines.append(f"范围：{spec.minimum} – {spec.maximum}")
    elif spec.kind == "template":
        lines.append("支持多行文本和模板占位符。")
    elif spec.kind == "secret":
        lines.append("可发送单独的值，也可粘贴包含该项的整段 Cookie。保存后会尝试删除你的消息。")
    lines.append("10 分钟内有效。")
    return "\n".join(lines), _CANCEL_MARKUP


def render_cookie_prompt(target: str) -> tuple[str, InlineKeyboardMarkup]:
    if target == "ex":
        text = (
            "🔑 替换 ExHentai Cookie\n"
            "请直接粘贴 exhentai.org 的 Cookie，会自动识别 ipb_member_id、ipb_pass_hash、igneous。\n\n"
            "支持：\n"
            "• 请求头中的整段 Cookie\n"
            "• Cookie-Editor 等扩展导出的 JSON\n"
            "• 开发者工具 Cookie 表格复制的多行内容\n\n"
            "保存后会尝试删除你发送的消息。10 分钟内有效。"
        )
    else:
        text = (
            "🔑 替换 Pixiv Cookie\n"
            "请粘贴 pixiv.net 的整段 Cookie，或直接发送 PHPSESSID 的值。\n"
            "保存后会尝试删除你发送的消息。10 分钟内有效。"
        )
    return text, _CANCEL_MARKUP


def _set_pending(
    context: ContextTypes.DEFAULT_TYPE, kind: str, target: str, chat_id: int | None, message_id: int | None
) -> None:
    context.user_data[_EDIT_PENDING_KEY] = {
        "kind": kind,
        "target": target,
        "chat_id": chat_id,
        "message_id": message_id,
        "created_at": time.monotonic(),
    }


def _get_pending(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    pending = context.user_data.get(_EDIT_PENDING_KEY)
    if isinstance(pending, str):
        spec = RUNTIME_SETTINGS_BY_KEY.get(pending)
        pending = {"kind": "value", "target": spec.sid if spec else pending, "created_at": time.monotonic()}
    if not isinstance(pending, dict):
        return None
    if time.monotonic() - float(pending.get("created_at", 0)) > _EDIT_TTL_SECONDS:
        context.user_data.pop(_EDIT_PENDING_KEY, None)
        return None
    return pending


async def _reply(update: Update, rendered: tuple[str, InlineKeyboardMarkup | None]):
    text, markup = rendered
    return await update.effective_message.reply_text(text, reply_markup=markup)


async def _send_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, target: str,
    rendered: tuple[str, InlineKeyboardMarkup],
) -> None:
    message = await _reply(update, rendered)
    _set_pending(context, kind, target, message.chat_id, message.message_id)


def _admin_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == "private" and is_admin(update, context.bot_data["allowlist"])


async def cmd_setting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    runtime: RuntimeSettings = context.bot_data["runtime_settings"]
    if update.effective_user is None or update.effective_chat is None:
        return
    if not is_admin(update, context.bot_data["allowlist"]):
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ 为安全起见，/setting 仅限私聊使用")
        return

    args = context.args or []
    if not args:
        await _reply(update, render_main_menu(config, runtime))
        return

    sub = args[0].lower()
    if sub == "help":
        await _send_help(update)
    elif sub == "list":
        await _list_settings(update, config, runtime, args[1] if len(args) >= 2 else "")
    elif sub == "get":
        await _with_spec(update, args, lambda spec: _reply(update, render_item(spec, config, runtime)))
    elif sub == "set":
        if len(args) < 3:
            await update.message.reply_text("用法：/setting set <key> <value>\n多行/含空格的值请用 /setting edit <key>")
            return
        spec = _find_spec(args[1])
        if spec is None:
            await update.message.reply_text(f"⚠️ {args[1]!r} 不在可改配置列表中（/setting list 查看）")
            return
        await _set_from_command(update, context, spec, " ".join(args[2:]))
    elif sub == "edit":
        await _with_spec(update, args, lambda spec: _start_value_edit(update, context, spec))
    elif sub == "unset":
        await _with_spec(update, args, lambda spec: _unset_from_command(update, config, runtime, spec))
    elif sub == "cookie":
        target = args[1].lower() if len(args) >= 2 else ""
        target = {"ex": "ex", "exhentai": "ex", "eh": "ex", "px": "px", "pixiv": "px"}.get(target, "")
        if not target:
            await _reply(update, render_main_menu(config, runtime))
            return
        await _send_prompt(update, context, "cookie", target, render_cookie_prompt(target))
    elif sub == "cancel":
        await cmd_cancel(update, context)
    else:
        await _search(update, config, runtime, " ".join(args))


async def _with_spec(update: Update, args: list[str], action) -> None:
    if len(args) < 2:
        await update.message.reply_text(f"用法：/setting {args[0].lower()} <key>")
        return
    spec = _find_spec(args[1])
    if spec is None:
        await update.message.reply_text(f"⚠️ {args[1]!r} 不在可改配置列表中（/setting list 查看）")
        return
    await action(spec)


async def _send_help(update: Update) -> None:
    await update.message.reply_text(
        "/setting                    打开按钮设置面板（推荐）\n"
        "/setting <关键词>           搜索设置项，例如 /setting 超时\n"
        "/setting cookie ex          粘贴整段 ExHentai Cookie\n"
        "/setting cookie pixiv       粘贴整段 Pixiv Cookie\n"
        "/setting list [prefix]      列出可改配置\n"
        "/setting get <key>          查看单项（附按钮）\n"
        "/setting set <key> <val>    设置单行值\n"
        "/setting edit <key>         下一条消息作为新值\n"
        "/setting unset <key>        恢复 yaml/默认值（立即生效）\n"
        "/cancel                     取消正在进行的设置输入"
    )


async def _list_settings(update: Update, config: Config, runtime: RuntimeSettings, prefix: str) -> None:
    specs = [spec for spec in RUNTIME_SETTINGS if spec.key.startswith(prefix)] if prefix else list(RUNTIME_SETTINGS)
    if not specs:
        await update.message.reply_text(f"没有匹配前缀 {prefix!r} 的可改配置")
        return
    lines: list[str] = []
    current_group = None
    for spec in specs:
        if spec.group != current_group:
            lines.append(f"\n[{_GROUP_LABELS.get(spec.group, spec.group)}]")
            current_group = spec.group
        marker = "★" if _is_overridden(runtime, spec.key) else " "
        lines.append(f"  {marker} {spec.key} = {_short(_display(spec, config.get_field(spec.key)), 80)}")
    lines.append("\n（★ 表示该项有 runtime 覆盖；发送 /setting 可用按钮修改）")
    chunk = ""
    for line in "\n".join(lines).strip().splitlines(keepends=True):
        if len(chunk) + len(line) > 4000:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += line
    if chunk:
        await update.message.reply_text(chunk)


async def _search(update: Update, config: Config, runtime: RuntimeSettings, query: str) -> None:
    needle = query.strip().lower()
    matches = [
        spec
        for spec in RUNTIME_SETTINGS
        if needle in " ".join(
            (spec.sid, spec.key, spec.label, spec.description, _GROUP_LABELS.get(spec.group, ""))
        ).lower()
    ]
    if not matches:
        await update.message.reply_text(f"没有找到与 {query!r} 相关的设置。发送 /setting 打开设置面板。")
        return
    if len(matches) == 1:
        await _reply(update, render_item(matches[0], config, runtime))
        return
    buttons = [
        InlineKeyboardButton(f"{_GROUP_LABELS.get(spec.group, spec.group)} · {spec.label}", callback_data=f"stg:v:{spec.sid}")
        for spec in matches[:20]
    ]
    suffix = "（仅显示前 20 项）" if len(matches) > 20 else ""
    await update.message.reply_text(
        f"找到 {len(matches)} 项{suffix}，点击查看：",
        reply_markup=InlineKeyboardMarkup([*_rows(buttons, 1), [_HOME_BUTTON, _CLOSE_BUTTON]]),
    )


async def _delete_user_message(update: Update) -> bool:
    try:
        await update.effective_message.delete()
        return True
    except Exception as e:
        logger.warning(f"failed to delete sensitive setting message: {type(e).__name__}")
        return False


def _deleted_notice(deleted: bool) -> str:
    return "" if deleted else "\n⚠️ 无法自动删除你发送的消息，请手动删除其中的 Cookie。"


async def _set_from_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, spec: RuntimeSettingSpec, value: str
) -> None:
    config: Config = context.bot_data["config"]
    runtime: RuntimeSettings = context.bot_data["runtime_settings"]
    deleted = await _delete_user_message(update) if spec.kind == "secret" else True
    try:
        await config.set_runtime(spec.key, value, updated_by=update.effective_user.id)
    except ValueError as e:
        await update.effective_chat.send_message(f"⚠️ 值无效：{e}{_deleted_notice(deleted)}")
        return
    except Exception as e:
        logger.exception(f"setting set {spec.key} failed")
        await update.effective_chat.send_message(f"⚠️ 写入失败：{type(e).__name__}{_deleted_notice(deleted)}")
        return
    text, markup = render_item(spec, config, runtime, "✓ 已保存并即时生效" + _deleted_notice(deleted))
    await update.effective_chat.send_message(text, reply_markup=markup)


async def _start_value_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, spec: RuntimeSettingSpec) -> None:
    if spec.key == _PIXIV_COOKIE_KEY:
        await _send_prompt(update, context, "cookie", "px", render_cookie_prompt("px"))
        return
    await _send_prompt(update, context, "value", spec.sid, render_edit_prompt(spec))


async def _unset_from_command(
    update: Update, config: Config, runtime: RuntimeSettings, spec: RuntimeSettingSpec
) -> None:
    try:
        removed = await config.unset_runtime(spec.key)
    except Exception as e:
        logger.exception(f"setting unset {spec.key} failed")
        await update.message.reply_text(f"⚠️ 删除失败：{type(e).__name__}")
        return
    notice = "✓ 已恢复 yaml/默认值并即时生效" if removed else "（该项没有 runtime 覆盖）"
    await _reply(update, render_item(spec, config, runtime, notice))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin_private(update, context):
        return
    if context.user_data.pop(_EDIT_PENDING_KEY, None) is None:
        await update.message.reply_text("当前没有正在进行的设置输入")
        return
    await update.message.reply_text("已取消设置输入")


def _secret_value(spec: RuntimeSettingSpec, text: str) -> str:
    name = spec.key.rsplit(".", 1)[-1].lower()
    value = parse_cookie_pairs(text).get(name) or text.strip()
    if spec.key == "collectors.exhentai.ipb_member_id" and not value.isdigit():
        raise ValueError("ipb_member_id 应该是纯数字")
    return value


def _cookie_notice(target: str, values: dict[str, str], config: Config) -> str:
    if target == "px":
        return "✓ 已更新 Pixiv Cookie 并即时生效"
    names = [name for name, key in _EX_COOKIE_KEYS.items() if key in values]
    lines = [f"✓ 已更新 ExHentai Cookie：{', '.join(names)}", f"当前状态：{_ex_cookie_status(config)}"]
    if not config.collectors.exhentai.enabled:
        lines.append("提示：ExHentai 当前未启用，可在「ExHentai」分类中开启。")
    return "\n".join(lines)


async def _show_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: dict[str, Any],
    rendered: tuple[str, InlineKeyboardMarkup],
) -> None:
    text, markup = rendered
    chat_id = pending.get("chat_id")
    message_id = pending.get("message_id")
    if chat_id == update.effective_chat.id and message_id:
        try:
            await context.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
            return
        except Exception as e:
            logger.debug(f"setting prompt edit failed, sending a new panel: {type(e).__name__}")
    await update.effective_chat.send_message(text, reply_markup=markup)


async def handle_setting_edit_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """检查当前用户是否在设置输入流程中，是则消费这条消息并返回 True。"""
    if update.effective_chat is None or update.effective_chat.type != "private":
        return False
    pending = _get_pending(context)
    if pending is None:
        return False
    if not is_admin(update, context.bot_data["allowlist"]):
        context.user_data.pop(_EDIT_PENDING_KEY, None)
        return False

    text = update.effective_message.text or ""
    if text.strip() == "/cancel":
        context.user_data.pop(_EDIT_PENDING_KEY, None)
        await update.message.reply_text("已取消设置输入")
        return True

    config: Config = context.bot_data["config"]
    runtime: RuntimeSettings = context.bot_data["runtime_settings"]
    kind = pending.get("kind")
    target = str(pending.get("target", ""))
    spec = RUNTIME_SETTINGS_BY_SID.get(target) if kind == "value" else None
    if kind == "value" and spec is None:
        context.user_data.pop(_EDIT_PENDING_KEY, None)
        await update.message.reply_text("⚠️ 要编辑的设置项已不存在，请重新打开 /setting")
        return True
    sensitive = kind == "cookie" or (spec is not None and spec.kind == "secret")
    deleted = await _delete_user_message(update) if sensitive else True

    try:
        if kind == "cookie":
            values = parse_exhentai_cookie(text) if target == "ex" else parse_pixiv_cookie(text)
        else:
            values = {spec.key: _secret_value(spec, text) if sensitive else text}
        if len(values) == 1:
            key, value = next(iter(values.items()))
            await config.set_runtime(key, value, updated_by=update.effective_user.id)
        else:
            await config.set_runtime_many(values, updated_by=update.effective_user.id)
    except ValueError as e:
        await update.effective_chat.send_message(
            f"⚠️ {e}。请重新发送，或发送 /cancel 取消。{_deleted_notice(deleted)}",
            reply_markup=_CANCEL_MARKUP,
        )
        return True
    except Exception as e:
        context.user_data.pop(_EDIT_PENDING_KEY, None)
        logger.exception(f"setting edit {target} failed")
        await update.effective_chat.send_message(f"⚠️ 写入失败：{type(e).__name__}{_deleted_notice(deleted)}")
        return True

    context.user_data.pop(_EDIT_PENDING_KEY, None)
    if kind == "cookie":
        text, markup = render_main_menu(config, runtime)
        rendered = (f"{_cookie_notice(target, values, config)}{_deleted_notice(deleted)}\n\n{text}", markup)
    else:
        rendered = render_item(spec, config, runtime, "✓ 已保存并即时生效" + _deleted_notice(deleted))
    await _show_result(update, context, pending, rendered)
    return True


async def _edit_panel(query, rendered: tuple[str, InlineKeyboardMarkup | None]) -> None:
    text, markup = rendered
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def _callback_set(query, config: Config, runtime: RuntimeSettings, spec: RuntimeSettingSpec, value: str) -> None:
    if _is_current(config.get_field(spec.key), value):
        await query.answer("已是该值")
        await _edit_panel(query, render_item(spec, config, runtime))
        return
    try:
        await config.set_runtime(spec.key, value, updated_by=query.from_user.id)
    except ValueError as e:
        await query.answer(f"⚠️ 值无效：{e}", show_alert=True)
        return
    except Exception as e:
        logger.exception(f"setting button {spec.key}={value!r} failed")
        await query.answer(f"⚠️ 写入失败：{type(e).__name__}", show_alert=True)
        return
    await query.answer("✓ 已保存")
    await _edit_panel(query, render_item(spec, config, runtime, "✓ 已保存并即时生效"))


def _legacy_callback(payload: str) -> tuple[RuntimeSettingSpec, str] | None:
    key, _, value = payload.rpartition(":")
    spec = RUNTIME_SETTINGS_BY_KEY.get(key)
    if spec is None or not spec.choices:
        return None
    return spec, value


async def handle_setting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """处理 stg:* 设置面板按钮。返回 True 表示已消费。"""
    query = update.callback_query
    if query is None or not query.data or not query.data.startswith("stg:"):
        return False
    config: Config = context.bot_data["config"]
    runtime: RuntimeSettings = context.bot_data["runtime_settings"]
    user = query.from_user
    if user is None or user.id not in set(config.auth.admin_users):
        await query.answer("⚠️ 仅 admin 可修改配置", show_alert=True)
        return True
    if query.message is None or query.message.chat.type != "private":
        await query.answer("⚠️ 设置面板仅限私聊使用", show_alert=True)
        return True

    payload = query.data[4:]
    action, _, rest = payload.partition(":")
    if "." in action:
        legacy = _legacy_callback(payload)
        if legacy is None:
            await query.answer("⚠️ 按钮已过期，请重新发送 /setting", show_alert=True)
        else:
            await _callback_set(query, config, runtime, *legacy)
        return True

    try:
        await _dispatch_callback(query, context, config, runtime, action, rest)
    except Exception as e:
        logger.exception(f"setting callback {query.data!r} failed")
        await query.answer(f"⚠️ 操作失败：{type(e).__name__}", show_alert=True)
    return True


async def _dispatch_callback(
    query, context: ContextTypes.DEFAULT_TYPE, config: Config, runtime: RuntimeSettings, action: str, rest: str
) -> None:
    if action == "m":
        await query.answer()
        await _edit_panel(query, render_main_menu(config, runtime))
        return
    if action == "x":
        pending = _get_pending(context)
        if pending and pending.get("message_id") == query.message.message_id:
            context.user_data.pop(_EDIT_PENDING_KEY, None)
        await query.answer()
        await _edit_panel(query, ("设置面板已关闭。发送 /setting 可重新打开。", None))
        return
    if action == "g":
        if rest not in _GROUP_LABELS:
            await query.answer("⚠️ 分类不存在", show_alert=True)
            return
        await query.answer()
        await _edit_panel(query, render_group(rest, config, runtime))
        return
    if action == "ck":
        if rest not in _COOKIE_TARGETS:
            await query.answer("⚠️ 未知 Cookie 类型", show_alert=True)
            return
        _set_pending(context, "cookie", rest, query.message.chat_id, query.message.message_id)
        await query.answer(f"请粘贴 {_COOKIE_TARGETS[rest]} Cookie")
        await _edit_panel(query, render_cookie_prompt(rest))
        return
    if action == "c":
        pending = _get_pending(context)
        context.user_data.pop(_EDIT_PENDING_KEY, None)
        await query.answer("已取消")
        spec = RUNTIME_SETTINGS_BY_SID.get(str(pending.get("target"))) if pending else None
        if pending and pending.get("kind") == "value" and spec is not None:
            await _edit_panel(query, render_item(spec, config, runtime))
        else:
            await _edit_panel(query, render_main_menu(config, runtime))
        return
    await _dispatch_item_callback(query, context, config, runtime, action, rest)


async def _dispatch_item_callback(
    query, context: ContextTypes.DEFAULT_TYPE, config: Config, runtime: RuntimeSettings, action: str, rest: str
) -> None:
    sid, _, arg = rest.partition(":")
    spec = RUNTIME_SETTINGS_BY_SID.get(sid)
    if spec is None:
        await query.answer("⚠️ 按钮已过期，请重新发送 /setting", show_alert=True)
        return

    if action == "v":
        await query.answer()
        await _edit_panel(query, render_item(spec, config, runtime))
    elif action == "s":
        try:
            value = spec.choices[int(arg)][0]
        except (ValueError, IndexError):
            await query.answer("⚠️ 选项已过期", show_alert=True)
            return
        await _callback_set(query, config, runtime, spec, value)
    elif action == "n":
        if spec.kind != "int" or arg.lstrip("+-") not in {"1", "10"}:
            await query.answer("⚠️ 该项不支持快捷调整", show_alert=True)
            return
        current = int(config.get_field(spec.key))
        target = current + int(arg)
        if spec.minimum is not None:
            target = max(spec.minimum, target)
        if spec.maximum is not None:
            target = min(spec.maximum, target)
        if target == current:
            await query.answer("已到范围边界")
            return
        await _callback_set(query, config, runtime, spec, str(target))
    elif action == "e":
        if spec.key == _PIXIV_COOKIE_KEY:
            _set_pending(context, "cookie", "px", query.message.chat_id, query.message.message_id)
            await query.answer()
            await _edit_panel(query, render_cookie_prompt("px"))
            return
        _set_pending(context, "value", spec.sid, query.message.chat_id, query.message.message_id)
        await query.answer("请发送新值")
        await _edit_panel(query, render_edit_prompt(spec))
    elif action == "z":
        if spec.kind not in ("text", "template"):
            await query.answer("⚠️ 该项不支持清空", show_alert=True)
            return
        await _callback_set(query, config, runtime, spec, "")
    elif action == "r":
        removed = await config.unset_runtime(spec.key)
        await query.answer("✓ 已恢复" if removed else "该项没有运行时覆盖")
        notice = "✓ 已恢复 yaml/默认值并即时生效" if removed else ""
        await _edit_panel(query, render_item(spec, config, runtime, notice))
    else:
        await query.answer("⚠️ 未知操作", show_alert=True)


__all__ = [
    "cmd_cancel",
    "cmd_setting",
    "handle_setting_callback",
    "handle_setting_edit_followup",
    "parse_cookie_pairs",
    "parse_exhentai_cookie",
    "parse_pixiv_cookie",
]
