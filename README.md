# Feed Bot

> 小凤的 TG 解析机器人（好像暂时只能用来涩涩）
> 嗯，虽然说是咱的机器人，除了这两句话好像全都是AI帮忙写的

群友丢个 Pixiv / e-hentai / ExHentai / nhentai 链接进来，bot 自动整理成 Telegra.ph 页面或直接发图回复——省得人人都开梯子点进去看。

## 支持的站点

| 站点 | 内容类型 | 是否需要登录 |
| ---- | -------- | ------------ |
| Pixiv | illust（插画）/ novel（小说）| 仅 R-18 与部分小说需要 PHPSESSID |
| e-hentai | gallery | 不需要 |
| ExHentai | gallery | 必须（ipb_pass_hash / ipb_member_id / igneous）|
| nhentai | gallery | 不需要 |

## 特性

- **自动识别**：消息里有链接就直接响应，不用打命令
- **智能发送**：图少直发 sendMediaGroup，图多走 Telegra.ph 整页
- **运行时改配置**：`/setting set ...` 私聊就能改，**不用重启**
- **白名单**：`/allow` / `/deny` 控制谁能用
- **Inline 模式**（仅 admin）：`@bot 12345` 直接查 Pixiv 作品

## 快速开始

```bash
git clone https://github.com/StudyingPy/shengxiu_feedbot.git
cd shengxiu_feedbot
pip install -e .
cp config.example.yaml config.yaml
# 编辑 config.yaml，至少填这几项：
#   telegram.token、auth.admin_users、publish.base_url、storage.*
python -m pixivfeed
```

首次启动会自动创建 Telegra.ph 账号并写回 `config.yaml`。

### 前置准备

1. **Telegram Bot Token**：找 [@BotFather](https://t.me/BotFather) 申请
2. **Nginx 反向代理**：`cache_dir` 必须能从公网访问（Telegra.ph 服务器要来拉图）
3. **Pixiv PHPSESSID**（可选）：要看 R-18 / 受限小说才需要

### Nginx 参考片段

```nginx
location /p/ {
    alias /var/cache/pixiv-feed-bot/images/;
    add_header Cache-Control "public, max-age=86400";
}
```

## 用法详解

### Pixiv

- **Illust**：图 ≤ `direct_threshold`（默认 5）直接发图，超过走 Telegra.ph
  - 直发 ≤ 10 张走 sendMediaGroup，> 10 张自动降级 Telegra.ph
  - `/pixiv_telegraph <链接>` / `/pixiv_direct <链接>` 强制模式
- **Novel**：自动转 Telegra.ph，支持 `[newpage]` / `[chapter:]` / `[[jumpuri:>]]` / `[pixivimage:]` / `[uploadedimage:]`
- **Inline**（仅 admin）：`@bot 12345`、`@bot artworks/12345`、`@bot novel/999`、或贴完整 URL

### e-hentai / ExHentai

私聊收到链接 → 显示标题 + 页数 → 弹 4 个按钮：

```
网页 · 显示图   |   网页 · 原图
归档 · 1280x   |   归档 · 原图
取消
```

- **网页 · 显示图**：sample 图，免 GP / Credits
- **网页 · 原图**：子页 "Download original" 链接，消耗 GP / Credits
- **归档 · 1280x**：调 `archiver.php` 拿 zip，1280x 重采样，消耗免费 archive 配额
- **归档 · 原图**：同上，原始分辨率

群聊不弹按钮，按 `collectors.{ehentai|exhentai}.default_mode` 走默认模式。归档失败（H@H 节点不可用 / 配额耗尽 / 超时）会报错让用户改模式。

### nhentai

API 走第三方镜像 `nhapi.cat42.uk`，直接 Telegra.ph 发布，多 CDN 自动 fallback。

### 启用三个 collector

启动后私聊 bot（不用重启）：

```
/setting set collectors.ehentai.enabled true
/setting set collectors.nhentai.enabled true
/setting edit collectors.exhentai.ipb_pass_hash
/setting edit collectors.exhentai.ipb_member_id
/setting edit collectors.exhentai.igneous
/setting set collectors.exhentai.enabled true
```

## 管理命令速查

| 命令 | 作用 |
| ---- | ---- |
| `/allow` / `/deny` / `/listallow` | 白名单热更新 |
| `/setting list` | 查看所有可改的 key |
| `/setting get <key>` | 看当前值 |
| `/setting set <key> <value>` | 改值（单行） |
| `/setting edit <key>` | 多行编辑（适合改模板） |
| `/setting unset <key>` | 恢复默认 |

**不可运行时改的字段**：`telegram.token`、`storage.*`、`publish.base_url`、`auth.admin_users`、`publish.telegraph_token`。

所有改动存 SQLite，重启不丢。优先级：`runtime_settings` > 环境变量 > `config.yaml` > 内置默认。

## 模板自定义

每个站点的输出文案都可以改，详见 `config.example.yaml` 里 `templates` 段的注释：

- `templates.illust.*` — Pixiv 插画
- `templates.novel.*` — Pixiv 小说
- `templates.gallery.*` — e-hentai / ExHentai / nhentai 共用

## 进阶：本地 Bot API（处理大文件必备）

Telegram 官方 Bot API 限制 `getFile` 20MB、`sendDocument` 50MB。如果你要用 `/zip2tph`（接收用户上传 zip）或 `/archive`（把图集打包回发），几乎一定超限——必须自建 [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) 服务绕开。

<details>
<summary><b>展开看完整配置步骤</b></summary>

### 1. 起一个 telegram-bot-api 服务（docker 示例）

```bash
docker run -d --name telegram-bot-api --restart unless-stopped \
  -p 127.0.0.1:8081:8081 \
  -e TELEGRAM_API_ID=<你的 api_id> \
  -e TELEGRAM_API_HASH=<你的 api_hash> \
  -e TELEGRAM_LOCAL=1 \
  -v /var/lib/telegram-bot-api:/var/lib/telegram-bot-api \
  -v /tmp/telegram-bot-api:/tmp/telegram-bot-api \
  aiogram/telegram-bot-api:latest \
  --local --dir=/var/lib/telegram-bot-api --temp-dir=/tmp/telegram-bot-api \
  --http-ip-address=0.0.0.0 --http-port=8081
```

`api_id` / `api_hash` 在 [my.telegram.org](https://my.telegram.org/) 申请（不是 Bot Token）。`--local` 缺一不可，否则即使自建也会按 20MB 处理。

### 2. 把 bot 从官方 API 切到本地 API

Telegram 服务端规定：bot 切到本地 Bot API 前**必须**先在官方 API 调一次 `logOut`，否则本地 API 会拒绝该 bot 的 `getFile`，表现为 `Not Found`。

```bash
# 1) 先停 feed bot
systemctl stop pixiv-feed-bot

# 2) 在官方 API 上 logOut（一次性，之后该 bot 必须用本地 API）
curl -sS "https://api.telegram.org/bot${BOT_TOKEN}/logOut"
# 期待：{"ok":true,"result":true}

# 3) 重启本地 telegram-bot-api 干净接管
docker restart telegram-bot-api
```

要换回官方 API，需要在本地 API 上再调一次 `logOut`。

### 3. 在 `config.yaml` 启用

```yaml
telegram:
  token: "..."
  base_url: "http://127.0.0.1:8081/bot"
  base_file_url: "http://127.0.0.1:8081/file/bot"
  local_mode: true
```

`local_mode: true` 是 PTB 客户端开关，告诉 PTB 在 `getFile` 后直接读本地文件路径，绕开 HTTPS 拉一遍的 20MB 限制。三项必须同时正确，否则要么走回官方 API，要么报 `File is too big`。

### 4. 文件读权限

`--local` 模式下 telegram-bot-api 把上传文件落盘到 `--dir`（默认 `/var/lib/telegram-bot-api/<token>/...`）。运行 feed bot 的用户必须能读到：

```bash
chmod -R o+rX /var/lib/telegram-bot-api
```

出现 `Permission denied` 就是这步漏了。

### 5. 启动并验证

```bash
systemctl start pixiv-feed-bot
journalctl -u pixiv-feed-bot -n 50 --no-pager
```

之后**重新发送一次 zip** 给 bot 触发 `/zip2tph`——之前的 `file_id` 是官方 API 发的，本地 API 不认。

</details>

## 开发调试 CLI

```bash
python -m pixivfeed.provider.pixiv.cli illust 12345 --meta-only
python -m pixivfeed.provider.pixiv.cli publish-illust 12345
python -m pixivfeed.provider.pixiv.cli publish-novel 999
python -m pixivfeed.provider.pixiv.cli url "https://www.pixiv.net/artworks/12345"
```

## 许可证

[MIT](LICENSE)。

参考实现致谢：
- [DojinGo](https://github.com/...) —— collector 抽象与 nhentai / eh 解析逻辑
- [telegram-bili-feed-helper](https://github.com/...) —— Provider / Registry 架构形状
