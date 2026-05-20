# Feed Bot

在 Telegram 里贴一个 Pixiv / e-hentai / ExHentai / nhentai 链接，Bot 自动把作品变成可直接阅览的图片或 Telegra.ph 页面——不用离开聊天窗口，不用打开浏览器。

## 特点

- **零命令触发**：贴链接即响应，不需要记 `/` 命令。
- **自动选择最佳展示方式**：图片少时直发到聊天（保留原图质量），多时生成 Telegra.ph 长页面。
- **私聊可选模式、群聊全自动**：私聊提供按钮选取下载模式；群聊静默按预设模式处理，不打扰对话。
- **运行时热配置**：绝大多数参数通过 `/setting` 私聊修改即时生效，不需要 SSH 上去重启。
- **可选持久化存储**：启用 Cloudflare R2 后图片永不过期；不启用则走本地缓存 + 定时清理，同样能正常使用。

## 部署要求

| 条件 | 说明 |
| --- | --- |
| Python ≥ 3.11 | 运行环境 |
| 公网可达的域名 + Nginx | 将本地图片缓存暴露为 HTTPS 静态文件服务，供 Telegra.ph 服务器拉取 |
| 磁盘空间 | 图片缓存（默认保留 7 天）视使用频率可达 **数十 GB**。归档下载 / zip2tph 涉及 GB 级临时文件，建议至少预留 **50 GB** 可用空间 |
| Telegram Bot Token | 通过 [@BotFather](https://t.me/BotFather) 申请 |
| Pixiv PHPSESSID（可选）| 访问 R-18 或受限小说时需要 |
| 本地 Bot API（可选）| 处理 >50 MB 文件时需要，详见 [部署指南](docs/DEPLOY.md#本地-bot-api) |
| Cloudflare R2（可选）| 让 Telegra.ph 页面图片永久可用，免受 7 天缓存 TTL 限制 |

## 快速开始

```bash
git clone https://github.com/StudyingPy/shengxiu_feedbot.git
cd shengxiu_feedbot
pip install -e .
cp config.example.yaml config.yaml
```

编辑 `config.yaml`，至少填写：

- `telegram.token` — Bot Token
- `auth.admin_users` — 管理员的 Telegram user ID
- `publish.base_url` — Nginx 暴露图片缓存后的 HTTPS 前缀（如 `https://example.com/p`）
- `storage.cache_dir` — 图片缓存目录（Nginx alias 指向的同一路径）

然后启动：

```bash
python -m pixivfeed
```

首次启动会自动创建 Telegra.ph 账号并将 token 写回 `config.yaml`。

完整的 Nginx 配置、systemd 服务、自动部署、R2 对象存储、本地 Bot API 搭建等详见 **[部署指南](docs/DEPLOY.md)**。

## 支持的站点

| 站点 | 内容类型 | 是否需要登录 |
| ---- | -------- | ------------ |
| Pixiv | illust（插画）/ novel（小说）| 仅 R-18 与部分小说需要 PHPSESSID |
| e-hentai | gallery | 不需要 |
| ExHentai | gallery | 必须（ipb_pass_hash / ipb_member_id / igneous）|
| nhentai | gallery | 不需要 |

e-hentai / ExHentai / nhentai 默认禁用，启动后通过私聊命令开启即可（无需重启）：

```
/setting set collectors.ehentai.enabled true
/setting set collectors.nhentai.enabled true
/setting edit collectors.exhentai.ipb_pass_hash
/setting edit collectors.exhentai.ipb_member_id
/setting edit collectors.exhentai.igneous
/setting set collectors.exhentai.enabled true
```

## 用法

### Pixiv

- **插画**：图片数 ≤ `direct_threshold`（默认 5）时直接发图，超过则生成 Telegra.ph 页面。
  - 直发上限 10 张（Telegram API 限制），超出自动转 Telegra.ph。
  - `/pixiv_telegraph <链接>` 强制走 Telegra.ph；`/pixiv_direct <链接>` 强制直发。
- **小说**：自动转为 Telegra.ph 页面，支持 `[newpage]`、`[chapter:]`、`[[jumpuri:>]]`、`[pixivimage:]` 等 Pixiv 小说标记。

### e-hentai / ExHentai

私聊中发送链接后会显示标题与页数，并提供四个模式按钮：

| 按钮 | 说明 |
| ---- | ---- |
| 网页 · 显示图 | sample 图，不消耗 GP / Credits |
| 网页 · 原图 | 原始分辨率，消耗 GP / Credits |
| 归档 · 1280x | archiver.php 获取 zip，1280x 重采样，消耗免费 archive 配额 |
| 归档 · 原图 | 同上，原始分辨率 |

群聊中不弹按钮，按 `collectors.{ehentai|exhentai}.default_mode` 配置的默认模式自动处理。

### nhentai

通过第三方 API 获取数据并直接生成 Telegra.ph 页面，支持多 CDN 自动 fallback。

### 其它命令

| 命令 | 说明 |
| ---- | ---- |
| `/archive <链接>` | 将作品打包为 zip 发送（eh/ex 支持四种下载模式）|
| `/ehsearch <关键词>` | 搜索 e-hentai / ExHentai 画廊，结果以按钮列出 |
| `/zip2tph` | 将上传的图片 zip 包发布为 Telegra.ph 页面 |
| `/wiki <词条>` | 查询中文维基百科 |
| `/stats` | 用量统计（支持 `7d`、`user @x`、`chat <id>`、`system` 等子命令）|

## 管理命令

| 命令 | 说明 |
| ---- | ---- |
| `/allow` / `/deny` / `/listallow` | 白名单管理 |
| `/setting list` | 查看所有可在运行时修改的配置项 |
| `/setting get <key>` | 查看当前值 |
| `/setting set <key> <value>` | 修改配置 |
| `/setting edit <key>` | 多行编辑（适用于模板等长文本）|
| `/setting unset <key>` | 恢复为默认值 |
| `/stats` | 用量统计（支持 `/stats 7d`、`/stats user @x`、`/stats chat <id>`、`/stats system`、`/stats r2_evict` 手动触发 R2 LRU 清理）|

不可在运行时修改的字段：`telegram.token`、`storage.*`、`publish.base_url`、`auth.admin_users`、`publish.telegraph_token`。修改这些字段需编辑 `config.yaml` 后重启。

配置优先级：`runtime_settings`（SQLite）> 环境变量 > `config.yaml` > 内置默认值。

## 模板自定义

输出文案可通过 `/setting edit` 修改，详见 `config.example.yaml` 中 `templates` 段的注释：

- `templates.illust.*` — Pixiv 插画
- `templates.novel.*` — Pixiv 小说
- `templates.gallery.*` — e-hentai / ExHentai / nhentai 共用

## 开发调试

提供 CLI 工具用于本地测试（不启动 Telegram bot）：

```bash
python -m pixivfeed.provider.pixiv.cli illust 12345 --meta-only
python -m pixivfeed.provider.pixiv.cli publish-illust 12345
python -m pixivfeed.provider.pixiv.cli publish-novel 999
python -m pixivfeed.provider.pixiv.cli url "https://www.pixiv.net/artworks/12345"
```

## 许可证

[MIT](LICENSE)

致谢：

- [DojinGo](https://github.com/Olivi-9/DojinGo) — collector 抽象与 nhentai / eh 解析逻辑
- [telegram-bili-feed-helper](https://github.com/simonsmh/telegram-bili-feed-helper) — Provider / Registry 架构设计
- [bot-rs](https://github.com/jizizr/bot-rs) — `/wiki` 命令实现参考
- [EhTagTranslation](https://github.com/EhTagTranslation/Database) — 提供 e-hentai 标签翻译