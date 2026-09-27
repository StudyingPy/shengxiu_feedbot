# 项目记忆

- Guest Mode 的回调来自 inline 消息，`CallbackQuery.message` 可能为 `None`；必须保存召唤时的 `Update` 与 `GuestReply`，并通过 `inline_message_id` 编辑。
- Guest API 只能编辑一条 inline 回复，不能像普通 Bot 一样上传本地 ZIP；但 EH/EX 归档模式仍可在后台下载并解包 archive，再把图片发布到同一条 Telegraph 回复。Pixiv 单图直发要使用缓存的公开 URL。
- 标准 Bot API 没有用 `inline_message_id` 删除 Guest inline 消息的方法；取消时只能短暂延迟后尝试兼容实现，失败则移除按钮并编辑为“已取消”。
- Telegram Guest Mode 的官方协议是“当前消息提及 Bot 后产生 Guest query”；`reference_messages` 是被回复/引用内容的额外上下文。应用应提取当前消息和用户引用消息中的链接，但忽略 Bot 自己结果消息里的链接：普通回复（如“这个图片好棒”）静默，回复用户的链接并 `@bot` 时作为新任务。若普通回复仍触发，应重点检查真实 update 是否缺少引用字段、是否走了其它 update/handler 或旧进程。官方 Bot API 目前仍没有 `deleteGuestMessage`；社区对 inline 消息的常见处理也是用 `editMessage*` 清空/移除按钮。`deleteEphemeralMessage` 属于另一套 ephemeral 消息模型，不能接收 `inline_message_id`。
- Guest Mode 从当前消息与用户引用消息中去重提取出多个作品链接时，先用 inline keyboard 让触发者选择一个，再进入对应作品原有详情流程；普通 Bot 消息监听仍按原逻辑逐个处理，不受影响。
- Guest EH 的归档按钮复用 `_eh_run_with_mode(mode=ARCHIVE_*)`：后台下载并解包 archive，最终仍通过 Telegraph inline 回复交付，但队列类别仍应使用 `archive_zip`（与私聊归档一致），网页显示图模式才使用 `telegraph_publish`。另一个独立陷阱是 `PAGE_ORIGINAL` 在子页没有 `fullimg` 链接时会静默回退到 sample，但仍以 `page_original` 写入 cache，造成来源图与模式记录不一致；排查时要区分这条路径。
- Guest Pixiv / nhentai 详情卡也要启动对应的 size prefetch；`_safe_update_card` 需像 `_safe_update_buttons` 一样用 `GuestReply` 对象身份校验，且回填时必须保留 Guest 专用键盘，不能套用私聊按钮。
- Telegraph 完成态若要固定 Telegram 右侧小缩略图，不能只传 `disable_web_page_preview=False`；发送/编辑每个入口都要传 `LinkPreviewOptions(url=<页面 URL>, prefer_small_media=True)`。缓存命中、EH fallback、Pixiv、nhentai、zip2tph 与 Guest inline 路径需分别覆盖，否则同一功能会因入口不同恢复默认大预览。
- `/zip2tph` 的本地 Bot API `local_mode` 不是“已配置即可”：PTB 会把 `getFile` 返回的绝对路径直接读入 bot 进程；telegram-bot-api 与 feed bot 分容器/分用户时，路径可见性和读权限必须单独验证。下载异常不能把 `str(e)` 原样回显（路径可能含敏感凭据），应按权限/路径不可见、Bot API 未配置、网络超时、损坏文件分别提示并对路径脱敏。
- `/zip2tph` 的空间峰值不是压缩包大小的 2 倍：至少要估算 `输入 zip + 解压目录 + cache_dir 公共副本`，并分别检查临时目录和 cache 所在挂载点；解压需限制条目数、解压后总字节、单文件大小并拒绝符号链接/重名覆盖，重型 copy/hash 不能阻塞 event loop。失败或取消时要回收已创建的 `cache_dir/zip_*`，并为下载、解压、发布失败补齐 `usage_log status=failed` 和测试覆盖。
- Docker 版 telegram-bot-api 常以容器 UID/GID 101 写宿主机文件；宿主机可能显示为 `sshd:crontab`，而 systemd feed bot 用 `pixivbot`。`local_mode` 直接读宿主路径时要按数字 UID/GID 或针对单个 token 目录的 ACL 修复，不能用全目录 `chmod o+rX`。
