#!/usr/bin/env bash
# 收到 GitHub webhook 后被 adnanh/webhook 调起。
# 流程：fetch → 无变更则直接通知；有变更则 reset，按需 pip install，
#       restart 服务，校验 systemd 状态。任一步失败立即通知 admin。
#
# 期望环境变量（来自 /etc/feed-bot-webhook/env，0600，仅 deploy 用户可读）：
#   TG_BOT_TOKEN   - 给 admin 推送通知用的 bot token（可以与 feed-bot 自己的 token 同/不同）
#   TG_ADMIN_ID    - 接收通知的 admin 数字 user_id
# 可选：
#   REPO_DIR       - 仓库 checkout 路径，默认 /opt/pixiv-feed-bot
#   SERVICE_NAME   - systemd 服务名，默认 pixiv-feed-bot
#   VENV_PIP       - venv 内 pip 路径，默认 ${REPO_DIR}/.venv/bin/pip
#   BRANCH         - 跟踪的远端分支，默认 main
#
# 退出码：成功 0；其它情况 1（并已发 admin 通知）。

set -uo pipefail

# 加载环境
ENV_FILE="${ENV_FILE:-/etc/feed-bot-webhook/env}"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
fi

REPO_DIR="${REPO_DIR:-/opt/pixiv-feed-bot}"
SERVICE_NAME="${SERVICE_NAME:-pixiv-feed-bot}"
VENV_PIP="${VENV_PIP:-${REPO_DIR}/.venv/bin/pip}"
BRANCH="${BRANCH:-main}"

LOG_FILE="$(mktemp -t feed-bot-deploy.XXXXXX.log)"
trap 'rm -f "$LOG_FILE"' EXIT

log() {
    # 同时写 stdout（journal 收）和 LOG_FILE（用作通知正文）
    printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

# Telegram 推送。失败不阻塞——通知失败时也不能掩盖真正错误。
notify() {
    local title="$1"
    local body="$2"
    if [[ -z "${TG_BOT_TOKEN:-}" || -z "${TG_ADMIN_ID:-}" ]]; then
        log "skip notify (TG_BOT_TOKEN / TG_ADMIN_ID not set)"
        return 0
    fi
    # Telegram 单条 4096 字符上限，正文截短
    if [[ "${#body}" -gt 3500 ]]; then
        body="${body:0:3500}"$'\n...(truncated)'
    fi
    curl -sS --max-time 15 \
        "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_ADMIN_ID}" \
        --data-urlencode "text=${title}"$'\n'"${body}" \
        --data-urlencode "disable_web_page_preview=true" \
        >/dev/null || log "notify curl failed (non-fatal)"
}

die() {
    local title="$1"
    shift
    log "ABORT: ${title}: $*"
    notify "❌ feed-bot deploy: ${title}" "$(tail -n 30 "$LOG_FILE")"
    exit 1
}

cd "$REPO_DIR" || die "cd repo" "REPO_DIR=$REPO_DIR not accessible"

OLD_REV=$(git rev-parse HEAD 2>/dev/null) || die "git rev-parse" "OLD_REV failed"
log "old HEAD: $OLD_REV"

if ! git fetch --quiet origin "$BRANCH" 2>>"$LOG_FILE"; then
    die "git fetch" "see log above"
fi

NEW_REV=$(git rev-parse "origin/${BRANCH}" 2>>"$LOG_FILE") || die "git rev-parse origin/${BRANCH}" ""
log "new HEAD: $NEW_REV"

if [[ "$OLD_REV" == "$NEW_REV" ]]; then
    log "already up to date, skip"
    notify "ℹ️ feed-bot deploy: 无新提交" "HEAD = $(git log -1 --format='%h %s')"
    exit 0
fi

# 推到新版本（hard reset 比 pull --ff-only 更明确；config.yaml 在 /etc 下，不受影响）
if ! git reset --hard "origin/${BRANCH}" >>"$LOG_FILE" 2>&1; then
    die "git reset --hard" ""
fi

CHANGED_LIST=$(git log --format='%h %s' "${OLD_REV}..${NEW_REV}" 2>>"$LOG_FILE" | head -n 15)
log "applying commits:
${CHANGED_LIST}"

# 检测 pyproject.toml 是否变了；变了才重装依赖
PYPROJECT_CHANGED=0
if git diff --name-only "${OLD_REV}..${NEW_REV}" 2>>"$LOG_FILE" | grep -q '^pyproject\.toml$'; then
    PYPROJECT_CHANGED=1
    log "pyproject.toml changed, running pip install -e ."
    if ! "$VENV_PIP" install -e . --quiet >>"$LOG_FILE" 2>&1; then
        die "pip install" "$VENV_PIP install -e . 失败"
    fi
fi

# systemctl restart 需要 sudoers 中给 deploy 用户开 NOPASSWD 的 restart pixiv-feed-bot 权限
log "restarting ${SERVICE_NAME}"
if ! sudo -n /bin/systemctl restart "$SERVICE_NAME" >>"$LOG_FILE" 2>&1; then
    die "systemctl restart" "sudoers 可能没配好；检查 /etc/sudoers.d/feed-bot-deploy"
fi

# 给服务点时间起来 + 健康校验
sleep 3
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    die "service not active after restart" "$(journalctl -u "$SERVICE_NAME" -n 40 --no-pager 2>&1 || true)"
fi

PYHINT=""
if [[ "$PYPROJECT_CHANGED" == "1" ]]; then
    PYHINT=$'\n(pyproject.toml 变更，已 pip install)'
fi

JOURNAL_TAIL=$(journalctl -u "$SERVICE_NAME" -n 10 --no-pager 2>/dev/null | tail -n 10 || true)
notify "✅ feed-bot deploy ok" "新增 commit：
${CHANGED_LIST}${PYHINT}

最近日志：
${JOURNAL_TAIL}"
log "done"
