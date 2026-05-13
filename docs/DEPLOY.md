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

## GitHub webhook 自动部署

`main` 分支 push 后服务器在 1–2 秒内自动 `git pull` + `pip install`（仅当 `pyproject.toml` 变更）+ `systemctl restart pixiv-feed-bot`，结果通过 Telegram 推送给 admin。

架构：

```
GitHub push → HTTPS POST → Nginx (feed.fengshengxiu.club/deploy)
                              ↓
                        adnanh/webhook (127.0.0.1:9000)
                              ↓ HMAC 验签 + 仅 main + 仅 push
                       feed-bot-deploy.sh （以 deploy 用户身份）
                              ↓ sudo（受 sudoers 限制）
                        systemctl restart pixiv-feed-bot
                              ↓
                        curl Telegram bot API → admin
```

### 1. 安装 adnanh/webhook

Debian/Ubuntu：

```bash
apt install webhook
```

其它发行版直接下 [GitHub release](https://github.com/adnanh/webhook/releases) 的二进制，放到 `/usr/bin/webhook`。

### 2. 创建 deploy 用户并接管仓库

```bash
useradd --system --shell /bin/bash --home-dir /var/lib/feed-bot-deploy --create-home deploy
chown -R deploy:deploy /opt/pixiv-feed-bot
# venv 也要给 deploy 写权限（pip install 时要动）
chown -R deploy:deploy /opt/pixiv-feed-bot/.venv
```

deploy 用户**只**用来跑部署脚本——bot 仍以 `pixivbot` 运行，二者解耦。

### 3. 安装 sudoers 片段

```bash
cp deploy/feed-bot-deploy.sudoers /etc/sudoers.d/feed-bot-deploy
chmod 0440 /etc/sudoers.d/feed-bot-deploy
visudo -cf /etc/sudoers.d/feed-bot-deploy
```

只放行 `systemctl restart pixiv-feed-bot`；deploy 用户被打穿也接管不了系统。

### 4. 安装部署脚本

```bash
cp deploy/feed-bot-deploy.sh /usr/local/bin/feed-bot-deploy.sh
chmod 0755 /usr/local/bin/feed-bot-deploy.sh
```

### 5. 让 deploy 用户能读 bot 配置（默认凭据来源）

部署脚本默认从 `/etc/pixiv-feed-bot/config.yaml` 自动读 `telegram.token`、`auth.admin_users[0]`、`telegram.base_url`——通知用的 bot 和接收 admin 与 feed-bot 本身保持一致。所以**默认情况下你什么都不用单独配**，只需要让 `deploy` 用户能读 config.yaml：

```bash
chgrp pixivbot /etc/pixiv-feed-bot/config.yaml
chmod 0640 /etc/pixiv-feed-bot/config.yaml
usermod -aG pixivbot deploy
# 让组成员身份生效；deploy 用户没真正登录过的话也得清缓存
systemctl restart feed-bot-webhook 2>/dev/null || true
```

完成这三行就可以**直接跳到第 6 步**。

#### 可选：自定义通知凭据（override）

只有在以下场景才需要单独写 `/etc/feed-bot-webhook/env`：

- 想让 deploy 通知走**另一个 bot**（不和 feed-bot 正常对话混在一起）
- 推给**别的 admin**，或 `admin_users` 列表里的第 2、3 个
- 走本地 Bot API 推送（默认会读 `config.yaml` 里的 `base_url`，已经够用——除非你想为 deploy 单独指定一个 base_url）

```bash
mkdir -p /etc/feed-bot-webhook
cat > /etc/feed-bot-webhook/env <<'EOF'
# 留空 = 用 config.yaml 里的默认值；填了就盖默认
TG_BOT_TOKEN=
TG_ADMIN_ID=
# 可选：本地 Bot API。留空 = 走 config.yaml 的 base_url，再没有就走官方
# TG_BOT_API_BASE=http://127.0.0.1:8081/bot
EOF
chown deploy:deploy /etc/feed-bot-webhook/env
chmod 0600 /etc/feed-bot-webhook/env
```

> deploy 完全没法读 config.yaml 也没设 env？脚本仍然能跑完部署，只是不发 TG 通知（journal 还有）。

### 6. 配置 webhook hooks

```bash
SECRET=$(openssl rand -hex 32)   # 记住这个值，GitHub 端要用
mkdir -p /etc/feed-bot-webhook
cp deploy/feed-bot-webhook-hooks.json.example /etc/feed-bot-webhook/hooks.json
sed -i "s|REPLACE_WITH_LONG_RANDOM_SECRET|${SECRET}|" /etc/feed-bot-webhook/hooks.json
chown -R deploy:deploy /etc/feed-bot-webhook
chmod 0600 /etc/feed-bot-webhook/hooks.json
echo "GitHub webhook secret = ${SECRET}"
```

hooks.json 内部已限定：HMAC-SHA256 验签 + `ref == refs/heads/main` + `X-GitHub-Event == push`，任一不满足直接 403。

### 7. 启用 webhook 服务

```bash
cp deploy/feed-bot-webhook.service /etc/systemd/system/feed-bot-webhook.service
systemctl daemon-reload
systemctl enable --now feed-bot-webhook
journalctl -u feed-bot-webhook -n 20 --no-pager
```

### 8. Nginx 接入

把下面这段加到 `feed.fengshengxiu.club` server block 内（任意位置，建议挨着 `/p/` 那段）：

```nginx
# GitHub webhook → 本地 adnanh/webhook → feed-bot-deploy.sh
location = /deploy {
    if ($request_method != POST) { return 405; }
    proxy_pass http://127.0.0.1:9000/hooks/feed-bot;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 120s;
    # 部署日志单独切一个文件，便于排查
    access_log /www/wwwlogs/feed.fengshengxiu.club-deploy.log;
}
```

`nginx -t && systemctl reload nginx`。

**宝塔面板用户**：**不要**直接编辑 `/www/server/panel/vhost/nginx/<域名>.conf` 主文件——面板"保存网站设置"会覆盖。宝塔的主配置里已经预留了一行 `include /www/server/panel/vhost/nginx/extension/<域名>/*.conf;`，把片段做成独立文件丢进去就行：

```bash
mkdir -p /www/server/panel/vhost/nginx/extension/feed.fengshengxiu.club
cp deploy/feed-bot-webhook.nginx.conf.example \
   /www/server/panel/vhost/nginx/extension/feed.fengshengxiu.club/feed-bot-webhook.conf
# 重载：宝塔面板 → 软件商店 → Nginx → 重载；或命令行
nginx -t && nginx -s reload
```

如果开启了宝塔的「Nginx 防火墙」/「网站防火墙」插件，去把 `/deploy` 加白名单，否则 GitHub 的 POST 可能被 CC 防御误拦。

### 9. 配置 GitHub webhook

仓库 → Settings → Webhooks → Add webhook：

- **Payload URL**: `https://feed.fengshengxiu.club/deploy`
- **Content type**: `application/json`
- **Secret**: 第 6 步生成的 `${SECRET}`
- **SSL verification**: Enable
- **Events**: Just the push event
- **Active**: ✓

加完会立刻发一个 `ping` 事件——hooks.json 里限定了 `X-GitHub-Event=push`，ping 会被 403 拒掉是正常的。push 一次 main 才会真的触发。

### 测试

本地推个无害 commit（比如改 README 空格）：

```bash
git commit --allow-empty -m "chore: webhook smoke test"
git push
```

预期：

- 几秒内 admin 私聊收到 `✅ feed-bot deploy ok` 或 `❌ feed-bot deploy: <step>`
- `journalctl -u feed-bot-webhook -n 30 --no-pager` 看到 hook 触发
- `journalctl -u pixiv-feed-bot -n 30 --no-pager` 看到 bot 重启

成功通知样例：

```
✅ feed-bot deploy ok

版本：0.6.0 → 0.6.1
HEAD：6ac0612a → c2495c0c
Tags：v0.6.1

提交（3）：
  c2495c0 docs(deploy): aaPanel-friendly nginx snippet for webhook
  dbf47a2 release: v0.6.1 patch
  6ac0612 fix: small fixup

变更：3 files changed, 12 insertions(+), 2 deletions(-)
  CHANGELOG.md
  docs/DEPLOY.md
  pixivfeed/...

最近日志：
<journalctl -u pixiv-feed-bot 末 10 行>
```

失败通知同样会带版本与 commit 列表，最后补"错误尾部日志"段；可以直接判断是 git fetch、pip install 还是 systemctl restart 哪一步出问题。

### 排错

| 现象 | 排查 |
| --- | --- |
| GitHub Recent Deliveries 显示 401/403 | hooks.json 里的 secret 与 GitHub 端不一致；或 X-Hub-Signature-256 没传过来（Nginx 抹掉了 header） |
| Nginx 返回 405 | GitHub 发了 GET（手动点 redeliver 也可能）；这是正常拒绝，push 事件是 POST |
| Recent Deliveries 200 但 admin 没消息 | deploy 用户读不了 `/etc/pixiv-feed-bot/config.yaml`（看第 5 步的 chgrp/chmod）；或 `/etc/feed-bot-webhook/env` 里 override 凭据格式不对。`journalctl -u feed-bot-webhook` 看 `skip notify` 行确认 |
| 通知报 `systemctl restart` 失败 | sudoers 没装好；`sudo -u deploy sudo -n /bin/systemctl restart pixiv-feed-bot` 手工跑一次看错误 |
| 通知报 `service not active after restart` | bot 自己起不来（config 错、依赖版本不匹配）；按通知里的 journal tail 排 |

要临时禁用自动部署（比如准备做破坏性测试）：

```bash
systemctl stop feed-bot-webhook
# 或在 GitHub webhook 配置页把 Active 关掉
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
