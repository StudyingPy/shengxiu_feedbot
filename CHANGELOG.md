# Changelog

## Unreleased — 2026-05-13

### 修复
- `deploy/feed-bot-webhook.service` 里的 `NoNewPrivileges=yes` 和脚本里的 `sudo systemctl restart pixiv-feed-bot` 天然冲突——sudo 是 setuid 二进制，这条 hardening 直接让它拒绝提权，所有 webhook 触发的部署都卡在 restart 那一步报「sudo: The "no new privileges" flag is set」。删除该行；其它 hardening（ProtectSystem / ProtectHome / PrivateTmp / ReadWritePaths）保留。注释里写明这条不能加。
- DEPLOY.md 排错表补一条对应症状的修法。

### 变更
- 部署 webhook 通知现在默认从 `/etc/pixiv-feed-bot/config.yaml` 读 `telegram.token` / `auth.admin_users[0]` / `telegram.base_url`，省去再单写一份 `/etc/feed-bot-webhook/env`。env 文件保留作为 override（写另一个 bot/admin 接 deploy 噪音时用）。要求 deploy 用户能读 config.yaml（chgrp pixivbot + chmod 0640 + usermod -aG）。
- 通知正文加：版本号变化（`0.6.0 → 0.6.1`）、HEAD 短哈希、`git tag --points-at` 命中的 tag、commit 列表（最多 15 条）、`git diff --shortstat` + 文件列表（最多 12 个）。失败通知额外保留尾部错误日志段。从此打开 admin 私聊就能直接看到这版部署到底发生了什么，不必再 ssh 登服务器看 journal。

### 改动文件
- `deploy/feed-bot-deploy.sh`：新增 config.yaml 默认凭据读取（venv python + PyYAML），新增 `build_summary` helper；`notify` 支持 base_url
- `docs/DEPLOY.md`：第 5 步重写为「默认值 + override」结构；测试段加成功/失败通知样例；排错表更新

## v0.6.0 — 2026-05-13

### 新增
- GitHub webhook 自动部署。`main` push 后服务器 1–2 秒内 `git pull` + 按需 `pip install` + `systemctl restart pixiv-feed-bot`，结果通过 TG 推给 admin（成功/失败都推）。链路 `GitHub → Nginx /deploy → adnanh/webhook (127.0.0.1:9000) → feed-bot-deploy.sh → sudo systemctl restart`，webhook 跑在专用 deploy 用户下，sudoers 仅放行 `restart pixiv-feed-bot`。hooks.json 三重过滤（HMAC-SHA256 / 仅 main / 仅 push event），减少误触发面。配置/安装见 [docs/DEPLOY.md](docs/DEPLOY.md) 新增的「GitHub webhook 自动部署」段。
- `/stats` 总览补「按群组排行（前 10）」段；管理员在群里直接 `/stats` 默认查本群（私聊仍是全局总览）。`/stats chat <id>` 现在同时接受 `-1001838275879` / `1838275879`（短 id 自动补 `-100` 前缀）/ `@username` 三种形式；新增 `/stats chats [窗口]` 列群组活跃排行。展示用 [_friendly_chat_id](pixivfeed/channel/telegram/handlers.py) 把 `-100…` 转成 `c/…`，让管理员不用再面对原始 bot API id。
- 新增 `chats` 表与 [upsert_chat](pixivfeed/storage/usage.py) / [get_chat_display](pixivfeed/storage/usage.py) / [per_chat_summary](pixivfeed/storage/usage.py)：`_track_user`（早就在跑）现在顺手把 effective_chat 的标题/类型/用户名 upsert 进来，给 chat 维度统计提供可读名字。
- 取消按钮（`jc:` 任务取消、`eh:` 模式选择取消、`eha:` /archive 模式取消）触发后 5s 自动删用户原始触发消息 + bot 回复。仅限群组且 bot 是 admin + 持 `can_delete_messages` 才动；私聊和无权限群一律不删，避免"半截"消息。删除范围严格限定本对消息（用 placeholder.reply_to_message 推出来），不会误伤其他历史。逻辑在 [_schedule_delete_after_cancel](pixivfeed/channel/telegram/handlers.py)。
- `/setting` 命令为布尔型与少数枚举型字段附带 inline 按钮切换。`/setting get <key>` 在 key 命中 `TOGGLE_OPTIONS` 时返回当前值 + 一组按钮（当前值前缀 `●`），点击直接 `set_runtime` 并刷新消息。`_set_setting` / `handle_setting_edit_followup` 完成后也返回同样的键盘，省去再敲一次 `get` 的步骤。覆盖范围：`collectors.{ehentai,exhentai,nhentai}.enabled`、`collectors.{ehentai,exhentai}.default_mode`、`logging.level`。callback_data 走 `stg:<key>:<value>` 单 prefix，由 [handlers.py:handle_callback](pixivfeed/channel/telegram/handlers.py) 分发到 [setting.py:handle_setting_callback](pixivfeed/channel/telegram/setting.py)；回调内复校 admin 权限。

### 修复
- Pixiv 长篇小说（约 70k+ 字）发布到 Telegra.ph 抛 `CONTENT_TOO_BIG` / `PAGE_SAVE_FAILED`。根因：`NOVEL_TEXT_SOFT_LIMIT = 18000` 是**字符数**，但 Telegra.ph 64KB 限制是**序列化 JSON 字节数**，纯中文 18000 字 × 3 字节 + JSON 节点开销已撞上限。修复：(1) 字符上限降至 14000（留 15% 余量给章节分布不均与嵌入图）；(2) 在 [novel_publisher.py](pixivfeed/provider/pixiv/novel_publisher.py) 新增 `_ensure_byte_safe_chunks` —— 粗切后对每个 chunk 真实构建 nodes 并测 `json.dumps` 字节数，超 60KB 就在自然分隔符处二分递归，作为最后防线。

### 改动文件
- `deploy/feed-bot-deploy.sh`：新增，部署执行脚本（fetch → 比对 → reset → 可选 pip install → restart → TG 通知）
- `deploy/feed-bot-webhook.service`：新增，adnanh/webhook 的 systemd unit
- `deploy/feed-bot-webhook-hooks.json.example`：新增，hooks 配置模板（HMAC + main + push 三重 trigger-rule）
- `deploy/feed-bot-deploy.sudoers`：新增，sudoers 片段
- `docs/DEPLOY.md`：新增「GitHub webhook 自动部署」段，含 9 步安装、Nginx 接入、排错表
- `pixivfeed/storage/db.py`：新增 `chats` 表
- `pixivfeed/storage/usage.py`：新增 `ChatSummary` / `upsert_chat` / `get_chat_by_username` / `get_chat_display` / `per_chat_summary`
- `pixivfeed/storage/__init__.py`：导出 `ChatSummary`
- `pixivfeed/channel/telegram/handlers.py`：`_track_user` 顺带 upsert_chat；`cmd_stats` 接 group default、`chats` 子命令、`/stats chat` 多形式解析；新增 `_schedule_delete_after_cancel` / `_delete_pair_after_cancel` 并挂到三个 cancel 回调；`handle_callback` 增加 `stg:` 前缀分发
- `pixivfeed/channel/telegram/setting.py`：新增 `TOGGLE_OPTIONS`、`_toggle_keyboard`、`_eq_value`、`_render_setting_value`、`handle_setting_callback`；`_get_setting` / `_set_setting` / `handle_setting_edit_followup` 在回消息时附带键盘
- `pixivfeed/provider/pixiv/novel_publisher.py`：`NOVEL_TEXT_SOFT_LIMIT` 18000 → 14000；新增 `_find_split_point`、`_ensure_byte_safe_chunks`、`TELEGRAPH_CONTENT_BYTE_LIMIT = 60000`；`publish_novel` 主循环在粗切后接一道字节预检

## v0.5.0 — 2026-05-11

### 新增
- `/wiki <词条>` 命令：在中文维基百科查词条，回首条命中（标题、链接、概要、总词数）。走白名单鉴权，群组/私聊均可用。逻辑参考 [Reference projects/bot-rs-master/src/funcs/command/wiki.rs](Reference%20projects/bot-rs-master/src/funcs/command/wiki.rs)。
- Inline 模式从 pixiv 图片解析切换为维基百科搜索（`@bot <关键词>`，返回 top 5 结果列表）。仍仅 admin_users 可用。
- 新模块 [pixivfeed/wikipedia.py](pixivfeed/wikipedia.py)：纯逻辑层 `search_wikipedia(query, lang, limit)`，与 telegram 解耦，slash command 与 inline 共用同一份。未来扩 en/jp wiki 或换 API 只改一处。

### 变更
- Pixiv inline 图片解析（v0.4 起的"@bot 12345 直接出图"）暂时禁用——图片代理在某些情况下加载失败。代码连同恢复步骤一并以注释形式保留在 [pixivfeed/channel/telegram/inline.py](pixivfeed/channel/telegram/inline.py) 文末，未来稳定后取消注释 + 在 dispatcher 加一行优先匹配即可恢复。

### 修复
- 取消按钮在 v0.4.3 引入的新进度路径上消失。`Progress.update` 每次 `edit_text` 会清掉 reply_markup；按钮通过 `_PLACEHOLDER_MARKUPS` 共享给 Progress 实例，但 v0.4.3 新构造的 5 个 `Progress(...)` 没调 `_attach_progress_markup` 把按钮装回去。补齐：[handlers.py:_handle_eh_via_telegraph](pixivfeed/channel/telegram/handlers.py) 主路径 + exhentai fallback 重建处、[handlers.py:_send_via_telegraph_generic](pixivfeed/channel/telegram/handlers.py)、[handlers.py:_send_pixiv_illust_via_telegraph](pixivfeed/channel/telegram/handlers.py)、[handlers.py:_send_pixiv_illust_direct](pixivfeed/channel/telegram/handlers.py)。
- eh/ex 默认链路的 archive 模式用固定 `archive_timeout`（config 默认 300s），大画廊（>500MB）必现 timeout。`/archive` 命令路径 v0.4.1 起就有动态超时（5min + 5s/MB，封顶 1h），但只写在 handlers.py 那一段。本次抽出共享 helper `compute_archive_timeout` 放进 [provider/ehentai/_archive.py](pixivfeed/provider/ehentai/_archive.py)，两条路径都用——默认链路也能跑大画廊；config 的 `archive_timeout` 仍作为下限，用户调高也生效。
- v0.4.3 重构 archive zip 下载时漏了 `[N线程]` / `[单流]` 后缀。原因是 hook 工厂 `make_bytes_hook` 在 channel 层、字符串模板固定，没法表达 downloader 内部选了哪种策略。改为 `download_archive_with_timeout` 内部用 `ByteRateTracker` 拼好富文本（含 suffix）通过 `on_status` 推；`on_progress`（数值钩子）退为兼容入口，`on_status` 设置时跳过它避免两个回调争抢同一占位消息。

### 变更（底层）
- `ByteRateTracker` / `fmt_bytes` / `fmt_duration` 从 [channel/telegram/progress.py](pixivfeed/channel/telegram/progress.py) 移到 [pixivfeed/utils.py](pixivfeed/utils.py)（与 telegram 解耦，避免 Provider 层用它时构成循环依赖）。`channel/telegram/progress.py` 保留 re-export，handlers.py 现有 import 行无需改。
- `_EHFamilyProvider.fetch_and_download_with_mode` 新增可选 `on_status: StatusUpdater = None` 参数；archive 模式下走 `on_status` 走富文本，page_* 模式仍走 `on_progress`。

### 改动文件
- `pixivfeed/wikipedia.py`：新增，wiki 搜索纯逻辑
- `pixivfeed/channel/telegram/wiki.py`：新增，`/wiki` 命令处理
- `pixivfeed/channel/telegram/inline.py`：dispatcher 化，默认走 wiki；pixiv 旧代码以注释保留
- `pixivfeed/channel/telegram/bot.py`：注册 `cmd_wiki`，`PUBLIC_COMMANDS` 加 `/wiki`
- `pixivfeed/channel/telegram/handlers.py`：`cmd_start` 帮助文本加 `/wiki`；5 处新 `Progress(...)` 后补 `_attach_progress_markup`；archive 模式从 `on_progress=make_bytes_hook(...)` 改为 `on_status=progress.update`；`_eh_archive_with_mode` 内联的 dyn_timeout 计算改用 `compute_archive_timeout`
- `pixivfeed/utils.py`：搬入 `ByteRateTracker` / `fmt_bytes` / `fmt_duration`
- `pixivfeed/channel/telegram/progress.py`：删本地定义改为 re-export
- `pixivfeed/provider/ehentai/_archive.py`：新增 `compute_archive_timeout`；`download_archive_with_timeout` 内部用 `ByteRateTracker` 推富文本，恢复 `[N线程]` / `[单流]` 后缀
- `pixivfeed/provider/ehentai/__init__.py`：`fetch_and_download_with_mode` 加 `on_status`；`_archive_pipeline` 用 `compute_archive_timeout` 替代固定 `archive_timeout`

## v0.4.3 — 2026-05-11

### 新增 / 变更
- 所有下载/发布路径补齐实时进度反馈。此前只有 `/archive` archive 模式和 `/zip2tph` 接收阶段能看到字节流进度；其余"贴链接走默认链路"的下载阶段（pixiv illust direct、pixiv illust telegraph、nhentai、eh/ex page_*、eh/ex archive 默认链路）以及 Telegra.ph 多 chunk 发布阶段全部缺失。本次补齐：
  - pixiv illust 直发 / Telegra.ph 模式：显示 `⏳ 下载图片 N/M · ~ETA剩余`
  - nhentai：显示 `⏳ nhentai 下载图片 N/M · ~ETA剩余`，缓存命中也计入推进
  - eh/ex 网页模式（page_sample / page_original）：显示下载图片 N/M
  - eh/ex 归档模式（archive_resample / archive_original）默认链路：显示字节流 `⏳ 下载 zip 12.3MB/45.6MB (27.0%) · 1.2MB/s · ~28s剩余`
  - Telegra.ph 多 chunk 发布（>300 图）：显示 `⏳ 发布 Telegra.ph 页面 N/M`
- 引入统一 progress hook 协议：`ProgressHook = Callable[[int, int], Awaitable[None]] | None`、`StatusUpdater = Callable[[str], Awaitable[None]] | None`，定义在 [pixivfeed/provider/__init__.py](pixivfeed/provider/__init__.py)。Provider 层不感知 telegram，回调内容由 channel 层通过 `make_item_hook` / `make_bytes_hook` 工厂决定（[pixivfeed/channel/telegram/progress.py](pixivfeed/channel/telegram/progress.py)）。
- 重构合并：`/archive` 命令此前为 archive zip 下载复刻了一份带进度的实现（`_stream_download_with_progress`，含 H@H 节点等待 + Range 多线程下载），与 `download_archive_with_timeout` 默认链路并行存在。本次把丰富逻辑全部搬进 [provider/ehentai/_archive.py:download_archive_with_timeout](pixivfeed/provider/ehentai/_archive.py)，两条路径合一；`/archive` 和默认链路获得完全一致的进度反馈与下载实现。
- 文档修正：`Progress` 类构造器实际默认 `min_interval=1.0`，docstring 与顶部说明里残留的"3 秒一次"已改正（[pixivfeed/channel/telegram/progress.py](pixivfeed/channel/telegram/progress.py)）。
- novel 路径不变：现有阶段+计数反馈已足够，未追加 ETA。

### 改动文件
- `pixivfeed/provider/__init__.py`：新增 `ProgressHook` / `StatusUpdater` 类型别名，`Provider.fetch_and_download` 加 `on_progress` 可选参数
- `pixivfeed/provider/pixiv/__init__.py`、`pixivfeed/provider/pixiv/downloader.py`：透传 `on_progress`，每张图（含缓存命中）调一次
- `pixivfeed/provider/nhentai/__init__.py`：透传 `on_progress`，主下载轮 + fallback 轮共用计数器
- `pixivfeed/provider/ehentai/__init__.py`：`fetch_and_download_with_mode(..., on_progress=)` + `_download_direct(..., on_progress=)` + `_archive_pipeline(..., on_progress=)`
- `pixivfeed/provider/ehentai/_archive.py`：`download_archive_with_timeout(..., on_progress=, on_status=)`，并入 H@H 节点探测 + Range 并发下载（原 handlers.py 中 `_stream_download_with_progress` 的实现）
- `pixivfeed/publisher/telegraph.py`：`publish_gallery(..., on_progress=)`，单页/多页都推
- `pixivfeed/channel/telegram/progress.py`：新增 `make_item_hook` / `make_bytes_hook` 工厂；导入 `ProgressHook`；docstring 节流默认值订正
- `pixivfeed/channel/telegram/handlers.py`：所有 fetch_and_download / fetch_and_download_with_mode / fetch_and_download_illust / publish_gallery 调用点接入 hook；删除 `_stream_download_with_progress`，`/archive` archive 路径改调 `download_archive_with_timeout`

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
