# Changelog

## v0.4.2 — 2026-05-07

### 新增
- R-18/R-18G 图片在群聊直发时自动加 spoiler 遮罩（点击才展开）。私聊不加遮罩。([handlers.py:_send_pixiv_illust_direct](pixivfeed/channel/telegram/handlers.py))
- 用量统计体系：新增 `usage_log` 与 `users` 两张 SQLite 表，所有任务入口在结束时（成功/失败）写入一行记录：kind / provider / ref_id / gp_cost / bytes_in / bytes_out / status / chat_id / user_id / ts。写入失败全部吞掉，绝不影响主流程。
- `/stats` 管理员命令：
  - `/stats`                  —— 最近 24h 总览 + 前 10 用户排行
  - `/stats 7d`               —— 指定窗口（支持 1h/24h/7d/30d）
  - `/stats user @x` / `user 12345` —— 单用户详情（带类别拆分）
  - `/stats chat <chat_id>`   —— 单群组
  - `/stats system`           —— 缓存占用 + 磁盘剩余
- 用户显示信息：每次授权用户触发任何动作时静默 upsert `users` 表（user_id / first_name / last_name / username / last_seen），供 `/stats` 展示"昵称 + username + id"。
- e-hentai/exhentai 归档下载自动解析 chooser 页 "Download Cost: N GP"，写入 usage_log.gp_cost。Free! 与解析失败都记 0。`parse_gp_cost` 函数（[provider/ehentai/_archive.py](pixivfeed/provider/ehentai/_archive.py)）

### 改动文件
- `pixivfeed/storage/db.py`：新增 `users` 与 `usage_log` 两张表 + 索引
- `pixivfeed/storage/usage.py`（新建）：UsageStore 封装 + 查询 API + kind 常量
- `pixivfeed/storage/__init__.py`：export
- `pixivfeed/channel/telegram/bot.py`：注入 UsageStore、注册 /stats
- `pixivfeed/channel/telegram/handlers.py`：spoiler、用量日志散点、cmd_stats
- `pixivfeed/provider/ehentai/_archive.py`：parse_gp_cost；`request_archive` 返回值多一项 gp_cost
- `pixivfeed/provider/ehentai/__init__.py`：request_archive 调用点适配

## v0.4.1 — 2026-05-07

### 新增
- 任务队列与并发控制（`pixivfeed/channel/telegram/jobqueue.py`）：把"重活"按类别分到独立的 worker pool，避免群里多人同时触发把内存/网络打爆。
  - 类别与并发：`archive_zip`(1) / `zip2tph`(1) / `direct_image`(2) / `telegraph_publish`(3)
  - admin 永远 priority=0 插队前置；普通用户 priority=1 按入队顺序排
  - 同一用户在同一类别等待中的任务超 2~3 个会被拒绝入队（防止超时误判被反复重试灌爆队列）
  - 入队反馈：占位消息显示"已加入队列，前面还有 N 个任务等待中"；进入处理时切换到工作文案
- 实时下载 ETA：archive zip 下载（单流/多线程）、`/zip2tph` 接收的 zip，进度条携带速率与剩余时间，例如 `⏳ 下载 zip 12.3MB/45.6MB (27.0%) · 1.2MB/s · ~28s剩余 [6线程]`。
  - `progress.ByteRateTracker` 与 `fmt_duration` 工具
  - `ImageCounter.tick` 在 N>1 时也带上 ETA
- zip 解压进度：`/zip2tph` 解压阶段用旁路 watcher 周期数 extracted/ 目录里的图片数显示 `⏳ 解压中 12/85 · ~30s剩余`，避免长时间停在"解压并校验图片..."无反馈。
- 任务可观测性日志：jobqueue 在入队 / 拒绝 / 开始 / 完成 / 崩溃节点都打 `logger.info`（含 seq / user / priority / 等待时长 / 处理耗时），方便诊断"任务卡住但 log 一片空白"。
- 上传心跳：`send_document` 调用期间每 5 秒推一次 `⏳ 上传 zip (NMB)... 已 Ms（本地 Bot API → TG 主网）` 状态，避免大文件上传期间用户看到画面静止以为挂了；同时打出 send_document start / done 两条 log 标记真实进入与退出时间。
- archive 动态超时：解析 chooser 页 `Estimated Size`，按预估字节数动态算下载超时（基础 5 分钟 + 每 MB 5s，封顶 1 小时），不再固定 300s 卡死大画廊（如 1.77 GiB 原图归档）。解析失败时退回 `archive_timeout` 配置值。
- archive session 锁定识别：检测 chooser 页里 "This archive session has been used from too many different locations" 文案后抛 `ArchiveLockedError`，**自动 POST `invalidate_sessions=1` 取消旧 session**（让用户下次重新提交时拿新链接），但**不自动重试本次任务**。给出明确提示"archive session 已被锁定，已自动取消旧链接，请稍后重新提交本画廊"。
- archive zip H@H 节点等待：拿到 zip URL 后并不立即 GET 下载，先 HEAD 探测节点是否上线（404/503/502/504 时退避 5~15s 重试，最多 12 次 ≈3 分钟）。新分配的 archive 链接通常需要 5~30s H@H 节点才 ready，之前直接 GET 命中 404 会被当成永久错误返回；现在能正确等到节点启动。
- archive zip 探测改用 GET Range:0-0 + ZIP 头嗅探：`HEAD` 在某些 H@H 节点上即便服务正常也返 404，导致旧版本误判节点不可用。新版用 `GET Range:0-0` 拿前 2KB 判断 —— `PK\x03\x04` 是 ZIP 走下载，纯文本含 "too many different locations" 抛 `ArchiveLockedError`，其他文本当错误页报 preview。同时探测在 6 次失败时放弃，把后续 fallback 交给上层。
- archive 双链接 fallback：发现 eh/ex 给 bot 派的 hath.network 链接和主站本地 `/archive/...` 共用同一 path，所以只要把 host 换成 `exhentai.org`/`e-hentai.org` 就是稳定的本地下载。优先尝试 hath.network（带宽好），失败 fallback 到主站，再失败才走 `refresh_download_link` 重新拉。
- 任务取消按钮：所有"重活"占位消息上挂 `❌ 取消` 按钮，仅 owner 与 admin 可点。
  - 排队中：标记 cancelled，worker 跳过
  - 运行时：`asyncio.Task.cancel()` 干净中断（清理 .part 临时文件等）
  - 上传 zip / Telegraph 发布阶段：按钮自动消失，上传/发布不可中途取消（避免 telegram-bot-api 仍后台传输或 telegraph 留半成品页面）

### 修复
- 大文件超时误判：
  - `send_document` / `get_file` / `download_to_drive` 的 `read_timeout` / `write_timeout` 调大到 3600s（数据在本机走 local_mode，HTTP 不应该传输中超时）
  - 异常嗅探放宽：除 `telegram.error.TimedOut` 外，所有 `*Timeout*` 类型 / 字符串含 "timed out" 都按"温和提示"处理，不再误报失败
  - `/zip2tph` 入站 zip 下载：超时但落盘文件大小已等于 `document.file_size` 时视为成功；首次超时自动重试一次
  - 上传 zip：超时时显示"⏳ 上传超时，本地 Bot API 仍在向 TG 主网传输，请稍候 1-2 分钟"，而非"⚠️ 发送 zip 失败"
- `/zip2tph` 解压阶段卡死 event loop：`_extract_zip_images` 是同步阻塞调用，整个 bot 进程被卡住导致进度不更新、其他任务全部停摆。改为通过 `asyncio.to_thread` 在线程池里跑（[handlers.py:1157](pixivfeed/channel/telegram/handlers.py#L1157)）。
- e-hentai meta 阶段不带登录 cookie 导致受限画廊被误报为 404：部分画廊（年龄分类、特殊 tag）即便在 e-hentai 域名也需要登录态才能访问，未登录返回 "Gallery Not Available" 错误页被解析成 `EHGalleryUnavailable`。现在 e-hentai 在所有阶段（包括 meta / PAGE_SAMPLE）都尝试带上配置中的 ex cookie；没配也不影响公开画廊。([provider/ehentai/__init__.py:463](pixivfeed/provider/ehentai/__init__.py#L463))

### 改动文件
- `pixivfeed/channel/telegram/jobqueue.py`（新建）
- `pixivfeed/channel/telegram/progress.py`
- `pixivfeed/channel/telegram/bot.py`
- `pixivfeed/channel/telegram/handlers.py`
- `pixivfeed/provider/ehentai/__init__.py`
- `pixivfeed/provider/ehentai/_archive.py`

## v0.4.0 — 2026-05-06

### 新增
- 新增 `/archive <链接>` 命令：对链接产出压缩包并通过 sendDocument 返回。
  - eh/exhentai 仍弹四模式按钮（page_sample / page_original / archive_resample / archive_original）。
    archive_* 模式直接走 archiver.php 拿 zip 直链；page_* 模式下载图片后本地打 zip。
  - pixiv illust / nhentai：下载图片后打 zip。
  - pixiv novel：不支持，直接报错。
  - 文件超过 50MB 且未配置本地 Bot API 时直接报错。
- 新增 `/zip2tph` 命令：把上传的纯图片 zip 发布为 Telegra.ph。
  - 触发：把 zip 文件发给 bot 并在 caption 写 `/zip2tph`，或对 zip 消息回复 `/zip2tph`。
  - 仅处理 `.jpg/.jpeg/.png/.gif/.webp`，按文件名字典序排序。
  - 拷贝到 `storage.cache_dir/zip_<token>/` 由 Nginx 暴露给 Telegra.ph。
- 引入耗时长场景的进度条（节流 1 秒/次的 `edit_message_text`）：
  - pixiv novel：阶段 + 嵌入图/引用插画下载 N/M
  - eh/ex archive：zip 流式下载（含已下载/总大小、百分比）
  - /archive 通用：图片下载、打包阶段计数
  - /zip2tph：接收 → 解压 → 拷贝 → 发布
  其余场景仍只显示阶段状态。

### 变更
- pixiv 小说 / 漫画 / 图集类作品的 Telegra.ph 页面：把"原作品"链接置于篇首
  （独立于用户配置的 `templates.*.page_header`，模板里仍可保留旧行为）。
  ([pixivfeed/publisher/telegraph.py](pixivfeed/publisher/telegraph.py),
   [pixivfeed/provider/pixiv/novel_publisher.py](pixivfeed/provider/pixiv/novel_publisher.py))

### 改动文件
- 新增 `pixivfeed/channel/telegram/progress.py`
- `pixivfeed/channel/telegram/handlers.py`
- `pixivfeed/channel/telegram/bot.py`
- `pixivfeed/publisher/telegraph.py`
- `pixivfeed/provider/pixiv/novel_publisher.py`

## v0.3.1 — 2026-05-06

### 修复
- e-hentai/exhentai 归档下载报 `could not parse archiver token from gallery page`：
  画廊页 `onclick` 里的 `archiver.php?...` URL 在 JS 字符串内使用原始 `&`，
  而原正则只匹配 `&amp;` 转义形式，导致抠不出 `or=` 参数。
  修复为同时兼容两种形式。
  ([pixivfeed/provider/ehentai/_archive.py](pixivfeed/provider/ehentai/_archive.py#L32))
