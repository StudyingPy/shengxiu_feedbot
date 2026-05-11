# 部署指南

## 前置条件

1. **Telegram Bot Token**：通过 [@BotFather](https://t.me/BotFather) 申请。
2. **Nginx 反向代理**：`storage.cache_dir` 目录需通过 Nginx 对公网暴露，Telegra.ph 服务器需要能访问这些图片。
3. **Pixiv PHPSESSID**（可选）：访问 R-18 / 受限小说时需要。在浏览器登录 pixiv.net 后从开发者工具复制 cookie。

## Nginx 配置

将 `storage.cache_dir` 以静态文件形式暴露。参考片段：

```nginx
location /p/ {
    alias /var/cache/pixiv-feed-bot/images/;
    add_header Cache-Control "public, max-age=86400";
}
```

完整的 Nginx 配置示例见 [deploy/nginx.conf.example](../deploy/nginx.conf.example)。

## 更新部署

服务器侧使用 git 管理代码，通过 `git pull` 更新。

### 首次从手动上传迁移到 git

```bash
systemctl stop pixiv-feed-bot
cd /opt
mv pixiv-feed-bot pixiv-feed-bot.bak.$(date +%Y%m%d)
git clone https://github.com/StudyingPy/shengxiu_feedbot.git pixiv-feed-bot
cd pixiv-feed-bot
pip install -e .
chown -R pixivbot:pixivbot /opt/pixiv-feed-bot
systemctl start pixiv-feed-bot
```

`config.yaml` 存放在 `/etc/pixiv-feed-bot/`，不在仓库内，git 操作不会影响它。

### 日常更新

```bash
cd /opt/pixiv-feed-bot
systemctl stop pixiv-feed-bot
git pull
pip install -e . --quiet     # 仅 pyproject.toml 变动时需要
systemctl start pixiv-feed-bot
journalctl -u pixiv-feed-bot -n 50 --no-pager
```

### 部署指定版本

```bash
git fetch --tags
git checkout v0.4.2
```

### 回退

```bash
git checkout v0.4.1
# 或
git reset --hard <commit>
```

## systemd 服务

服务文件示例见 [deploy/pixiv-feed-bot.service](../deploy/pixiv-feed-bot.service)。另有定时清理服务 [deploy/pixiv-feed-bot-cleanup.service](../deploy/pixiv-feed-bot-cleanup.service) 和 [deploy/pixiv-feed-bot-cleanup.timer](../deploy/pixiv-feed-bot-cleanup.timer)。

## 本地 Bot API

Telegram 官方 Bot API 限制 `getFile` 20MB、`sendDocument` 50MB。使用 `/zip2tph`（接收用户上传 zip）或 `/archive`（打包图集回发）时，文件大小几乎一定超过此限制。需要自建 [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) 服务绕开。

### 1. 启动 telegram-bot-api 服务

Docker 示例：

```bash
docker run -d --name telegram-bot-api --restart unless-stopped \
  -p 127.0.0.1:8081:8081 \
  -e TELEGRAM_API_ID=<api_id> \
  -e TELEGRAM_API_HASH=<api_hash> \
  -e TELEGRAM_LOCAL=1 \
  -v /var/lib/telegram-bot-api:/var/lib/telegram-bot-api \
  -v /tmp/telegram-bot-api:/tmp/telegram-bot-api \
  aiogram/telegram-bot-api:latest \
  --local --dir=/var/lib/telegram-bot-api --temp-dir=/tmp/telegram-bot-api \
  --http-ip-address=0.0.0.0 --http-port=8081
```

`api_id` / `api_hash` 在 [my.telegram.org](https://my.telegram.org/) 申请（不是 Bot Token）。`--local` 参数必须指定，否则即使自建也会按 20MB 限制处理。

### 2. 从官方 API 切换到本地 API

Telegram 服务端要求：Bot 切到本地 Bot API 前**必须**先在官方 API 调用一次 `logOut`，否则本地 API 会拒绝该 Bot 的 `getFile`，表现为 `Not Found`。

```bash
# 1) 停止 feed bot
systemctl stop pixiv-feed-bot

# 2) 在官方 API 上 logOut（一次性操作）
curl -sS "https://api.telegram.org/bot${BOT_TOKEN}/logOut"
# 期望返回：{"ok":true,"result":true}

# 3) 重启本地 telegram-bot-api
docker restart telegram-bot-api
```

如需切回官方 API，需在本地 API 上再调用一次 `logOut`。

### 3. 修改 config.yaml

```yaml
telegram:
  token: "..."
  base_url: "http://127.0.0.1:8081/bot"
  base_file_url: "http://127.0.0.1:8081/file/bot"
  local_mode: true
```

三项必须同时正确配置。`local_mode: true` 告知 PTB 客户端在 `getFile` 后直接读取本地文件路径，绕开 HTTPS 20MB 下载限制。

### 4. 文件读权限

`--local` 模式下 telegram-bot-api 将上传文件写入 `--dir` 目录（默认 `/var/lib/telegram-bot-api/<token>/...`）。运行 feed bot 的用户必须有读取权限：

```bash
chmod -R o+rX /var/lib/telegram-bot-api
```

出现 `Permission denied` 错误即此步骤遗漏。

### 5. 启动并验证

```bash
systemctl start pixiv-feed-bot
journalctl -u pixiv-feed-bot -n 50 --no-pager
```

切换后需重新发送 zip 给 Bot 触发 `/zip2tph`——之前在官方 API 下生成的 `file_id` 在本地 API 中不可用。
