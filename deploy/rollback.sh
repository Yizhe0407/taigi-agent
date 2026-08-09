#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$SCRIPT_DIR/lib.sh"

require_app_user
require_commands curl nginx systemctl install mktemp python3
[[ -L "$CURRENT_LINK" ]] || die "目前沒有 active release"

target="$(release_path_from_argument "${1:-previous}")"
old_release="$(read_link_target "$CURRENT_LINK")"
old_previous="$(read_link_target "$PREVIOUS_LINK")"
[[ "$target" != "$old_release" ]] || die "指定 release 已經是 current"

log "回滾到 $(basename "$target")"
if ! activate_and_verify_release "$target"; then
    log "回滾驗證失敗，恢復 $(basename "$old_release")"
    restore_release_after_failure "$old_release" "$old_previous" || \
        log "警告：原 release 自動恢復失敗，請立即檢查 systemd 與 Nginx"
    exit 1
fi

log "回滾完成：$(basename "$target")"
