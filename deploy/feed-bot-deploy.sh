#!/usr/bin/env bash
# 收到 GitHub webhook 后被 adnanh/webhook 调起。
# 流程：fetch → 无变更则直接通知；有变更则 reset，按需 pip install，
#       restart 服务，校验 systemd 状态。任一步失败立即通知 admin。
#
# 通知凭据查找顺序：
#   1) /etc/feed-bot-webhook/env 里设置的 TG_BOT_TOKEN / TG_ADMIN_ID（override）
#   2) /etc/pixiv-feed-bot/config.yaml 的 telegram.token / auth.admin_users[0]（默认）
# 如果 base_url 在 config 里指了本地 Bot API，本脚本也会走那条 base 推送。
#
# 期望可选环境变量（来自 /etc/feed-bot-webhook/env，0600，仅 deploy 用户可读）：
#   TG_BOT_TOKEN     - 覆盖 config.yaml 里的 telegram.token
#   TG_ADMIN_ID      - 覆盖 config.yaml 里的 auth.admin_users[0]
#   TG_BOT_API_BASE  - 覆盖 telegram.base_url（含 /bot 前缀的形式，如
#                      http://127.0.0.1:8081/bot；为空时回退到官方 API）
#   BOT_CONFIG_YAML  - bot 配置路径，默认 /etc/pixiv-feed-bot/config.yaml
#   REPO_DIR         - 仓库 checkout 路径，默认 /opt/pixiv-feed-bot
#   SERVICE_NAME     - systemd 服务名，默认 pixiv-feed-bot
#   VENV_PIP         - venv 内 pip 路径，默认 ${REPO_DIR}/.venv/bin/pip
#   VENV_PYTHON      - venv 内 python 路径，默认 ${REPO_DIR}/.venv/bin/python
#   BRANCH           - 跟踪的远端分支，默认 main
#
# deploy 用户需要能读 BOT_CONFIG_YAML 才能拿到默认凭据；不能读时回落到 env 文件，
# 都没有就只写 journal、不发 TG。
#
# 退出码：成功 0；其它情况 1（且已尽力发 admin 通知）。

set -uo pipefail

# ---------------------------------------------------------------------------
# 加载环境
# ---------------------------------------------------------------------------

ENV_FILE="${ENV_FILE:-/etc/feed-bot-webhook/env}"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
fi

REPO_DIR="${REPO_DIR:-/opt/pixiv-feed-bot}"
SERVICE_NAME="${SERVICE_NAME:-pixiv-feed-bot}"
VENV_PIP="${VENV_PIP:-${REPO_DIR}/.venv/bin/pip}"
VENV_PYTHON="${VENV_PYTHON:-${REPO_DIR}/.venv/bin/python}"
BRANCH="${BRANCH:-main}"
BOT_CONFIG_YAML="${BOT_CONFIG_YAML:-/etc/pixiv-feed-bot/config.yaml}"

# 从 config.yaml 回填默认 TG_*（env 文件里已显式设置的不动）
if [[ -r "$BOT_CONFIG_YAML" && -x "$VENV_PYTHON" ]]; then
    _CFG_LINES=$(BOT_CONFIG_YAML="$BOT_CONFIG_YAML" "$VENV_PYTHON" - <<'PYEOF' 2>/dev/null || true
import os, sys
try:
    import yaml
    with open(os.environ["BOT_CONFIG_YAML"]) as f:
        c = yaml.safe_load(f) or {}
    tg = c.get("telegram") or {}
    auth = c.get("auth") or {}
    admins = auth.get("admin_users") or []
    print(tg.get("token") or "")
    print(admins[0] if admins else "")
    print(tg.get("base_url") or "")
except Exception:
    print(); print(); print()
PYEOF
)
    # 三行：token / admin / base
    _CFG_TOKEN=$(awk 'NR==1' <<<"$_CFG_LINES")
    _CFG_ADMIN=$(awk 'NR==2' <<<"$_CFG_LINES")
    _CFG_BASE=$(awk 'NR==3' <<<"$_CFG_LINES")
    TG_BOT_TOKEN="${TG_BOT_TOKEN:-$_CFG_TOKEN}"
    TG_ADMIN_ID="${TG_ADMIN_ID:-$_CFG_ADMIN}"
    TG_BOT_API_BASE="${TG_BOT_API_BASE:-$_CFG_BASE}"
fi

LOG_FILE="$(mktemp -t feed-bot-deploy.XXXXXX.log)"
trap 'rm -f "$LOG_FILE"' EXIT

log() {
    # 同时写 stdout（journal 收）和 LOG_FILE（用作通知正文）
    printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

# ---------------------------------------------------------------------------
# Telegram 通知。失败不阻塞——通知失败时也不能掩盖真正错误。
# ---------------------------------------------------------------------------

notify() {
    local title="$1"
    local body="$2"
    if [[ -z "${TG_BOT_TOKEN:-}" || -z "${TG_ADMIN_ID:-}" ]]; then
        log "skip notify (no TG creds in env nor in $BOT_CONFIG_YAML)"
        return 0
    fi
    # Telegram 单条 4096 字符上限，正文截短
    if [[ "${#body}" -gt 3500 ]]; then
        body="${body:0:3500}"$'\n...(truncated)'
    fi
    local api_url
    if [[ -n "${TG_BOT_API_BASE:-}" ]]; then
        # config 的 base_url 形如 http://127.0.0.1:8081/bot（含 /bot 前缀，token 直接拼）
        api_url="${TG_BOT_API_BASE}${TG_BOT_TOKEN}/sendMessage"
    else
        api_url="https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage"
    fi
    curl -sS --max-time 15 \
        "$api_url" \
        --data-urlencode "chat_id=${TG_ADMIN_ID}" \
        --data-urlencode "text=${title}"$'\n\n'"${body}" \
        --data-urlencode "disable_web_page_preview=true" \
        >/dev/null || log "notify curl failed (non-fatal)"
}

# ---------------------------------------------------------------------------
# 用于失败/成功通知的"上下文摘要"——版本、commit、文件统计、tag。
# 必须在已经 git fetch 之后调；OLD_REV/NEW_REV 已知。
# 失败路径下可能某些信息为空，正常打印空字符串就好。
# ---------------------------------------------------------------------------

# 从指定 commit 的 pyproject.toml 取出 version 行的值；失败返回空
_pyproject_version_at() {
    local rev="$1"
    git show "${rev}:pyproject.toml" 2>/dev/null \
        | awk -F'"' '/^version[[:space:]]*=/ {print $2; exit}'
}

# 拼接出"变更摘要"段，含版本/HEAD/commit 列表/文件统计/tag。
build_summary() {
    local old_rev="$1"
    local new_rev="$2"

    local lines=""

    # 版本号变化
    local old_ver new_ver ver_line=""
    old_ver=$(_pyproject_version_at "$old_rev")
    new_ver=$(_pyproject_version_at "$new_rev")
    if [[ -n "$old_ver" && -n "$new_ver" ]]; then
        if [[ "$old_ver" != "$new_ver" ]]; then
            ver_line="版本：${old_ver} → ${new_ver}"
        else
            ver_line="版本：${new_ver}（未变）"
        fi
        lines+="${ver_line}"$'\n'
    fi

    # HEAD 短哈希
    local old_short new_short
    old_short=$(git rev-parse --short=8 "$old_rev" 2>/dev/null || echo "$old_rev")
    new_short=$(git rev-parse --short=8 "$new_rev" 2>/dev/null || echo "$new_rev")
    lines+="HEAD：${old_short} → ${new_short}"$'\n'

    # tag（仅当 tag 直接指向 new_rev 时显示）
    local tags
    tags=$(git tag --points-at "$new_rev" 2>/dev/null | head -n 5 | paste -sd ' ' -)
    if [[ -n "$tags" ]]; then
        lines+="Tags：${tags}"$'\n'
    fi

    # commit 列表
    local commit_count commit_list
    commit_count=$(git rev-list --count "${old_rev}..${new_rev}" 2>/dev/null || echo 0)
    commit_list=$(git log --format='  %h %s' "${old_rev}..${new_rev}" 2>/dev/null | head -n 15)
    if [[ "$commit_count" -gt 0 ]]; then
        lines+=$'\n'"提交（${commit_count}）："$'\n'"${commit_list}"$'\n'
        if [[ "$commit_count" -gt 15 ]]; then
            lines+="  ...（其余 $((commit_count - 15)) 条省略）"$'\n'
        fi
    fi

    # 文件统计
    local stat_line file_list file_total
    stat_line=$(git diff --shortstat "${old_rev}..${new_rev}" 2>/dev/null | sed 's/^[[:space:]]*//')
    file_list=$(git diff --name-only "${old_rev}..${new_rev}" 2>/dev/null | head -n 12)
    file_total=$(git diff --name-only "${old_rev}..${new_rev}" 2>/dev/null | wc -l | tr -d ' ')
    if [[ -n "$stat_line" ]]; then
        lines+=$'\n'"变更：${stat_line}"$'\n'
        if [[ -n "$file_list" ]]; then
            lines+="$(echo "$file_list" | sed 's/^/  /')"$'\n'
            if [[ "$file_total" -gt 12 ]]; then
                lines+="  ...（其余 $((file_total - 12)) 个文件省略）"$'\n'
            fi
        fi
    fi

    printf '%s' "$lines"
}

die() {
    local title="$1"
    shift
    log "ABORT: ${title}: $*"
    local body=""
    if [[ -n "${OLD_REV:-}" && -n "${NEW_REV:-}" ]]; then
        body+="$(build_summary "$OLD_REV" "$NEW_REV")"
        body+=$'\n'
    fi
    body+="错误尾部日志："$'\n'"$(tail -n 25 "$LOG_FILE")"
    notify "❌ feed-bot deploy: ${title}" "$body"
    exit 1
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

cd "$REPO_DIR" || die "cd repo" "REPO_DIR=$REPO_DIR not accessible"

OLD_REV=$(git rev-parse HEAD 2>/dev/null) || die "git rev-parse" "OLD_REV failed"
log "old HEAD: $OLD_REV"

if ! git fetch --quiet --tags origin "$BRANCH" 2>>"$LOG_FILE"; then
    die "git fetch" "see log above"
fi

NEW_REV=$(git rev-parse "origin/${BRANCH}" 2>>"$LOG_FILE") || die "git rev-parse origin/${BRANCH}" ""
log "new HEAD: $NEW_REV"

if [[ "$OLD_REV" == "$NEW_REV" ]]; then
    log "already up to date, skip"
    notify "ℹ️ feed-bot deploy: 无新提交" "$(build_summary "$OLD_REV" "$NEW_REV")"
    exit 0
fi

# 推到新版本（hard reset 比 pull --ff-only 更明确；config.yaml 在 /etc 下，不受影响）
if ! git reset --hard "origin/${BRANCH}" >>"$LOG_FILE" 2>&1; then
    die "git reset --hard" ""
fi

log "applying commits:"
git log --format='  %h %s' "${OLD_REV}..${NEW_REV}" 2>>"$LOG_FILE" | tee -a "$LOG_FILE" >/dev/null

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

# 成功通知
SUMMARY=$(build_summary "$OLD_REV" "$NEW_REV")
PIP_HINT=""
if [[ "$PYPROJECT_CHANGED" == "1" ]]; then
    PIP_HINT=$'\n📦 pyproject.toml 已变更，已重新 pip install -e .'
fi
JOURNAL_TAIL=$(journalctl -u "$SERVICE_NAME" -n 10 --no-pager 2>/dev/null | tail -n 10 || true)

notify "✅ feed-bot deploy ok" "${SUMMARY}${PIP_HINT}

最近日志：
${JOURNAL_TAIL}"
log "done"
