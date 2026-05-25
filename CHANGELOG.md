# Changelog

## [Unreleased]

### Fixes
- **`/cache stats` 文案打磨**：[handlers.py:cmd_cache](pixivfeed/channel/telegram/handlers.py)。"legacy（升级前条目，状态未知）" 容易让人误读为"图坏了"，实际语义是 v0.8.2 schema 升级前的旧条目缺 `r2_image_count` 元数据；改为"升级前条目，元数据缺失"。"非 durable 非 legacy" 否定式拗口，改为"fallback（存于服务器中，到期失效）"。fallback 原因分布从裸 enum 名（`size_guard_skipped` / `r2_partial` 等）改为附中文一句话解释（`画廊 >1GB 跳过 R2` 等），新增 `_FALLBACK_REASON_ZH` 映射表对账 [publisher/_resolver.py:FallbackReason](pixivfeed/publisher/_resolver.py)。

## v0.10.0 — 2026-05-25

telegraph 缓存联动失效：底层图片（R2 对象 / cache_dir 文件）被清理时，对应 `telegraph_cache` 行自动失效，避免用户重提相同链接命中"图已 404 的旧 telegra.ph 链接"。同时新增 `/cache invalidate` admin 救济命令。

### Features
- **`/cache invalidate` admin 命令**：把 `TelegraphCache.invalidate_by_pattern()` 暴露给管理员私聊使用，用于救济"telegra.ph 页面里的图已失效但 cache 仍命中坏 URL"场景。kind 字段支持 SQL `LIKE` 通配符（例：`/cache invalidate ehentai/gallery/% 3936793` 一次失效该 gid 下全部 mode 的 cache 行）。同步加 `/cache stats` 复用现有 [TelegraphCache.stats()](pixivfeed/storage/cache.py)。
- **R2 LRU 联动失效 telegraph_cache**：[bot.py:_r2_lru_loop](pixivfeed/channel/telegram/bot.py) 后台任务和 [/stats r2_evict](pixivfeed/channel/telegram/handlers.py) 手动入口，在 `lru_evict_to_target` 删完 R2 对象后，把对应 `telegraph_cache` 行也失效掉。原本 R2 LRU 删了某画廊的图后 cache 行还在，普通用户重提相同链接命中坏 URL 永远拿不到正常页面；现在自动失效，下次重提会触发完整重发拿到新链接。
- **`deploy/cleanup.py` 联动失效 telegraph_cache**：cache_dir 顶层目录被完整 `rmdir` 时（即 cache_days 后某画廊全部文件已清理），同步删对应 `telegraph_cache` 行。R2 未启用部署的默认场景下，第 8 天起重提相同链接会自动重新发布而不是命中坏链接。
- **新增 [pixivfeed/storage/cache_keymap.py](pixivfeed/storage/cache_keymap.py)** 反向映射：从 cache_dir 顶层目录名 / R2 absolute key 反推 (kind_pattern, pixiv_id)，给上面两条联动失效共享。R2 key 与 cache_dir 相对路径完全同源（v0.8.0 设计约束）所以一份规则两边都能用。**协作者警告写在文件顶部**：每加一个走 cache 的 provider，必须同步在这里加规则，否则该 provider 的图片清理后 cache 不会失效。

### Fixes
- **`Config._coerce` 错误信息友好化**。[config.py:_coerce](pixivfeed/config.py) 此前 bool/int/float/list 解析失败时直接抛裸 `int("abc")` 这类英文 ValueError，`/setting set` handler 现有 `except ValueError` 显示给 admin 时是 "invalid literal for int() with base 10: 'abc'" 这种内部错误。改为统一中文友好提示，例如 "无法将 'abc' 解析为整数"。无行为变更（handler 端 catch 路径不变）。
- **e-hentai 子页 `fullimg` 链接路径式 URL 兼容**。e-hentai 在 2025 前后把 "Download original" 链接从 `?query` 形式（`/fullimg.php?gid=X&page=Y&key=Z`）改成路径形式（`/fullimg/X/Y/Z/N.png`），老正则 `_EH_FULLIMG_RE` 只接受 `?query`，新页面上拿不到 fullimg → `_extract_page_image_urls` 静默回退到 sample，导致 `PAGE_ORIGINAL` 实际产出 = `PAGE_SAMPLE` 大小。正则改为 `<a[^>]*?href="(https?://[^"]+/fullimg(?:\.php\?[^"]+|/[^"]+?))"` 同时匹配两种形式。

### Internal
- **`TelegraphCache.invalidate_by_pattern(kind_pattern, pixiv_id) -> int`**：新增 SQL `LIKE` 版 invalidate，返回删除行数。原 `invalidate(kind, pixiv_id)` 保留。
- **`pixivfeed.storage.cache.invalidate_for_r2_keys(cache, deleted_keys, *, r2_prefix)`**：共享 helper，给 `_r2_lru_loop` 和 `/stats r2_evict` 复用。负责"R2 keys → 反向映射 → 去重 → 批量 invalidate"完整流程。任何一步失败都只 log，不影响主流程（best-effort 联动）。
- **`lru_evict_to_target` 返回签名 breaking change**：`tuple[int, int]` → `tuple[int, int, list[str]]`，第三项是已删 absolute_key 列表给联动失效用。两个调用点（[bot.py:_r2_lru_loop](pixivfeed/channel/telegram/bot.py)、[handlers.py /stats r2_evict](pixivfeed/channel/telegram/handlers.py)）已同步更新；外部无调用方。
- **`_set_setting` / `handle_setting_edit_followup` / `handle_setting_callback` 路径不变**：[setting.py](pixivfeed/channel/telegram/setting.py) 现有 `except ValueError as e: ... ⚠️ 值无效：{e}` 直接显示新版 _coerce 的中文消息；不需要新增 try/except。
- **handlers.py 第一个 cache_kind 拼接处加协作者注释**（[handlers.py:1140](pixivfeed/channel/telegram/handlers.py)）：提醒新增 provider / 新 mode 时同步更新 `cache_keymap.py`。

### 改动文件
- 新增：`pixivfeed/storage/cache_keymap.py`
- `pixivfeed/storage/cache.py`：`invalidate_by_pattern` + `invalidate_for_r2_keys`
- `pixivfeed/storage/__init__.py`：暴露反向映射 helper
- `pixivfeed/storage/r2.py`：`lru_evict_to_target` 返回签名加 `deleted_keys`
- `pixivfeed/channel/telegram/bot.py`：`_r2_lru_loop` 联动失效 + 注册 `/cache` 命令 + ADMIN_COMMANDS
- `pixivfeed/channel/telegram/handlers.py`：`cmd_cache` admin 命令 + `/stats r2_evict` 联动失效 + cmd_start 帮助 + cache_kind 协作者注释
- `pixivfeed/config.py`：`_coerce` 中文友好错误
- `deploy/cleanup.py`：rmdir 后联动失效 telegraph_cache

## v0.9.0 — 2026-05-25

下载前预获取并展示作品大小。eh/ex 归档按钮 label 加 `~XX MB`，Pixiv / nhentai 私聊弹详情卡（标题 + 张数 + 预估大小 + 模式按钮）；网络估算走 HEAD → streaming Range fallback，永不读 body。

### Features
- **eh/ex 归档按钮显示预估大小**。私聊粘 eh/ex 链接、`/archive` 命令、`/ehsearch` 进 L2 详情卡 / L3 zip 选单共 5 处入口，发完详情卡后异步 GET `archiver.php` chooser 页解析 res / org 两档预估字节数（不消耗 archive 配额），1~3s 后通过 `edit_message_reply_markup` 把"归档 · 重采样" / "归档 · 原图"按钮 label 改成 `归档 · 原图 ~1.8GB`。失败一律静默回退到裸 label。
- **Pixiv 私聊详情卡**。`mode=auto` 时不再直接下载，而是先发卡片显示标题 / 画师 / 张数 / R-18 / `~XX MB` / 标签 + 按钮 `[直发图片] [Telegra.ph]`（page_count > 10 时仅 Telegra.ph）+ `[取消]`。`/pixiv_direct` `/pixiv_telegraph` 命令保留原 UX 不弹卡片。
- **nhentai 私聊详情卡**。新增 `nh:{token}:{action}` 路由 + `[开始下载] [取消]` 按钮，正文显示张数 / 预估大小 / 前 12 个 tag。
- **`size_prefetch` 配置段**。新增 `size_prefetch.enabled` / `sample_count` / `timeout` / `eh_archive` / `pixiv` / `nhentai` 6 个 key，全部进 `RUNTIME_KEYS`，私聊 `/setting set size_prefetch.enabled false` 可热关。

### Fixes
- **修复 Pixiv 私聊 direct 模式 reply_to 落到将被删 placeholder 的 bug**。`_Pending` 新增 `orig_chat_id` / `orig_msg_id` 字段记录用户原始消息身份；`_send_pixiv_illust_direct` 接收 `reply_to_message_id` / `reply_to_chat_id` 参数，按钮回调时显式传入。原来回调里 `update.effective_message` 是 bot 的详情卡，函数末尾 `placeholder.delete()` 后图片就回复到一条不存在的消息上。
- **`_size_prefetch.head_or_range_content_length` Range fallback 改 streaming**。原实现用 `client.get()`，服务端忽略 `Range` 返 200 时 httpx 会读完整响应体，把"下载前估算"反过来变成下载本身（i.pximg.net 大图、nhentai CDN 都可能这样）。改成 `client.stream("GET", ...)` 只读 headers，离开 `async with` 时 httpx aclose 连接，body 一字节不下。
- **Pixiv / nhentai 详情卡 HTML 转义不完整**。原 `.replace("<","&lt;").replace(">","&gt;")` 不处理 `&`，含 `&` 的 title / tag 会让 TG `edit_text(parse_mode=HTML)` 报 `Bad Request: can't parse entities`。统一改走仓库内已有的 `_html_escape`。
- **`/ehsearch` 切层 / 翻页时旧 ptoken 没失效的竞态**。L1 / L2 / L3 / 翻页复用同一条消息，旧 ptoken 在 TTL 过期前都活在 `_PENDING` 里；晚到的 size prefetch 可能把当前列表页或新页详情卡的按钮覆盖回旧条目的按钮。`_SearchState` 新增 `active_ptoken` 字段，所有切层入口（`ehs_open` / `ehs_arch_menu` / `ehs_back2det` / `ehs_back2list` / `ehs_more` / `_ehs_navigate` next/prev）调用 `_search_invalidate_active_ptoken(state)` 立刻把旧 ptoken 从 `_PENDING` 弹出，`_safe_update_buttons` 第一关 `pending is None` 就拒绝写入。`_handle_ehs_arch_menu` 不再"按需复用"callback_data 里的 ptoken，改为无条件重挂 + invalidate，避免 L2 起的 prefetch 回填到 L3。

### Internal
- **新增 `pixivfeed/provider/_size_prefetch.py`**：`head_or_range_content_length(client, url)` HEAD 优先 + streaming Range fallback；`estimate_total_bytes(client, urls, sample_count=N)` 采样均值 × len(urls) 求估算总大小。任何失败一律返回 None，永不抛给上层。
- **新增 `pixivfeed/provider/ehentai/_archive.fetch_archive_sizes(client, host, gid, token)`**：从 `request_archive` 内部 GET chooser 部分独立抽出，只 GET 不 POST，不消耗配额，解析 chooser HTML 的 `Estimated Size` 字段返回 `dict[EHMode, int]`。
- **新增 `pixivfeed/config.SizePrefetchConfig`** dataclass 与 6 个 `RUNTIME_KEYS`，挂在 `AppConfig.size_prefetch` 上。
- **`handlers._Pending` 加 `orig_chat_id` / `orig_msg_id`** 字段，4 处 `_Pending(...)` 构造点（eh / `/archive` / Pixiv / nhentai）均填充用户原始消息身份。
- **`handlers._safe_update_buttons` / `_safe_update_card`** 竞态保护 helper：三重校验 `token in _PENDING` + `chat_id` + `msg_id`，prefetch 完成时仅在三者全部匹配才发 edit。
- **`handlers._schedule_eh_size_prefetch` / `_schedule_pixiv_size_prefetch` / `_schedule_nhentai_size_prefetch`** fire-and-forget 任务封装；`_schedule_eh_size_prefetch` 支持可选 `keyboard_builder` 让搜索流 L2/L3 复用各自渲染逻辑，并接受 `prefix` 参数区分 `eh:` / `eha:` 回调路由。
- **`_make_eh_keyboard` / `_eh_mode_buttons`** 签名扩展：加 `sizes: dict[EHMode, int] | None` 与 `prefix: str` 参数；首次发送时 sizes=None 出裸 label，prefetch 完成回填出 `~XX MB`。
- **`provider/nhentai/__init__.py`** 把 `NHENTAI_CDNS` 加进 `__all__`，供 handlers 复用 CDN 列表生成 size 采样 URL。

### 改动文件
- 新增：`pixivfeed/provider/_size_prefetch.py`
- `pixivfeed/channel/telegram/handlers.py`：详情卡 + 竞态保护 + 路由
- `pixivfeed/config.py`：`SizePrefetchConfig` + RUNTIME_KEYS
- `pixivfeed/provider/ehentai/_archive.py`：抽 `fetch_archive_sizes`
- `pixivfeed/provider/nhentai/__init__.py`：`__all__` 暴露 `NHENTAI_CDNS`
- `config.example.yaml`：`size_prefetch` 段
- `pyproject.toml`：version 0.8.3 → 0.9.0

## v0.8.3 — 2026-05-20

磁盘剩余空间护栏 + 文档大幅重写。鲁棒性补强，无新功能。

### Fixes
- **磁盘满时不再以 OSError(28) 顶死**。下载图片 / 归档 zip / `/zip2tph` 接收 / `shutil.copy2` 拷贝到 `cache_dir` 等所有"重活"路径在 ENOSPC 发生前均无任何预检查；磁盘满时 Python 抛裸 `[Errno 28] No space left on device`，placeholder 只显示这一行错误，同时 `cache_dir` 已被部分写入的脏数据撑满，可能波及同主机其它服务。
  本版在 `pixivfeed/utils.py` 新增 `MIN_FREE_DISK_BYTES = 500 MB` 与 `check_disk_free` / `format_disk_full_message` 共享 helper；`pixivfeed/channel/telegram/handlers.py` 新增 `_gate_disk_space()` 并在所有任务入队前调用一次（贴链接、`/archive`、`/zip2tph`、eh/ex 模式按钮回调等共 7 处入口），cache_dir 所在挂载点剩余空间不足 500 MB 时直接 edit placeholder 给中文提示并 return，避免顶满磁盘。`/zip2tph` 因已知 `document.file_size`，按 2×file_size + 500 MB 余量做复合预估（覆盖 zip 接收 + 解压同时落盘的峰值）。

### Docs
- **README.md 重构**。原 README 把功能清单铺成一长串，既不突出价值也不提醒部署资源需求。重写后开头一句话概括用途 + 5 条特点；新增「部署要求」表格明确列出磁盘空间（建议 ≥50 GB）、公网域名、Nginx 等硬性需求；「快速开始」精简为只列最少必填项；散落的命令整合到「其它命令」表，避免新用户首屏被淹没。
- **docs/DEPLOY.md 重写**。删除内部架构描述（ASCII 流程图、Provider/Registry 等实现层概念），改为以行为视角描述；大量口语化措辞改为书面化（"deploy 用户被打穿也接管不了系统" → "即使 deploy 账号被攻陷，攻击者也无法通过该账号取得系统控制权" 等）；R2 配置示例补全 v0.8.2 引入的 `prefix` 与 v0.8.1 的 `max_upload_size_gb`。
- **「GitHub Webhook 自动部署」节默认折叠**（`<details>/<summary>`）。该机制本身不绑定原仓库，fork 用户替换域名、SECRET、仓库地址等自有值即可使用；折叠避免对仅做基础部署的用户造成干扰。
- **去除仓库内硬编码的私人域名 `feed.fengshengxiu.club`**。`docs/DEPLOY.md`（5 处）、`deploy/feed-bot-webhook.nginx.conf.example`（2 处）、`deploy/feed-bot-webhook.service`（1 处）统一替换为 `<your-domain>` 占位符。

### 改动文件
- `pixivfeed/utils.py`：新增 `MIN_FREE_DISK_BYTES` / `disk_free_bytes` / `check_disk_free` / `format_disk_full_message`
- `pixivfeed/channel/telegram/handlers.py`：新增 `_gate_disk_space()` + 7 处入队前接入
- `README.md`、`docs/DEPLOY.md`：重写
- `deploy/feed-bot-webhook.nginx.conf.example`、`deploy/feed-bot-webhook.service`：私人域名替换为占位符
- `pyproject.toml`：version 0.8.2 → 0.8.3

## v0.8.2 — 2026-05-17

R2 持久化语义闭环 + 扫描完整性防御 + `--r2` 工作流修复。

修复一类系统性 bug：R2 上传失败时 publisher 静默回退 nginx，但 `telegraph_cache` 仍永久写入，7 天后本地缓存清理让 Telegra.ph 页面里的图变 404，**而 cache 命中仍返回旧 URL，无路径自愈**。本版本让 cache 反映真实持久化状态，让 `--r2` 命令的承诺与实际行为一致。

### Features
- **`storage.r2.prefix` 配置项**：为 R2 bucket 内对象统一加前缀（如 `feedbot/`）。配置后所有 upload / public_url / list / delete / LRU 都局限在此前缀下，避免共用 bucket 时误删其他对象。空时启动 warning（v0.9.x 计划默认拒绝）。
- **`PublishResult.durable` 与 `fallback_reason` 元数据**：发布结果新增 `r2_image_count` / `fallback_image_count` / `fallback_reason` / `durable` 字段，`fallback_reason` 包含 `r2_disabled` / `size_guard_skipped` / `r2_batch_failed` / `r2_partial` / `local_file_missing` 完整枚举。
- **完成消息差异化风险提示**：根据 `fallback_reason` 显示不同文案（"体积超护栏" / "R2 上传失败" / "部分图片仍依赖本地缓存" 等）。**R2 未启用时一律不弹提示**，避免默认部署用户每次发布都看到吓人警告。
- **`--r2` 真正绕过非 durable 缓存**：admin 加 `--r2` 时 cache 命中非 durable 行视为 miss 触发重发并 upsert；命中 durable 行追加"已是 R2 durable 缓存，跳过重发"提示。R2 未启用 / client 未初始化时 `--r2` 在 cache gate 上视为 False，避免无效空转。
- **`/stats system` 接通 Telegraph cache durability 渗透率**：展示 `total / durable / legacy` 计数与 `fallback_reason` 分布，让管理员可见修复效果。
- **`/stats system` 暴露 R2 扫描健康度**：`last_scan_success_at` / `stale` / `failures_24h`（rolling 24h, since process start）。
- **Pixiv novel 接入 R2 持久化**：封面、textEmbeddedImages、`[pixivimage:]` 引用图统一走共享 `resolve_image_urls` helper 上传 R2，cache 行带真实 durability 元数据。novel 也支持 `--r2` 强制重发。
- **`deploy/cleanup.py` 读 SQLite runtime_settings 覆盖**：`/setting set storage.cache_days N` 修改的值现在对 cleanup timer 也生效。DB 缺失 / 表缺失静默回 YAML；脏值 warning + 回 YAML（不静默吞，便于排查）。

### Fixes
- **R2 扫描部分失败不再静默污染 stats**：`R2Client.list_all()` 中途网络异常或 HTTP 非 200 时抛 `R2ListIncomplete` 异常携带已扫部分 + 失败原因（不再 break 返回部分结果）。后台 LRU loop / `/stats r2_evict` 统一 catch：不刷新 stats（沿用旧 snapshot）、**跳过本轮 evict**、写 stale meta + 计入 24h 失败 deque。避免在不完整数据上做 LRU 决策误删 hot key。
- **R2 LRU prefix 一致性**：`lru_evict_to_target` 与 `list_all` 删除 prefix 参数，统一由 `R2Client.prefix`（构造时配置）决定扫描范围；`delete_object` 接收 list 返回的 absolute key，避免对已含 prefix 的 key 二次拼接。
- **空 `r2_key` 不上传 R2**：resolver 把无法推导 r2_key 的项标记为"不上传"直接走 fallback，避免把空 key PUT 到 bucket 根 / `prefix/` 自身造成冲突。

### Internal
- **新增 `pixivfeed/publisher/_resolver.py`**：`ResolveItem` / `ResolveResult` / `resolve_image_urls()` —— gallery / novel 共享的 "本地路径 → R2/fallback URL" 决策层，包含完整 fallback reason 计算与 size_guard。
- **`telegraph_cache` schema migration**：`Database.connect()` 内 `executescript(SCHEMA)` 之后调 `_migrate_schema()` 用 `PRAGMA table_info` 幂等补列。新增 4 列：`durable INTEGER NOT NULL DEFAULT 0` / `r2_image_count INTEGER` / `fallback_image_count INTEGER` / `fallback_reason TEXT`。legacy 行的 `r2_image_count IS NULL` 与 "已知 0" 严格区分。
- **`TelegraphCache.get()` 返回 `CacheEntry`**（dataclass：url / page_count / durable / r2_image_count / fallback_image_count / fallback_reason / created_at），breaking 内部 API，handlers 所有 6 处 cache.get 调用点同步改完。
- **`TelegraphCache.put()` 接受 keyword-only durability 参数**；handlers 4 处 cache.put 全部接通。
- **`TelegraphCache.stats()`**：聚合 `total / durable / legacy` 与 `fallback_reason` 分组计数，供 `/stats system` 查询。
- **`R2Client._normalize_key()` / `_normalize_prefix()`**：所有出入 R2 的 key 统一走 normalizer；调用方传相对 key（如 `pixiv/12345/0.jpg`），不感知 prefix。
- **`R2ListIncomplete` 异常类**：携带 `partial_keys` / `scanned_pages` / `cause`。
- **handlers 新增 `_effective_force_r2(context, force_r2)`** helper：统一 cache gate 上的 force_r2 判定，避免 R2 未启用时 admin --r2 无意义绕过 cache 重发。
- **handlers 新增 `_record_r2_scan_failure` / `_record_r2_scan_success`** helper：让后台 LRU loop 与手动 `/stats r2_evict` 共享同一份 `bot_data["r2_stats_meta"]`，避免状态漂移。
- **24h rolling 失败计数器**：`collections.deque[float]` 模式，写读两端都裁剪 >24h 项，文案明确 "since process start"。
- **`apply_runtime_overrides(cfg, db_path)` 同步 helper**（`pixivfeed/config.py`）：用 sqlite3 标准库 + `PRAGMA query_only=1`，cleanup.py 等同步脚本不引入 aiosqlite。bool 解析严格白名单（`"abc"` 不再被静默归零）。
- **`PixivProvider.publish_novel(force_r2=...)`**：novel publisher 暴露 force_r2 参数与 publish_gallery 一致。

### 改动文件
- 新增：`pixivfeed/publisher/_resolver.py`
- 改动：`pixivfeed/storage/{r2.py, db.py, cache.py, __init__.py}`
- 改动：`pixivfeed/publisher/telegraph.py`
- 改动：`pixivfeed/provider/pixiv/novel_publisher.py`
- 改动：`pixivfeed/channel/telegram/{bot.py, handlers.py}`
- 改动：`pixivfeed/config.py`、`pixivfeed/__main__.py`、`deploy/cleanup.py`、`config.example.yaml`

### 升级注意
- **schema 自动迁移**：升级首次启动会自动 `ALTER TABLE telegraph_cache ADD COLUMN` 补 4 列，旧条目 `durable=0` 且 `r2_image_count IS NULL`（legacy 行）。
- **legacy 缓存条目保守策略**：普通用户命中 legacy 行正常返回旧 URL；admin `--r2` 命中视为 miss 触发重发。避免升级当天热门作品集中重发引起 telegra.ph / Pixiv 限流。
- **共用 R2 bucket 用户建议配置 `storage.r2.prefix`**：留空时启动会 warning，LRU 仍按整 bucket 扫描/驱逐（兼容存量部署）。已配置 prefix 后**新上传走 prefix，旧对象不再被 LRU 看到**——需要时请手动迁移或一次性清理。

## v0.8.1 — 2026-05-16

R2 体验完善 + 体积护栏 + zip2tph 接通。

### Features
- **R2 单次发布体积护栏**：`storage.r2.max_upload_size_gb`（默认 1.0 GB）。一次发布总字节超过阈值时跳过 R2 走 nginx + 7 天 `cache_days`；完成消息追加"⚠️ 此 Telegra.ph 因体积过大未上传 R2... 最短 7 天 最长 30 天后图片可能失效"。设 0 关闭护栏（任意大小都上 R2）。
- **管理员 `--r2` 强制标志**：admin 在以下入口可加 `--r2` / `--force-r2` 绕过护栏强制上 R2：
  - 粘贴 eh/ex/nh/pixiv 链接的普通消息
  - `/pixiv_telegraph <url> --r2`
  - `/pixiv_direct <url> --r2`
  - `/zip2tph` 文档 caption 或 reply 命令加 `--r2`
  - `/ehsearch <关键词> --r2`（影响整个搜索会话内所有 [打开] 点击）
  - 普通用户用 `--r2` 静默忽略，不暴露命令存在。
- **`/zip2tph` 接通 R2**：原来 zip2tph 绕开 `publisher.publish_gallery` 自己手写 chunks + create_page 循环，**发布的 Telegra.ph 完全没经过 R2 路径**——7 天 cache_days 后必失效。本版改用 `publish_gallery`：自动接通 R2 上传、进度条、size guard、nginx fallback。代码减少。cache_dir 拷贝仍保留作 R2 失败/护栏跳过时的源（双份磁盘占用 7 天后清）。
- **R2 上传进度可见**：publish 阶段 placeholder 消息显示"⏳ 上传 R2 (k/N)"，每完成一张 PUT 更新一次。原本 25 张图上传 5-10 秒对用户是黑洞。
- **`/stats system` 显示 R2 用量**：bucket / 占用 / 容量百分比 / 对象数 / LRU 阈值 / 最旧+最新对象时间（UTC+8）/ 上次扫描时间。读 `bot_data["r2_stats"]` 缓存毫秒级返回，不每次都重扫。
- **`/stats r2_evict`** admin 调试命令：立刻扫一次 R2 + 触发 LRU 清理，结果（清理摘要 + 当前用量）直接显示在同一条消息上。

### Internal
- `GalleryImage` 加 `r2_key: str | None` 字段。调用方显式指定时 publisher 优先用；为空时仍按 `local_path.relative_to(cache_dir)` 推导。zip2tph 这种 cache_dir 外的图必须显式指定。
- `PublishResult` 加 `r2_skipped_reason: str` 字段。非空时调用方应在完成消息后追加用户提示。`_r2_skipped_suffix(pub)` helper 统一生成。
- `_Pending` / `_SearchState` 加 `force_r2: bool`，让按钮回调能跨越事件边界透传 admin --r2 标志。
- 新增 `_parse_r2_flag(args, user_id, admin_users) -> (filtered_args, force)` helper，统一所有 cmd 入口的 flag 处理。
- 后台 `_r2_lru_loop` 改造：每轮先 `list_all` → 聚合 `R2StatsSnapshot` 塞 `bot_data["r2_stats"]` → 用同一份 objects 喂给 `lru_evict_to_target(objects=...)`（避免扫两次）。开机 30 秒（原 5 分钟）后跑首轮。
- `lru_evict_to_target` 加 `objects` 可选参数复用调用方刚扫过的列表。
- 新增 `R2StatsSnapshot` dataclass + `stats_from_objects(objects)`。
- `upload_files_concurrent` 加 `on_progress(done, total)` 异步回调（asyncio.Lock 保护 counter）。
- R2 时间戳统一显示 UTC+8（`_fmt_utc8` helper）。
- `/stats` help 列表加 `r2_evict` 子命令。

### Fixes
- **`storage.r2.enabled: true` 启动崩溃**（`AttributeError: 'dict' object has no attribute 'enabled'`）。`Config._from_dict` 里 `StorageConfig(**...)` 把 yaml 解析出的 `r2:` 嵌套 dict 原样塞进 `r2` 字段，没构造成 `R2Config` 实例。修复：拆开 `storage` 段处理，先 `pop('r2', None)` 单独 `R2Config(**r2_raw)` 再传给 `StorageConfig`。

### 向后兼容
- R2 仍是 opt-in。未启用时所有行为跟 v0.7.1 完全一致。
- `--r2` 是新增 flag，老命令调用方式不变。
- zip2tph 接 publish_gallery 后老用户感知不到差异，只是 telegra.ph 现在能持久化（前提是启用了 R2）。

### 改动文件
- `pixivfeed/config.py`（R2Config + StorageConfig 嵌套构造修复 + max_upload_size_gb）
- `pixivfeed/storage/r2.py`（R2StatsSnapshot / stats_from_objects / upload progress / objects 复用）
- `pixivfeed/storage/__init__.py`（re-export）
- `pixivfeed/provider/__init__.py`（GalleryImage.r2_key）
- `pixivfeed/publisher/telegraph.py`（_resolve_image_urls + force_r2 + r2_skipped_reason + size guard）
- `pixivfeed/channel/telegram/bot.py`（_r2_lru_loop 改造）
- `pixivfeed/channel/telegram/handlers.py`（force_r2 透传 + zip2tph 接通 + stats UX）
- `config.example.yaml`、`README.md`、`docs/DEPLOY.md`


## v0.8.0 — 2026-05-15

Telegra.ph 发布走 Cloudflare R2 / 任意 S3 兼容对象存储（opt-in），解决"大画廊 / 冷链接几天后部分 `<img>` 加载失败"。

### 背景

v0.7.x 以及之前，Telegra.ph 页面里的 `<img src>` 指向 bot 服务器自己的 nginx（反代 `cache_dir`）。`cache_dir` 默认 7 天 TTL 清理；CF 边缘缓存能续命到 ~30-40 天；但**早期**没人访问的页面 / 长画廊里部分图过冷被驱逐时，边缘 cache miss 回源 → nginx 已删 → 404 → telegra.ph 页面渲染卡死。

实际表现就是用户报的"部分大 telegraph 无法即时查看"。

### 变更

- **新增 `storage.r2` 配置段**（默认 `enabled: false`）：endpoint / bucket / access_key / secret / custom_domain / capacity_gb / lru_check_interval_minutes。具体字段含义见 `config.example.yaml` 注释。
- **publisher 上传 R2 后用 R2 URL 喂给 Telegra.ph**。`TelegraphPublisher` 接受 `r2_client` 注入；`publish_gallery` 内部并发上传（默认 8 并发）；URL 切到 R2 自定义域名；单图上传失败该图回退到 nginx URL；整批异常时整批回退。**发布永不因 R2 故障而失败。**
- **R2 key 跟 cache_dir 相对路径完全一致**（例：`eh_3936793_d89fc4d30a_page_sample/p0.jpg`），方便对账 + 调试。
- **新增后台 LRU eviction task**。R2 启用且 `capacity_gb > 0` 时，每 `lru_check_interval_minutes` 跑一次 `ListObjectsV2` 全扫；用量超过 `capacity_gb × 0.9` 触发清理，按 LastModified 升序删到 `capacity_gb × 0.7`。R2 不记 access_time，按上传时间近似 LRU。

### 向后兼容（重要）

- **R2 是 opt-in 增强，本地缓存 + nginx 反代仍是默认且一等公民。** clone 本项目什么都不动，行为跟 v0.7.1 完全一致——`r2.enabled` 默认 `false`，publisher 路径里第一行就 short-circuit 出去。
- 老 Telegra.ph 链接（v0.7.x 及之前发布）`<img src>` 写死指向 nginx，本版**不动**它们；如要拯救老链接需要后续单独 PR（涉及 telegra.ph editPage API + 回填 R2）。
- 不依赖任何新 PyPI 依赖。R2 客户端是自写 S3 sigv4 协议（~150 行，覆盖 PUT/HEAD/GET-LIST/DELETE），跑过 AWS sigv4 docs 的 well-known derivation example 校验。

### 新增文件 / 改动文件

- `pixivfeed/storage/r2.py`（新增，~340 行）：`R2Client` + `upload_files_concurrent` + `lru_evict_to_target`。
- `pixivfeed/config.py`：新增 `R2Config` dataclass + 嵌入 `StorageConfig.r2`，扩展 `SENSITIVE_KEYS` + `_validate` enabled 时必填检查。
- `pixivfeed/publisher/telegraph.py`：`TelegraphPublisher` 加 `r2_client` 参数；新增 `_resolve_image_urls` 决定喂给 telegra.ph 的 URL（R2 / fallback）。
- `pixivfeed/__main__.py`：根据 `storage.r2.enabled` 实例化 `R2Client`，注入 publisher + init_bot_async。退出时 `aclose()` 关 httpx pool。
- `pixivfeed/channel/telegram/bot.py`：`build_application` / `init_bot_async` 多接 `r2_client` keyword-only 参数；启用时挂 `_r2_lru_loop` 后台任务（开机 5 分钟稳定期后开始）。
- `config.example.yaml`：示例 + 注释。
- `pyproject.toml`：version 0.7.1 → 0.8.0。

### 部署

- 不开 R2：什么都不用做。自动部署 pull 后 `pip install -e .` 重启即可（pyproject 变了所以会触发）。
- 开 R2：
  1. CF Dashboard → R2 → Create bucket（建议 location 选离你 bot 服务器近的）
  2. bucket Settings → Public access → Connect Custom Domain（不要用 `pub-xxx.r2.dev`，有 ratelimit 不能给 telegra.ph 用）
  3. R2 → Manage API Tokens → Create with "Object Read & Write" + 只授本 bucket
  4. 编辑 `/etc/pixiv-feed-bot/config.yaml` 加 `storage.r2:` 段，`enabled: true` + 填上面拿到的 endpoint / bucket / access_key / secret / custom_domain
  5. `systemctl restart pixiv-feed-bot`
  6. journal 应见 `R2 enabled: bucket=xxx, public=https://...`；发一个画廊看 telegra.ph 页面 `<img src>` 是否指向 R2 域名。

### 已知限制

- v0.7.x 及之前发布的 Telegra.ph 链接里 `<img src>` 仍指 nginx，没法批量改。bot 仍跑 nginx + cache_dir，老链接能撑多久就撑多久。
- LRU 按"上传时间"近似，不是"最后访问时间"——R2 / S3 不在协议层记 access_time。对你的使用场景（旧画廊基本没人翻）是合理近似。


## v0.7.1 — 2026-05-15

`/ehsearch` 后续 UX 反馈迭代 + eh/ex 详情卡统一 + tag 中文翻译。

### 变更（UX）

#### v0.7.0 落地反馈三件套
- **L2 详情卡改为同消息 edit**。点搜索结果里的 [打开 #N] 不再 reply 新消息，搜索消息**就地** edit 成「作品详情卡 + 6 按钮」：4 个发布模式（网页·显示图 / 网页·原图 / 归档·1280x / 归档·原图，走现有 `eh:` 流程 → Telegra.ph）+ [📦 归档下载（zip）]（进 L3）+ [⬅ 返回搜索结果]（回 L1）。整个三层 UX 始终在同一条消息上 edit。
- **新增 L3 zip 选单**。详情卡上点 [归档下载] → 同消息 edit 成 4 模式选 zip 的菜单（走现有 `eha:` 流程），加 [⬅ 返回详情] 可退回 L2。点了模式按钮跑完时直接发 zip document + 把消息 edit 成完成通知，行为对齐 `/archive`。
- **搜索结果列表加 [◀ 上一页]**。`SearchResultPage.prev_url` 在 v0.7.0 已经解析但没渲染按钮；本版同时挂在末行（首页时该按钮自然不显示）。`_handle_ehs_next` / `_handle_ehs_prev` 共享 `_ehs_navigate(direction=...)` helper。
- **tag 顺序重排**。每条搜索结果改为 `类型 · 语言 · N 页 · 优选 tag`。语言从 `language:*` tag 抽取，`translated/rewrite/speechless` 修饰符跳过；都没匹到的画廊默认 `japanese`。tag 区去掉 `language:` 项避免重复。

#### 链接流与搜索流详情卡 UX 统一
- **`EHGallery` 扩展 `category` + `tags` 字段**。`_fetch_gallery_meta` 用 bs4 解析详情页 `#gdc .cs`（分类）+ `#taglist table tr`（按 namespace 分组的全部 tags）。解析失败兜底为空，不影响下载流程。
- **共享 `_render_eh_detail_card`**。链接流（`_eh_offer_modes` / `_eh_offer_modes_for_archive`）+ 搜索 L2 + 搜索 L3 现在都调用同一个渲染函数：标题 + 「类型 · 语言 · N 页」meta 行 + 「全部 tag 按 eh 顺序分组」的 TG `<blockquote expandable>` 折叠区。粘 eh 链接的体验 == 从 /ehsearch 点进去的体验。
- **搜索 L1 列表 tag 翻译**。`_select_display_tags` 拆出 `_pick_display_tag_pairs` 保留 namespace，渲染时按 (ns, value) 走翻译。

#### tag 中文翻译数据库集成
- **新增 `pixivfeed/provider/ehentai/_tagdb.py`**：`EHTagDB` 类下载 [EhTagTranslation/Database](https://github.com/EhTagTranslation/Database) 的 `db.text.json`（GitHub Release，~1MB），缓存到 `{db_path.parent}/ehtagdb.json`，<30 天有效。
- **启动时异步加载**：`init_bot_async` 里 `asyncio.create_task(tagdb.load())` fire-and-forget，**不阻塞 bot 上线**。加载完成前所有 `translate*()` 调用 safe fallback 返回原文。GitHub 拉不到（网络问题）只 log warning，bot 照常运行。
- **手动预放缓存**（可选）：服务器拉不到 GH 时可以本机下载后 `scp` 到 `/var/lib/pixiv-feed-bot/ehtagdb.json`，bot 启动直接读本地。
- **翻译范围**：详情卡的完整 tag 折叠区（namespace 中文名 + value 中文名）+ 搜索 L1 列表的精简 tag。category 用硬编码字典（10 项）翻译，不走数据库（更稳）。

### 内部
- 新增 `_render_detail_card` / `_render_archive_menu` / `_make_pending_for_item`。每次进 L2 或返回 L2 时生成新 `_PENDING` token（旧 token 由现有 `_gc_pending` 清理）。
- 新增 callback handler：`_handle_ehs_arch_menu` / `_handle_ehs_back2list` / `_handle_ehs_back2det` / `_handle_ehs_prev`。`handle_callback` dispatch 加 4 个新前缀分支。
- 旧的 `_handle_ehs_arch` 保留，仅给 v0.7.0 部署期间生成的"Telegra.ph 完成消息底部 [归档下载]"按钮做 backward compat（按钮自身 TTL ≤ 10 分钟自然过期）。
- `_ehsearch_dispatch` 新增 `prev_param` 参数。
- 共享渲染常量：`_EH_NAMESPACE_ORDER` / `_EH_NAMESPACE_ZH_FALLBACK` / `_EH_CATEGORY_ZH` / `_EH_NS_MAX_VALUES_IN_BLOCKQUOTE`。

### 已知限制
- 仍不支持 `f_cats=` / `advsearch=1` / `f_sname` 等高级搜索 filter
- inline mode 搜索保留给 wikipedia
- ehtagdb 不带版本通知机制——cache 30 天后自动重拉，期间数据库新增条目暂时拿不到翻译


## v0.7.0 — 2026-05-15

### 新增
- **`/ehsearch <关键词>`** —— 直接在聊天里搜索 e-hentai / ExHentai 画廊，免去打开浏览器。站点选择策略：账号 ipb cookie 配齐时优先用 ExHentai；cookie 失效（HTTP 302/404 或空 body）自动回落到 e-hentai。结果纯文本展示前 10 条（每条 1 行：标题 / 页数 / 分类 / 优选 tag），按钮区每条 1 个 `[打开]`；末行可选 `[展开全部]`（看完 25 条）和 `[下一页 ▶]`。三层 UX：搜索 → 点 `[打开]` 走 `EHMode.PAGE_SAMPLE`（网页·显示图）→ Telegra.ph 发完后消息末尾挂 `[📦 归档下载]` → 点了进入 `/archive` 同款 4 模式按钮，按需挑 zip 模式。
- 搜索行为计入 stats：新增 [`KIND_EH_SEARCH`](pixivfeed/storage/usage.py)，`/stats` 会自动出现一行 "eh/ex 搜索"，不需要单独改聚合逻辑。
- `bot.py` 命令菜单（`PUBLIC_COMMANDS`）追加 `BotCommand("ehsearch", ...)`；`/start` `/help` 文本同步更新。

### 变更
- `_eh_run_with_mode`（[handlers.py](pixivfeed/channel/telegram/handlers.py)）新增可选 kwarg `extra_buttons: list[InlineKeyboardButton] | None`，发布完成（含缓存命中、fallback 后命中）后会把按钮挂在 Telegra.ph URL 消息底部一行。仅 `/ehsearch` 流程用，默认 `None` 时行为完全不变。
- `_eh_offer_modes_for_archive`（[handlers.py:1966](pixivfeed/channel/telegram/handlers.py)）拆出 `_eh_offer_archive_modes_on_placeholder` 内层函数，接受现成的 placeholder + user_id，供 `ehs_arch` callback 复用；`/archive` 入口走 1 行壳行为零变化。

### 风险说明
- 当前使用强度（≈3 次/天）下，对 cookie 持续性影响接近 0：搜索只读 cookie；登录账号 page-load quota ≈10000/24h，差 3 个数量级；image hits 与搜索缩略图独立配额不冲突；搜索不扣 GP。唯一非零风险是 bot 未来对外开放后被滥刷——可在 `cmd_ehsearch` 入口加 per-user rate limit（保留为未来 PR）。

### 已知限制
- 不支持高级 filter（`f_cats=` 分类位、`advsearch=1`、`f_sname/f_stags`）—— 等用着需要再补 `--cats=` 类 CLI flag
- inline mode 搜索（`@bot keyword`）保留给 wikipedia，不扩展
- 缩略图 preview 故意省略：保持纯文本结果列表，节省流量 + 避免 25 次 thumbnail fetch 拉慢响应

### 改动文件
- `pixivfeed/provider/ehentai/_search.py`：新增 parser + `search_eh()` async 入口
- `pixivfeed/provider/ehentai/__init__.py`：re-export 6 个搜索符号
- `pixivfeed/storage/usage.py` + `pixivfeed/storage/__init__.py`：新增 `KIND_EH_SEARCH = "eh_search"` + 中文映射 + `__all__`
- `pixivfeed/channel/telegram/handlers.py`：新增 `_SearchState` / `_SEARCH_STATES` / `cmd_ehsearch` / `_handle_ehs_open/_more/_next/_arch` / `_render_search_message`；`_gc_pending` 加入新 state TTL；`_eh_run_with_mode` 加 `extra_buttons` 参数；`_eh_offer_modes_for_archive` 拆出内层；`handle_callback` 加 4 个分发分支；`__all__` 加 `cmd_ehsearch`；`/start` 文本补一行
- `pixivfeed/channel/telegram/bot.py`：`PUBLIC_COMMANDS` 加 `BotCommand("ehsearch", ...)`；`build_application` 加 `CommandHandler("ehsearch", cmd_ehsearch)`
- `README.md`：「核心功能」段补 "eh 关键词搜索" 一条
- `pyproject.toml`：`version` 0.6.1 → 0.7.0


## v0.6.1 — 2026-05-14

### 新增
- 部署文档新增「启用缓存清理 timer」一段：`storage.cache_days` 一直只是配置项摆设，因为 `deploy/pixiv-feed-bot-cleanup.{service,timer}` 没有 enable 步骤——timer 不启用，cleanup.py 永远不会跑，缓存目录会一直堆。`docs/DEPLOY.md`「systemd 服务」一节补完整 `cp → daemon-reload → enable --now → list-timers → 手动 start 验证`流程；`config.example.yaml` 同步把"实现待添加"的旧注释改成指向 timer。
- 新增 `pixivfeed/channel/telegram/constants.py`，把 [handlers.py](pixivfeed/channel/telegram/handlers.py) 顶部的 `TG_DOCUMENT_LIMIT` / `LOCAL_BOT_API_DOCUMENT_LIMIT`、[handlers.py:_PENDING_TTL](pixivfeed/channel/telegram/handlers.py)、[handlers.py:_gc_pending](pixivfeed/channel/telegram/handlers.py) 里 hardcoded 的 cancel-token TTL（`3600`），以及 4 处 `read_timeout=3600 / write_timeout=3600` 上传超时统一成 `TG_UPLOAD_TIMEOUT`。改这类阈值不再需要 grep 全仓库。
- `job_queue` 段新增到 `Config` / `config.example.yaml`。原本 [bot.py](pixivfeed/channel/telegram/bot.py) 里硬编码的 `archive_zip=1 / zip2tph=1 / direct_image=2 / telegraph_publish=3` 现在从 yaml 读，小内存机器（<2GB）可以全调成 1，大机器可以放开。`_validate` 拒绝 `< 1`。该字段是基础设施级，不进 `RUNTIME_KEYS`——worker 启动后并发数固定，改完得重启 bot。

### 变更
- 移除未使用依赖 `orjson>=3.10`（pyproject.toml）——仓库里没有任何模块 import 它，从 v0.4 起一直空挂在那里。

### 修复
- `deploy/feed-bot-webhook.service` 里的 `NoNewPrivileges=yes` 和脚本里的 `sudo systemctl restart pixiv-feed-bot` 天然冲突——sudo 是 setuid 二进制，这条 hardening 直接让它拒绝提权，所有 webhook 触发的部署都卡在 restart 那一步报「sudo: The "no new privileges" flag is set」。删除该行；其它 hardening（ProtectSystem / ProtectHome / PrivateTmp / ReadWritePaths）保留。注释里写明这条不能加。
- DEPLOY.md 排错表补一条对应症状的修法。

### 变更（部署）
- 部署 webhook 通知现在默认从 `/etc/pixiv-feed-bot/config.yaml` 读 `telegram.token` / `auth.admin_users[0]` / `telegram.base_url`，省去再单写一份 `/etc/feed-bot-webhook/env`。env 文件保留作为 override（写另一个 bot/admin 接 deploy 噪音时用）。要求 deploy 用户能读 config.yaml（chgrp pixivbot + chmod 0640 + usermod -aG）。
- 通知正文加：版本号变化（`0.6.0 → 0.6.1`）、HEAD 短哈希、`git tag --points-at` 命中的 tag、commit 列表（最多 15 条）、`git diff --shortstat` + 文件列表（最多 12 个）。失败通知额外保留尾部错误日志段。从此打开 admin 私聊就能直接看到这版部署到底发生了什么，不必再 ssh 登服务器看 journal。

### 改动文件
- `pyproject.toml`：`version` 0.6.0 → 0.6.1；移除 `orjson` 依赖
- `pixivfeed/channel/telegram/constants.py`：新增
- `pixivfeed/channel/telegram/handlers.py`：删本地 `TG_DOCUMENT_LIMIT` / `LOCAL_BOT_API_DOCUMENT_LIMIT` / `_PENDING_TTL` 定义，改从 constants import；4 处 `read_timeout=3600 / write_timeout=3600` 改成 `TG_UPLOAD_TIMEOUT`；`_gc_pending` 里 cancel-token 过期阈值改 `CANCEL_TOKEN_TTL`
- `pixivfeed/channel/telegram/bot.py`：`job_queue.register(...)` 的并发数从 `config.job_queue` 读
- `pixivfeed/config.py`：新增 `JobQueueConfig` dataclass；`Config.job_queue` 字段；`_from_dict` 读 yaml；`_validate` 拒绝并发 < 1
- `config.example.yaml`：新增 `job_queue` 段；`cache_days` 注释改成指向 cleanup timer
- `docs/DEPLOY.md`：新增「启用缓存清理 timer」步骤
- `deploy/feed-bot-deploy.sh`：新增 config.yaml 默认凭据读取（venv python + PyYAML），新增 `build_summary` helper；`notify` 支持 base_url

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
