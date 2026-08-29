# Guest Mode 部署端排查提示词

将下面整段提示词粘贴给部署服务器上的 Agent。排查时请只读日志和配置；修改服务前先给出证据、方案及回滚方式。

```text
请排查 shengxiu_feedbot 的 Telegram Guest Mode 问题，重点查看 2026-08-29 17:16:03（服务器本地时区）前后至少 ±3 分钟的完整日志。目标是解释：

1. 为什么“回复一条已有消息后只发送 @bot”仍然会被 Bot 响应；
2. Guest 详情卡点击“取消”后为什么不能真正删除 Bot 的响应；
3. 当前部署是否能提供或扩展自定义 delete_guest_message 能力。

第一步：确认时间和日志范围

- 先执行 `date -Is`、`timedatectl`（若存在），确认服务器时区；明确日志中的 17:16:03 对应的绝对时间。
- 查看：
  - `journalctl -u pixiv-feed-bot --since "2026-08-29 17:13:00" --until "2026-08-29 17:19:00" -o short-iso --no-pager`
  - 若服务名不同，先用 `systemctl list-units --type=service | grep -Ei 'pixiv|feed|bot'` 找到真实 unit。
  - 同时检查应用日志文件（若配置了），搜索 `guest|reference_messages|callback|cancel|answerGuestQuery|inline_message_id|delete_guest`。
- 不要只截取错误行；请保留同一请求从 Guest update、详情卡、callback 到最终 edit/delete 的连续日志，并记录日志文件路径、时间戳、request/update/query/token（敏感 token 打码）。

第二步：判断“回复消息仍被响应”的真实字段

- 在 PTB 22.8 的运行环境中，用只读 Python 检查 `Update.de_json` 后 Guest 消息字段：
  `guest_message.reply_to_message`、`guest_message.external_reply`、`guest_message.quote`、`guest_message.api_kwargs`。
- 特别确认 `guest_message.api_kwargs.get("reference_messages")` 的实际结构；打印键名、消息数量、每条消息的 message_id、text/caption 摘要（脱敏），不要打印完整链接中的 cookie/token。
- 结合 17:16:03 的原始 update（如果日志没有 update JSON，见第三步抓取）确认触发链接究竟位于：
  1) 当前召唤消息 text/caption；
  2) reply_to_message；
  3) external_reply/quote；
  4) api_kwargs.reference_messages；
  5) 其它 Bot API/本地 Bot API 扩展字段。
- 这次要验证的规则不是“所有回复都拒绝”，而是：当前消息或被回复的用户消息含有支持链接，
  且当前消息显式 `@bot` 时，应当作为新任务；当前消息和引用消息都没有支持链接（例如只是
  “这个图片好棒”）时必须静默忽略。引用的是 Bot 自己的结果消息时，不应重新提取其中的链接。
  若合计提取到多个作品，应先显示 Guest 链接选择按钮。若线上行为不符，记录上述字段和
  `text/caption`，排查回复关系是否落在其它字段、旧进程、重复 handler 或其它 update 类型。
- 检查当前代码实际运行版本：`git rev-parse HEAD`、`git log -3 --oneline`、`python -c "import telegram; print(telegram.__version__)"`。确认已经包含提交 `08bf5d8`，并检查 systemd 的 WorkingDirectory/ExecStart 是否指向该 checkout，而不是旧副本。
- 如果链接只存在于 reference_messages，而当前逻辑仍提取到了它，请指出是哪一层（PTB 反序列化、registry.extract_all_refs、其它 handler 或旧进程）重新合并了引用消息。

第三步：如果现有日志不足，临时抓取下一次 Guest update（不要记录 Bot Token）

- 先备份当前 unit 和配置，不要覆盖生产配置：
  `systemctl cat pixiv-feed-bot > /tmp/pixiv-feed-bot.unit.$(date +%s).txt`
- 临时把 Guest 调试日志加到独立文件或 journald，至少记录以下结构化字段：
  - update_id；guest_message.message_id/date/chat.id/chat.type/from_user.id；
  - text/caption（链接域名和作品 ID 可保留，query 参数、cookie、token 必须脱敏）；
  - 是否存在 reply_to_message、external_reply、quote；
  - api_kwargs 的键名，以及 reference_messages 的数量和摘要；
  - answer_guest_query 返回的 inline_message_id；
  - callback_query.id/from_user.id/data/inline_message_id/message 是否存在；
  - 取消处理调用了哪些 Bot API 方法及返回/异常。
- 推荐使用一次性 middleware/handler wrapper 或在现有 Guest handler 入口临时加日志，复现一次后立即恢复日志级别并重启服务。不要把完整 Update JSON 写入长期日志。
- 复现时分别测试两种消息：
  A. 当前消息写“链接 @bot”；
  B. 回复含链接的消息，只写“@bot”。
  比较 A/B 的字段差异，验证 B 是否仍有链接被代码提取。

第四步：调查自定义 delete_guest_message

- 先不要假设标准 Telegram Bot API 支持它。检查当前 Python `telegram.Bot` 是否存在 `delete_guest_message`/`deleteGuestMessage`：
  `python -c "from telegram import Bot; print([x for x in dir(Bot) if 'guest' in x.lower() or 'delete' in x.lower()])"`
- 检查本地 Bot API 服务版本和实现：
  - `curl -sS http://127.0.0.1:<BOT_API_PORT>/getMe`（不要把 token 写入输出）；
  - 查看本地 Bot API 的镜像/二进制版本、启动参数、源码或 patch；
  - 搜索 `guest`、`setBotGuestChatResult`、`inline_message_id`、`deleteGuest`、`deleteEphemeralMessage`。
- 验证实际接口能力时使用测试 Bot/测试群，不要对生产消息发删除请求。若自定义接口存在，记录：HTTP 方法、路径、参数格式、是否需要 chat_id/receiver_user_id/message_id、是否接受 inline_message_id、返回 JSON 和错误码。
- 如果只能通过 Telegram MTProto 层删除，确认它需要的 InputBotInlineMessageID/数据中心字段，以及当前 Bot API 服务是否暴露了安全封装；不要直接改线上二进制。
- 若没有可用删除接口，请明确结论：标准 Guest inline 消息只有 inline_message_id，无法用普通 deleteMessage 删除；当前应用只能延迟后清空按钮/隐藏内容。请给出最小可行自定义 API 设计（例如 deleteGuestInlineMessage(inline_message_id)）及实现位置，并说明 Telegram 客户端/服务器是否真正接受该操作。

最终请输出：

1. 17:16:03 前后关键日志时间线；
2. A/B 两次 Guest update 的字段对比；
3. 实际运行 commit、PTB 版本、Bot API 服务版本；
4. “回复仍被响应”的确定原因和最小修复建议；
5. delete_guest_message 是否存在、如何验证、若不存在的替代方案；
6. 在未获得确认前不要提交、重启生产服务或修改远端仓库。
```
