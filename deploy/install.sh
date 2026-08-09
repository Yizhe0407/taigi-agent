#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$SCRIPT_DIR/lib.sh"

require_app_user
require_commands git uv pnpm node python3 rsync curl nginx systemctl install mktemp tr
require_source_repo
require_clean_repo

ensure_layout
install_initial_env
validate_env_file "$ENV_FILE"
as_root chown "$APP_USER:$APP_GROUP" "$ENV_FILE"
as_root chmod 0600 "$ENV_FILE"
migrate_initial_state

old_release="$(read_link_target "$CURRENT_LINK")"
old_previous="$(read_link_target "$PREVIOUS_LINK")"
release_id="$(make_release_id)"
build_release "$release_id"
release_path="$RELEASES_DIR/$release_id"

if ! activate_and_verify_release "$release_path"; then
    if [[ -n "$old_release" && -d "$old_release" ]]; then
        log "安裝驗證失敗，回切 $(basename "$old_release")"
        restore_release_after_failure "$old_release" "$old_previous" || \
            log "警告：舊 release 自動恢復失敗，請立即檢查 systemd 與 Nginx"
    fi
    exit 1
fi

log "安裝完成：$release_id"
