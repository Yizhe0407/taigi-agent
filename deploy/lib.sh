#!/usr/bin/env bash

# Shared production deployment helpers. Callers must enable strict mode.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"

APP_NAME="${APP_NAME:-taigi-agent}"
APP_USER="${APP_USER:-$(id -un)}"
APP_GROUP="${APP_GROUP:-$(id -gn "$APP_USER")}"
DEPLOY_ROOT="${DEPLOY_ROOT:-/opt/$APP_NAME}"
RELEASES_DIR="$DEPLOY_ROOT/releases"
CURRENT_LINK="$DEPLOY_ROOT/current"
PREVIOUS_LINK="$DEPLOY_ROOT/previous"
STATE_DIR="${STATE_DIR:-/var/lib/$APP_NAME}"
ENV_DIR="${ENV_DIR:-/etc/$APP_NAME}"
ENV_FILE="${ENV_FILE:-$ENV_DIR/$APP_NAME.env}"
BACKEND_PORT="${BACKEND_PORT:-8080}"
WEB_PORT="${WEB_PORT:-3000}"
SYSTEMD_UNIT_NAME="${SYSTEMD_UNIT_NAME:-$APP_NAME.service}"
SYSTEMD_UNIT_PATH="/etc/systemd/system/$SYSTEMD_UNIT_NAME"
NGINX_SITE_NAME="${NGINX_SITE_NAME:-$APP_NAME}"
NGINX_SITE_PATH="/etc/nginx/sites-available/$NGINX_SITE_NAME"
FRONTEND_API_BASE_URL="${FRONTEND_API_BASE_URL:-}"

log() {
    printf '[%s] %s\n' "$APP_NAME" "$*"
}

die() {
    printf '[%s] ERROR: %s\n' "$APP_NAME" "$*" >&2
    exit 1
}

as_root() {
    if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "找不到必要指令：$1"
}

require_commands() {
    local command_name
    for command_name in "$@"; do
        require_command "$command_name"
    done
    if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
        require_command sudo
    fi
}

require_app_user() {
    local current_user
    current_user="$(id -un)"
    [[ "${EUID:-$(id -u)}" -ne 0 ]] || die "不要用 root 執行；請切換到正式服務帳號"
    [[ "$current_user" == "$APP_USER" ]] || die "請用 APP_USER=$APP_USER 執行；目前帳號是 $current_user"
}

require_source_repo() {
    [[ -d "$SOURCE_DIR/.git" ]] || die "$SOURCE_DIR 不是 Git repository"
    [[ -f "$SOURCE_DIR/backend/uv.lock" ]] || die "缺少 backend/uv.lock"
    [[ -f "$SOURCE_DIR/frontend/pnpm-lock.yaml" ]] || die "缺少 frontend/pnpm-lock.yaml"
}

require_clean_repo() {
    if [[ -n "$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=normal)" ]]; then
        git -C "$SOURCE_DIR" status --short >&2
        die "Git working tree 必須乾淨；請先 commit、stash 或移除未追蹤檔案"
    fi
}

env_value() {
    local file="$1"
    local key="$2"
    python3 - "$file" "$key" <<'PY'
import re
import sys

path, wanted = sys.argv[1:]
value = ""
with open(path, encoding="utf-8") as handle:
    for raw_line in handle:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match or match.group(1) != wanted:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
print(value)
PY
}

validate_env_file() {
    local file="$1"
    local key value value_lower pair first second first_value second_value
    local pairs=(
        "CF_ACCESS_CLIENT_ID:CF_ACCESS_CLIENT_SECRET"
        "CLOUDFLARE_TURN_KEY_ID:CLOUDFLARE_TURN_KEY_API_TOKEN"
        "ASR_BASE_URL:ASR_MODEL"
    )
    [[ -f "$file" ]] || die "環境檔不存在：$file"

    for key in LLM_BASE_URL LLM_MODEL ADMIN_TOKEN; do
        value="$(env_value "$file" "$key")"
        [[ -n "$value" ]] || die "$file 缺少必要設定：$key"
        value_lower="$(printf '%s' "$value" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
        case "$value_lower" in
            *replace-with*|*your-model*|*your-ollama*|*changeme*|*example.com*)
                die "$file 的 $key 仍是範例值"
                ;;
        esac
    done

    value="$(env_value "$file" ADMIN_TOKEN)"
    (( ${#value} >= 24 )) || die "ADMIN_TOKEN 至少需要 24 個字元"

    for pair in "${pairs[@]}"; do
        IFS=: read -r first second <<<"$pair"
        first_value="$(env_value "$file" "$first")"
        second_value="$(env_value "$file" "$second")"
        if [[ -n "$first_value" && -z "$second_value" ]] ||
           [[ -z "$first_value" && -n "$second_value" ]]; then
            die "$file 的 $first 與 $second 必須一起設定"
        fi
    done
}

ensure_layout() {
    as_root install -d -o "$APP_USER" -g "$APP_GROUP" -m 0755 "$DEPLOY_ROOT" "$RELEASES_DIR"
    as_root install -d -o "$APP_USER" -g "$APP_GROUP" -m 0700 "$STATE_DIR"
    as_root install -d -o root -g "$APP_GROUP" -m 0750 "$ENV_DIR"
}

install_initial_env() {
    local source_env="$SOURCE_DIR/backend/.env"
    [[ -f "$ENV_FILE" ]] && return 0
    validate_env_file "$source_env"
    log "安裝 production environment file：$ENV_FILE"
    as_root install -o "$APP_USER" -g "$APP_GROUP" -m 0600 "$source_env" "$ENV_FILE"
}

migrate_initial_state() {
    local source_state="$SOURCE_DIR/backend/.agent_state"
    [[ -d "$source_state" ]] || return 0
    [[ -z "$(find "$STATE_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]] || return 0
    log "移轉現有 runtime state 到 $STATE_DIR"
    rsync -a "$source_state/" "$STATE_DIR/"
    chmod -R go-rwx "$STATE_DIR"
}

render_template() {
    local source="$1"
    local destination="$2"
    python3 - "$source" "$destination" \
        "APP_USER=$APP_USER" \
        "APP_GROUP=$APP_GROUP" \
        "DEPLOY_ROOT=$DEPLOY_ROOT" \
        "ENV_FILE=$ENV_FILE" \
        "BACKEND_PORT=$BACKEND_PORT" \
        "WEB_PORT=$WEB_PORT" <<'PY'
import sys

source, destination, *pairs = sys.argv[1:]
text = open(source, encoding="utf-8").read()
for pair in pairs:
    key, value = pair.split("=", 1)
    text = text.replace(f"@@{key}@@", value)
if "@@" in text:
    raise SystemExit(f"unresolved template placeholder in {source}")
with open(destination, "w", encoding="utf-8") as handle:
    handle.write(text)
PY
}

install_service_configs() (
    local template_dir="${1:-$SCRIPT_DIR}"
    local unit_tmp nginx_tmp
    [[ -f "$template_dir/systemd/taigi-agent.service.in" ]] || die "缺少 systemd template：$template_dir"
    [[ -f "$template_dir/nginx/taigi-agent.conf.in" ]] || die "缺少 Nginx template：$template_dir"

    unit_tmp="$(mktemp)"
    nginx_tmp="$(mktemp)"
    trap 'rm -f "$unit_tmp" "$nginx_tmp"' EXIT

    render_template "$template_dir/systemd/taigi-agent.service.in" "$unit_tmp"
    render_template "$template_dir/nginx/taigi-agent.conf.in" "$nginx_tmp"

    as_root install -m 0644 "$unit_tmp" "$SYSTEMD_UNIT_PATH"
    as_root install -m 0644 "$nginx_tmp" "$NGINX_SITE_PATH"
    as_root ln -sfn "$NGINX_SITE_PATH" "/etc/nginx/sites-enabled/$NGINX_SITE_NAME"
    as_root nginx -t
    as_root systemctl daemon-reload
)

make_release_id() {
    local commit timestamp candidate suffix=0
    commit="$(git -C "$SOURCE_DIR" rev-parse --short=12 HEAD)"
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    candidate="$timestamp-$commit"
    while [[ -e "$RELEASES_DIR/$candidate" ]]; do
        ((suffix += 1))
        candidate="$timestamp-$commit-$suffix"
    done
    printf '%s\n' "$candidate"
}

build_release() (
    local release_id="$1"
    local destination="$RELEASES_DIR/$release_id"

    [[ ! -e "$destination" ]] || die "release 已存在：$release_id"
    trap 'rm -rf -- "$destination"' EXIT

    log "安裝 frontend dependencies 並建立 production build"
    (
        cd "$SOURCE_DIR/frontend"
        pnpm install --frozen-lockfile
        VITE_API_BASE_URL="$FRONTEND_API_BASE_URL" pnpm build
    )

    # Build the virtualenv at its final absolute path. Moving a completed venv
    # would leave console-script shebangs (including uvicorn) pointing at the
    # old staging path.
    log "建立 release：$release_id"
    mkdir -m 0755 "$destination"
    rsync -a \
        --exclude '/.git/' \
        --exclude '/backend/.env' \
        --exclude '/backend/.agent_state/' \
        --exclude '/backend/.venv/' \
        --exclude '/frontend/node_modules/' \
        --exclude '/frontend/dist/' \
        --exclude '/.DS_Store' \
        "$SOURCE_DIR/" "$destination/"
    mkdir -p "$destination/frontend/dist"
    rsync -a --delete "$SOURCE_DIR/frontend/dist/" "$destination/frontend/dist/"
    ln -s "$ENV_FILE" "$destination/backend/.env"
    ln -s "$STATE_DIR" "$destination/backend/.agent_state"

    log "同步 locked production Python dependencies"
    (
        cd "$destination/backend"
        uv sync --locked --no-dev
        .venv/bin/python -c 'import api; assert api.app.title == "Taigi Bus Agent API"'
    )

    touch "$destination/.release-complete"
    trap - EXIT
)

atomic_symlink() {
    local target="$1"
    local link_path="$2"
    local temp_link="${link_path}.new.$$"
    ln -s "$target" "$temp_link"
    mv -Tf "$temp_link" "$link_path"
}

activate_release() {
    local target="$1"
    local old_target=""
    [[ -d "$target" ]] || die "release 不存在：$target"
    [[ -f "$target/.release-complete" ]] || die "release 尚未完成：$target"

    if [[ -L "$CURRENT_LINK" ]]; then
        old_target="$(readlink -f "$CURRENT_LINK")"
    fi
    if [[ -n "$old_target" && "$old_target" != "$target" ]]; then
        atomic_symlink "$old_target" "$PREVIOUS_LINK"
    fi
    atomic_symlink "$target" "$CURRENT_LINK"
}

read_link_target() {
    local link_path="$1"
    if [[ -L "$link_path" ]]; then
        readlink -f "$link_path"
    fi
}

restore_previous_link() {
    local target="${1:-}"
    if [[ -n "$target" ]]; then
        atomic_symlink "$target" "$PREVIOUS_LINK"
    elif [[ -L "$PREVIOUS_LINK" ]]; then
        rm -f -- "$PREVIOUS_LINK"
    elif [[ -e "$PREVIOUS_LINK" ]]; then
        die "$PREVIOUS_LINK 存在但不是 symlink"
    fi
}

release_template_dir() {
    local release="$1"
    local template_dir="$release/deploy"
    [[ -f "$template_dir/systemd/taigi-agent.service.in" ]] || die "release 缺少 systemd template：$release"
    [[ -f "$template_dir/nginx/taigi-agent.conf.in" ]] || die "release 缺少 Nginx template：$release"
    printf '%s\n' "$template_dir"
}

restart_services() {
    as_root systemctl enable "$SYSTEMD_UNIT_NAME" >/dev/null
    as_root systemctl restart "$SYSTEMD_UNIT_NAME"
    as_root systemctl enable --now nginx >/dev/null
    as_root systemctl reload nginx
}

activate_and_verify_release() {
    local target="$1"
    local template_dir
    template_dir="$(release_template_dir "$target")" || return 1
    install_service_configs "$template_dir" &&
        activate_release "$target" &&
        restart_services &&
        "$SCRIPT_DIR/verify.sh"
}

restore_release_after_failure() {
    local target="$1"
    local previous_target="${2:-}"
    local status=0 template_dir

    log "恢復 $(basename "$target") 的程式與服務設定"
    template_dir="$(release_template_dir "$target")" || status=1
    activate_release "$target" || status=1
    restore_previous_link "$previous_target" || status=1
    if [[ -n "$template_dir" ]]; then
        install_service_configs "$template_dir" || status=1
    fi
    restart_services || status=1
    "$SCRIPT_DIR/verify.sh" || status=1
    return "$status"
}

release_path_from_argument() {
    local requested="${1:-previous}"
    local target
    if [[ "$requested" == "previous" ]]; then
        [[ -L "$PREVIOUS_LINK" ]] || die "找不到 previous release"
        readlink -f "$PREVIOUS_LINK"
        return
    fi
    target="$RELEASES_DIR/${requested##*/}"
    [[ -d "$target" ]] || die "release 不存在：$requested"
    [[ -f "$target/.release-complete" ]] || die "release 尚未完成：$requested"
    printf '%s\n' "$target"
}
