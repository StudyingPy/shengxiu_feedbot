# Feed Bot

Feed Bot 是一个 Telegram 机器人，用于自动解析群聊或私聊中出现的 Pixiv、e-hentai、ExHentai、nhentai 链接，将作品内容整理为 Telegra.ph 页面或以图片形式直接发送。用户无需离开 Telegram 即可预览作品。

Bot 采用 Provider / Registry 架构，各数据源（Pixiv、e-hentai、ExHentai、nhentai）实现统一的 Provider 接口，由 Registry 根据链接自动路由到对应 Provider 处理。配置系统支持多层覆盖（SQLite runtime > 环境变量 > config.yaml > 内置默认），大部分参数可通过 `/setting` 命令在运行时热更新，无需重启。

## 支持的站点

| 站点 | 内容类型 | 是否需要登录 |
| ---- | -------- | ------------ |
| Pixiv | illust（插画）/ novel（小说）| 仅 R-18 与部分小说需要 PHPSESSID |
| e-hentai | gallery | 不需要 |
| ExHentai | gallery | 必须（ipb_pass_hash / ipb_member_id / igneous）|
| nhentai | gallery | 不需要 |

## 主要功能

- **链接自动识别**：消息中包含支持站点的链接时自动响应，无需输入命令。
- **智能发送策略**：根据图片数量自动选择发送方式——少量图片通过 `sendMediaGroup` 直接发送，大量图片转为 Telegra.ph 页面。可通过 `/pixiv_telegraph`、`/pixiv_direct` 强制指定模式。
- **运行时配置热更新**：管理员通过 `/setting set ...` 私聊修改配置，立即生效，不需要重启 Bot。所有改动持久化到 SQLite。
- **白名单权限控制**：通过 `/allow`、`/deny`、`/listallow` 管理授权用户和群组。
- **任务队列与并发控制**：按任务类型（archive / zip2tph / 直发图片 / Telegraph 发布）分配独立的 worker pool，避免并发过载。管理员任务优先执行。
- **下载进度与 ETA**：archive zip 下载、`/zip2tph` 接收等耗时操作显示实时进度条、速率和剩余时间估计。
- **用量统计**：`/stats` 命令查看使用统计，支持按时间窗口、用户、群组筛选。
- **维基百科查询**：`/wiki <词条>` 查中文维基百科首条命中；inline 模式（`@bot <关键词>`，仅管理员）返回 top 5 结果列表。
- **归档下载**：`/archive` 命令将作品打包为 zip 发送。e-hentai / ExHentai 支持四种下载模式（网页显示图 / 网页原图 / 归档 1280x / 归档原图）。
- **zip 转 Telegra.ph**：`/zip2tph` 命令将上传的图片 zip 包发布为 Telegra.ph 页面。

## 快速开始

```bash
git clone https://github.com/StudyingPy/shengxiu_feedbot.git
cd shengxiu_feedbot
pip install -e .
cp config.example.yaml config.yaml
# 编辑 config.yaml，至少填写：telegram.token、auth.admin_users、publish.base_url、storage.*
python -m pixivfeed
```

前置条件：

1. **Telegram Bot Token**：通过 [@BotFather](https://t.me/BotFather) 申请
2. **公网可访问的图片服务**：`storage.cache_dir` 需通过 Nginx 等反向代理对外暴露，供 Telegra.ph 服务器拉取图片。详见 [部署指南](docs/DEPLOY.md)
3. **Pixiv PHPSESSID**（可选）：访问 R-18 / 受限小说时需要

首次启动会自动创建 Telegra.ph 账号并将 token 写回 `config.yaml`。

## 用法

### Pixiv

- **Illust**：图片数 ≤ `direct_threshold`（默认 5）时直接发图，超过则生成 Telegra.ph 页面
  - 直发上限 10 张（`sendMediaGroup` 限制），超过自动降级为 Telegra.ph
  - `/pixiv_telegraph <链接>` 强制走 Telegra.ph；`/pixiv_direct <链接>` 强制直发
- **Novel**：自动转为 Telegra.ph 页面，支持 `[newpage]`、`[chapter:]`、`[[jumpuri:>]]`、`[pixivimage:]`、`[uploadedimage:]` 等标记
- **Inline**（仅管理员）：`@bot 12345`、`@bot artworks/12345`、`@bot novel/999`、或完整 URL

### e-hentai / ExHentai

私聊发送链接后显示标题与页数，并提供四个模式按钮：

| 按钮 | 说明 |
| ---- | ---- |
| 网页 · 显示图 | sample 图，不消耗 GP / Credits |
| 网页 · 原图 | 子页 "Download original" 链接，消耗 GP / Credits |
| 归档 · 1280x | 调用 `archiver.php` 获取 zip，1280x 重采样，消耗免费 archive 配额 |
| 归档 · 原图 | 同上，原始分辨率 |

群聊不弹按钮，按 `collectors.{ehentai|exhentai}.default_mode` 配置的默认模式处理。

### nhentai

通过第三方 API 镜像获取数据，直接生成 Telegra.ph 页面发布，支持多 CDN 自动 fallback。

### 启用 e-hentai / ExHentai / nhentai

启动后通过私聊命令启用，无需重启：

```
/setting set collectors.ehentai.enabled true
/setting set collectors.nhentai.enabled true
/setting edit collectors.exhentai.ipb_pass_hash
/setting edit collectors.exhentai.ipb_member_id
/setting edit collectors.exhentai.igneous
/setting set collectors.exhentai.enabled true
```

## 管理命令

| 命令 | 说明 |
| ---- | ---- |
| `/allow` / `/deny` / `/listallow` | 白名单管理 |
| `/setting list` | 查看所有可修改的配置项 |
| `/setting get <key>` | 查看当前值 |
| `/setting set <key> <value>` | 修改配置（单行） |
| `/setting edit <key>` | 多行编辑（适用于模板等长文本） |
| `/setting unset <key>` | 恢复为默认值 |
| `/stats` | 用量统计（支持 `/stats 7d`、`/stats user @x`、`/stats chat <id>`、`/stats system`）|

不可运行时修改的字段：`telegram.token`、`storage.*`、`publish.base_url`、`auth.admin_users`、`publish.telegraph_token`。

配置优先级：`runtime_settings`（SQLite）> 环境变量 > `config.yaml` > 内置默认值。

## 模板自定义

输出文案可通过配置修改，详见 `config.example.yaml` 中 `templates` 段的注释：

- `templates.illust.*` — Pixiv 插画
- `templates.novel.*` — Pixiv 小说
- `templates.gallery.*` — e-hentai / ExHentai / nhentai 共用

## 开发调试

```bash
python -m pixivfeed.provider.pixiv.cli illust 12345 --meta-only
python -m pixivfeed.provider.pixiv.cli publish-illust 12345
python -m pixivfeed.provider.pixiv.cli publish-novel 999
python -m pixivfeed.provider.pixiv.cli url "https://www.pixiv.net/artworks/12345"
```

## 部署与运维

Nginx 配置、服务器更新流程、本地 Bot API 搭建等详见 [部署指南](docs/DEPLOY.md)。

## 许可证

[MIT](LICENSE)

致谢：
- [DojinGo](https://github.com/Olivi-9/DojinGo) — collector 抽象与 nhentai / eh 解析逻辑
- [telegram-bili-feed-helper](https://github.com/simonsmh/telegram-bili-feed-helper) — Provider / Registry 架构设计
- [bot-rs](https://github.com/jizizr/bot-rs) — `/wiki` 命令实现参考
