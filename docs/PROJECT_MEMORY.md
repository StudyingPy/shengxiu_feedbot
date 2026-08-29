# 项目记忆

- Guest Mode 的回调来自 inline 消息，`CallbackQuery.message` 可能为 `None`；必须保存召唤时的 `Update` 与 `GuestReply`，并通过 `inline_message_id` 编辑。
- Guest API 只能编辑一条 inline 回复，不能像普通 Bot 一样上传本地 ZIP；但 EH/EX 归档模式仍可在后台下载并解包 archive，再把图片发布到同一条 Telegraph 回复。Pixiv 单图直发要使用缓存的公开 URL。
- 标准 Bot API 没有用 `inline_message_id` 删除 Guest inline 消息的方法；取消时只能短暂延迟后尝试兼容实现，失败则移除按钮并编辑为“已取消”。
- Telegram Guest Mode 的官方协议是“当前消息提及 Bot 后产生 Guest query”；`reference_messages` 是被回复/引用内容的额外上下文。应用应提取当前消息和用户引用消息中的链接，但忽略 Bot 自己结果消息里的链接：普通回复（如“这个图片好棒”）静默，回复用户的链接并 `@bot` 时作为新任务。若普通回复仍触发，应重点检查真实 update 是否缺少引用字段、是否走了其它 update/handler 或旧进程。官方 Bot API 目前仍没有 `deleteGuestMessage`；社区对 inline 消息的常见处理也是用 `editMessage*` 清空/移除按钮。`deleteEphemeralMessage` 属于另一套 ephemeral 消息模型，不能接收 `inline_message_id`。
- Guest Mode 从当前消息与用户引用消息中去重提取出多个作品链接时，先用 inline keyboard 让触发者选择一个，再进入对应作品原有详情流程；普通 Bot 消息监听仍按原逻辑逐个处理，不受影响。
- Guest EH 的归档按钮复用 `_eh_run_with_mode(mode=ARCHIVE_*)`：后台下载并解包 archive，最终仍通过 Telegraph inline 回复交付，队列类别为 `telegraph_publish`。另一个独立陷阱是 `PAGE_ORIGINAL` 在子页没有 `fullimg` 链接时会静默回退到 sample，但仍以 `page_original` 写入 cache，造成来源图与模式记录不一致；排查时要区分这条路径。
- Telegraph 完成态若要固定 Telegram 右侧小缩略图，不能只传 `disable_web_page_preview=False`；发送/编辑每个入口都要传 `LinkPreviewOptions(url=<页面 URL>, prefer_small_media=True)`。缓存命中、EH fallback、Pixiv、nhentai、zip2tph 与 Guest inline 路径需分别覆盖，否则同一功能会因入口不同恢复默认大预览。
