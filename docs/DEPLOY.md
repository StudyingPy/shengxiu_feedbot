# 部署指南

本文档介绍如何在服务器上部署 Feed Bot，包括反向代理、自动更新、定时清理、对象存储与本地 Bot API 等内容。

## 前置条件

1. **Telegram Bot Token**：通过 [@BotFather](https://t.me/BotFather) 申请。
2. **Nginx 反向代理**：`storage.cache_dir` 目录需通过 Nginx 对公网暴露，供 Telegra.ph 服务器拉取图片。
3. **Pixiv PHPSESSID**（可选）：访问 R-18 或受限小说时需要。在浏览器登录 pixiv.net 后从开发者工具复制 cookie。

## Nginx 配置

将 `storage.cache_dir` 以静态文件形式暴露。参考片段：

```nginx
location /p/ {
    alias /var/cache/pixiv-feed-bot/images/;
    add_header Cache-Control "public, max-age=86400";
}
```

完整示例见 [deploy/nginx.conf.example](../deploy/nginx.conf.example)。

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
pip install -e . --quiet     # 仅在 pyproject.toml 变动时需要
systemctl start pixiv-feed-bot
journalctl -u pixiv-feed-bot -n 50 --no-pager
```

### 部署指定版本

```bash
git fetch --tags
git checkout v0.8.2
```

### 回退

```bash
git checkout v0.8.1
# 或
git reset --hard <commit>
```

<details>
<summary><h2 style="display:inline">GitHub Webhook 自动部署（可选，点击展开）</h2></summary>

> 本节面向自托管 fork 的用户。机制本身不绑定原仓库，将下文中的域名、`SECRET`、仓库地址、用户名等替换为自己的值即可使用。

配置完成后，`main` 分支的每次 push 会触发服务器自动执行 `git pull` + 按需 `pip install` + `systemctl restart pixiv-feed-bot`，结果通过 Telegram 推送给管理员。

整体流程：GitHub 通过 HTTPS POST 调用 Nginx 上的 `/deploy`，Nginx 反代到本机的 [adnanh/webhook](https://github.com/adnanh/webhook) 服务，验证签名后调用部署脚本，并通过 `sudo` 重启 bot。

### 1. 安装 adnanh/webhook

Debian/Ubuntu：

```bash
apt install webhook
```

其它发行版可下载 [GitHub Release](https://github.com/adnanh/webhook/releases) 的二进制文件并放到 `/usr/bin/webhook`。

### 2. 创建 deploy 用户并接管仓库

```bash
useradd --system --shell /bin/bash --home-dir /var/lib/feed-bot-deploy --create-home deploy
chown -R deploy:deploy /opt/pixiv-feed-bot
# venv 也要给 deploy 写权限（pip install 时需要）
chown -R deploy:deploy /opt/pixiv-feed-bot/.venv
```

deploy 用户仅用于运行部署脚本；bot 仍以 `pixivbot` 用户运行。两个账号职责分离，即使 deploy 账号被攻陷，攻击者也不能通过它接管系统。

### 3. 安装 sudoers 片段

```bash
cp deploy/feed-bot-deploy.sudoers /etc/sudoers.d/feed-bot-deploy
chmod 0440 /etc/sudoers.d/feed-bot-deploy
visudo -cf /etc/sudoers.d/feed-bot-deploy
```

该片段仅放行 `systemctl restart pixiv-feed-bot`，不会授予其它特权。

### 4. 安装部署脚本

```bash
cp deploy/feed-bot-deploy.sh /usr/local/bin/feed-bot-deploy.sh
chmod 0755 /usr/local/bin/feed-bot-deploy.sh
```

### 5. 准备 webhook 配置目录与读取 bot 配置

webhook 自身的配置目录与 bot 的配置目录是分开的，先创建前者：

```bash
mkdir -p /etc/feed-bot-webhook
chown deploy:deploy /etc/feed-bot-webhook
chmod 0750 /etc/feed-bot-webhook
```

部署脚本默认会从 `/etc/pixiv-feed-bot/config.yaml` 读取 `telegram.token`、`auth.admin_users[0]` 与 `telegram.base_url`，使部署通知与 bot 本身使用同一个 Telegram 账号。因此默认情况下无需额外配置，只需让 deploy 用户能读取 `config.yaml`：

```bash
chgrp pixivbot /etc/pixiv-feed-bot/config.yaml
chmod 0640 /etc/pixiv-feed-bot/config.yaml
usermod -aG pixivbot deploy
# 让组成员身份生效
systemctl restart feed-bot-webhook 2>/dev/null || true
```

完成后即可直接进行第 6 步。

#### 可选：自定义通知凭据

仅在以下场景才需要单独写 `/etc/feed-bot-webhook/env`：

- 希望部署通知由另一个 bot 发出，与 feed-bot 的正常对话分离。
- 希望通知推给其他管理员，或 `admin_users` 列表中的第 2、3 个用户。
- 希望部署通知走与 bot 不同的 Bot API base URL。

```bash
cat > /etc/feed-bot-webhook/env <<'EOF'
# 留空则使用 config.yaml 中的默认值；填写则覆盖
TG_BOT_TOKEN=
TG_ADMIN_ID=
# 可选：本地 Bot API 地址。留空则使用 config.yaml 的 base_url
# TG_BOT_API_BASE=http://127.0.0.1:8081/bot
EOF
chown deploy:deploy /etc/feed-bot-webhook/env
chmod 0600 /etc/feed-bot-webhook/env
```

> 即使 deploy 用户读不到 config.yaml 也未配置 env，脚本仍能完成部署，只是不会发送 Telegram 通知（journal 中仍可查到记录）。

### 6. 配置 webhook hooks

```bash
SECRET=$(openssl rand -hex 32)   # 记下此值，GitHub 端需要使用
cp deploy/feed-bot-webhook-hooks.json.example /etc/feed-bot-webhook/hooks.json
sed -i "s|REPLACE_WITH_LONG_RANDOM_SECRET|${SECRET}|" /etc/feed-bot-webhook/hooks.json
chown -R deploy:deploy /etc/feed-bot-webhook
chmod 0600 /etc/feed-bot-webhook/hooks.json
echo "GitHub webhook secret = ${SECRET}"
```

`hooks.json` 已限定三重过滤：HMAC-SHA256 签名验证 + `ref == refs/heads/main` + `X-GitHub-Event == push`。任一不满足将直接返回 403。

### 7. 启用 webhook 服务

```bash
cp deploy/feed-bot-webhook.service /etc/systemd/system/feed-bot-webhook.service
systemctl daemon-reload
systemctl enable --now feed-bot-webhook
journalctl -u feed-bot-webhook -n 20 --no-pager
```

### 8. Nginx 接入

将以下片段加入对应站点（即 bot 用于对外暴露 `cache_dir` 的那个域名）的 server block 内（建议放在 `/p/` 段附近）：

```nginx
# GitHub webhook → 本地 adnanh/webhook → feed-bot-deploy.sh
location = /deploy {
    if ($request_method != POST) { return 405; }
    proxy_pass http://127.0.0.1:9000/hooks/feed-bot;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 120s;
    # 部署日志单独切到一个文件，便于排查
    access_log /www/wwwlogs/<your-domain>-deploy.log;
}
```

随后执行 `nginx -t && systemctl reload nginx`。

**宝塔面板用户**：请勿直接编辑 `/www/server/panel/vhost/nginx/<域名>.conf` 主文件——面板「保存网站设置」时会覆盖。宝塔的主配置已预留 `include /www/server/panel/vhost/nginx/extension/<域名>/*.conf;`，将片段保存为独立文件即可：

```bash
mkdir -p /www/server/panel/vhost/nginx/extension/<your-domain>
cp deploy/feed-bot-webhook.nginx.conf.example \
   /www/server/panel/vhost/nginx/extension/<your-domain>/feed-bot-webhook.conf
nginx -t && nginx -s reload
```

如启用了宝塔的「Nginx 防火墙」或「网站防火墙」插件，请将 `/deploy` 加入白名单，避免 GitHub 的 POST 请求被 CC 防御误拦截。

### 9. 配置 GitHub webhook

仓库 → Settings → Webhooks → Add webhook：

- **Payload URL**：`https://<your-domain>/deploy`
- **Content type**：`application/json`
- **Secret**：第 6 步生成的 `${SECRET}`
- **SSL verification**：Enable
- **Events**：Just the push event
- **Active**：勾选

添加后 GitHub 会立即发送一次 `ping` 事件——由于 `hooks.json` 限定了 `X-GitHub-Event=push`，ping 会被 403 拒绝，属于正常现象。push 到 main 后才会真正触发部署。

### 测试

推送一次空提交进行验证：

```bash
git commit --allow-empty -m "chore: webhook smoke test"
git push
```

预期结果：

- 几秒内管理员私聊会收到 `✅ feed-bot deploy ok` 或 `❌ feed-bot deploy: <step>`。
- `journalctl -u feed-bot-webhook -n 30 --no-pager` 可看到 hook 触发记录。
- `journalctl -u pixiv-feed-bot -n 30 --no-pager` 可看到 bot 重启记录。

成功通知示例：

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

失败通知同样会附带版本号、提交列表与错误日志末尾片段，可直接判断是 git fetch、pip install 还是 systemctl restart 哪一步出现问题。

### 排错

| 现象 | 排查方向 |
| --- | --- |
| GitHub Recent Deliveries 显示 401 / 403 | `hooks.json` 中的 secret 与 GitHub 端不一致；或 `X-Hub-Signature-256` 未传递（Nginx 抹掉了 header） |
| Nginx 返回 405 | GitHub 发送了 GET 请求（手动点 redeliver 也可能触发）。push 事件为 POST，405 属于正常拒绝 |
| Recent Deliveries 显示 200 但管理员未收到消息 | deploy 用户读不到 `/etc/pixiv-feed-bot/config.yaml`（参考第 5 步的权限设置）；或 `/etc/feed-bot-webhook/env` 中的凭据格式错误。可通过 `journalctl -u feed-bot-webhook` 中的 `skip notify` 行确认 |
| 通知报告 `systemctl restart` 失败 | sudoers 未正确安装；可手工运行 `sudo -u deploy sudo -n /bin/systemctl restart pixiv-feed-bot` 查看错误 |
| 通知中出现 `sudo: The "no new privileges" flag is set` | webhook 的 systemd unit 中加了 `NoNewPrivileges=yes`，与 sudo 提权冲突。最新版 `deploy/feed-bot-webhook.service` 已移除此项；重新拷贝并 `systemctl daemon-reload && systemctl restart feed-bot-webhook` 即可 |
| 通知报告 `service not active after restart` | bot 启动失败（配置错误、依赖版本不匹配等）。按通知中附带的 journal 末尾片段排查 |

临时禁用自动部署：

```bash
systemctl stop feed-bot-webhook
# 或在 GitHub webhook 配置页将 Active 关闭
```

</details>

## systemd 服务

主服务示例见 [deploy/pixiv-feed-bot.service](../deploy/pixiv-feed-bot.service)；定时清理服务见 [deploy/pixiv-feed-bot-cleanup.service](../deploy/pixiv-feed-bot-cleanup.service) 与 [deploy/pixiv-feed-bot-cleanup.timer](../deploy/pixiv-feed-bot-cleanup.timer)。

### 启用缓存清理 timer

`storage.cache_days` 依赖一个 systemd timer 定时删除超期图片。**未启用 timer 时 `cache_days` 不会生效**，缓存会持续累积。一次性启用：

```bash
cp deploy/pixiv-feed-bot-cleanup.service /etc/systemd/system/
cp deploy/pixiv-feed-bot-cleanup.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pixiv-feed-bot-cleanup.timer

# 查看下次触发时间
systemctl list-timers pixiv-feed-bot-cleanup.timer

# 手动执行一次以验证清理逻辑
systemctl start pixiv-feed-bot-cleanup.service
journalctl -u pixiv-feed-bot-cleanup.service -n 20 --no-pager
# 期望末行类似：cleanup done: removed N files (X.X MB), M empty dirs
```

默认每天 04:00 触发（带 30 分钟抖动），运行 `deploy/cleanup.py` 删除 `storage.cache_dir` 下 mtime 超过 `cache_days` 天的文件。`telegra.ph_cache` 表中的永久映射不会被删除，避免旧链接失效。

## Cloudflare R2 / S3 兼容对象存储（可选）

启用后，发布 Telegra.ph 页面前先将图片上传到 R2，并使用 R2 自定义域名作为 Telegra.ph 的图片地址，使发布的页面不再受本地缓存 7 天 TTL 的影响，从根本上解决「大画廊或冷链接几天后部分图片加载失败」。

**默认关闭**（`storage.r2.enabled: false`）。本地缓存 + Nginx 反向代理仍是默认方案，未配置 R2 时行为与未启用前完全一致。

### 1. 在 Cloudflare 控制台创建 bucket

1. 登录 Cloudflare → 左侧 **R2 Object Storage**（首次使用需绑定信用卡，但提供 10 GB 免费额度，正常使用不会产生费用）。
2. **Create bucket**：
   - Bucket name：自定义，账户内唯一即可。
   - Location：选择 **Asia-Pacific (APAC)** 或 **Automatic**。
   - Storage class：**Standard**。

### 2. 绑定自定义域名（必须）

bucket → **Settings** → **Public Access** → **Connect Domain**：

1. 输入一个域名下未使用过的子域名（例如 `r2.your-domain.com`）。
2. Cloudflare 自动添加 DNS CNAME（前提是域名 DNS 由 CF 托管），等待数十秒至几分钟变为 `Active`。

> ⚠️ **请勿使用 `pub-<hash>.r2.dev` 默认开发域名**——该域名带速率限制，Telegra.ph 拉取图片时可能被限流。

### 3. 创建 API token

CF 控制台左下角 → **R2** → **Manage R2 API Tokens** → **Create API token**：

| 字段 | 值 |
|---|---|
| Token name | `pixiv-feed-bot` |
| Permissions | **Object Read & Write** |
| Specify bucket(s) | **Apply to specific buckets only** → 勾选目标 bucket |
| TTL | Forever |

确认后 Cloudflare 会一次性显示 Access Key ID、Secret Access Key 与 S3 endpoint URL（形如 `https://<account-id>.r2.cloudflarestorage.com`）。**该页面只显示一次**，关闭后无法找回 Secret，请先复制保存。

### 4. 编辑 config.yaml

在 `/etc/pixiv-feed-bot/config.yaml` 的 `storage:` 段添加：

```yaml
storage:
  cache_dir: "/var/cache/pixiv-feed-bot/images"
  cache_days: 7
  db_path: "/var/lib/pixiv-feed-bot/data.db"
  r2:
    enabled: true
    endpoint: "https://<account-id>.r2.cloudflarestorage.com"
    region: "auto"
    bucket: "your-bucket-name"
    access_key_id: "<32 字符 Access Key ID>"
    secret_access_key: "<64 字符 Secret Access Key>"
    custom_domain: "https://r2.your-domain.com"   # 不带尾斜杠
    prefix: "feedbot/"                            # 推荐设置，bucket 内对象统一前缀
    capacity_gb: 80                               # LRU 自动清理阈值
    lru_check_interval_minutes: 60                # LRU 扫描间隔（分钟）
    max_upload_size_gb: 1.0                       # 单次发布体积护栏，超过则跳过 R2
```

注意：`r2:` 下的字段需要再缩进 2 格作为 `r2` 的子字段（YAML 对缩进敏感）。

`prefix` 强烈建议设置，尤其在 bucket 与其他服务共用时。配置后所有上传、列出、删除、LRU 操作都限定在该前缀下，避免误删其他对象。留空时启动会输出 warning。

### 5. 重启与验证

```bash
sudo systemctl restart pixiv-feed-bot
sudo journalctl -u pixiv-feed-bot --since "2 min ago" | grep -iE 'r2 enabled|r2 stats'
```

预期日志：

```
INFO  R2 enabled: bucket=your-bucket-name, public=https://r2.your-domain.com
INFO  R2 stats: 0 objects, 0.00 GB / 80 GB           ← 30 秒后首次扫描
```

发送一个之前未发布过的画廊（已发布过的会命中 `telegraph_cache` 走原链接），等待 Telegra.ph URL 出现后，日志会显示：

```
INFO  R2 upload for e-hentai.org[gid/token]: 25/25 succeeded (0 fell back to nginx)
INFO  published e-hentai.org[gid/token] -> https://telegra.ph/...
```

在浏览器开发者工具的 Network 面板中确认图片 URL 指向 `https://r2.your-domain.com/...` 而非 `https://feed.your-domain.com/p/...`。

### 6. 容量管理

启用 R2 后，bot 会每隔 `lru_check_interval_minutes`（默认 60 分钟）扫描一次容量：超过 `capacity_gb` 的 90% 时触发清理，按上传时间从早到晚删除，直到降至 70%。

另一道护栏是单次发布体积上限 `max_upload_size_gb`（默认 1.0 GB）。当一次发布的总字节超过该阈值时跳过 R2 改走本地缓存，Telegra.ph 完成消息会附带：

> ⚠️ 此 Telegra.ph 因体积过大未上传 R2 持久化存储，最短 7 天 最长 30 天后图片可能失效。

管理员可在命令上追加 `--r2` 强制上传至 R2（详见 README「管理命令」一节）。

相关命令：

- `/stats system` — 显示 R2 占用、对象数、最旧 / 最新对象时间（UTC+8）、距上次扫描的时长。
- `/stats r2_evict` — 立即扫描 R2 并触发一次 LRU 清理，结果在同一条消息中返回。

### 7. R2 故障时的行为

R2 上传失败时，发布流程会自动回退到 Nginx URL：单图失败时仅该图回退，整批失败时全部回退。**发布过程不会因 R2 故障而失败。** 日志中会显示回退数量：

```
INFO  R2 upload for ...: 23/25 succeeded (2 fell back to nginx)
```

### 8. 启用 R2 之前发布的链接

启用 R2 之前发布的 Telegra.ph 页面中 `<img src>` 已写死指向 Nginx，不会自动迁移。本地 `cache_dir` 中仍存在的图片可继续使用；彻底迁移老链接需要后续工具支持，本版本暂未实现。

### 9. 计费提示

R2 计费（截至 2026 年）：

- **存储**：$0.015/GB/月 → 80GB ≈ $1.2/月
- **Class A**（PUT/LIST/DELETE）：每月免费 1 万次，超出后 $4.5/百万次
- **Class B**（GET/HEAD）：每月免费 1000 万次
- **出口流量**：免费（R2 相对 S3 的核心优势）

正常使用强度下，每小时一次的 LRU 扫描加上每次发布的 PUT 请求远低于免费额度。

## 本地 Bot API

Telegram 官方 Bot API 限制 `getFile` 20MB、`sendDocument` 50MB。使用 `/zip2tph`（接收用户上传的 zip）或 `/archive`（打包图集回传）时，文件大小几乎一定会超过此限制。需自建 [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) 服务以绕开限制。

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

`api_id` / `api_hash` 在 [my.telegram.org](https://my.telegram.org/) 申请（不是 Bot Token）。`--local` 参数必须指定，否则即使自建服务也会按 20MB 限制处理。

### 2. 从官方 API 切换到本地 API

Telegram 服务端要求：在切换到本地 Bot API 之前，必须先在官方 API 上调用一次 `logOut`，否则本地 API 会拒绝该 Bot 的 `getFile`，表现为 `Not Found`。

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

三项必须同时正确配置。`local_mode: true` 时客户端会在 `getFile` 后直接读取本地文件路径，绕开 HTTPS 20MB 下载限制。

### 4. 文件读权限

`--local` 模式下 telegram-bot-api 将上传的文件写入 `--dir` 目录（默认 `/var/lib/telegram-bot-api/<token>/...`）。运行 feed bot 的用户必须有读取权限：

```bash
chmod -R o+rX /var/lib/telegram-bot-api
```

出现 `Permission denied` 错误即说明此步骤遗漏。

### 5. 启动并验证

```bash
systemctl start pixiv-feed-bot
journalctl -u pixiv-feed-bot -n 50 --no-pager
```

切换后需重新发送 zip 给 bot 触发 `/zip2tph`——之前在官方 API 下生成的 `file_id` 在本地 API 中不可用。