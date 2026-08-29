# 项目记忆

- Guest Mode 的回调来自 inline 消息，`CallbackQuery.message` 可能为 `None`；必须保存召唤时的 `Update` 与 `GuestReply`，并通过 `inline_message_id` 编辑。
- Guest API 只能编辑一条 inline 回复，不能像普通 Bot 一样上传本地 ZIP/图片。Pixiv 单图直发要使用缓存的公开 URL；多图与 EH/EX 归档按钮需回退到网页/Telegra.ph 路径。
