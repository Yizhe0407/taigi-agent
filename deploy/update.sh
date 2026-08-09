#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$SCRIPT_DIR/lib.sh"

require_app_user
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
DEPLOY_REMOTE="${DEPLOY_REMOTE:-origin}"

require_commands git uv pnpm node python3 rsync curl nginx systemctl install mktemp tr
require_source_repo
require_clean_repo
[[ -f "$ENV_FILE" ]] || die "尚未安裝；請先執行 deploy/install.sh"
validate_env_file "$ENV_FILE"

current_branch="$(git -C "$SOURCE_DIR" branch --show-current)"
[[ "$current_branch" == "$DEPLOY_BRANCH" ]] || die "目前分支是 $current_branch，預期為 $DEPLOY_BRANCH"

log "取得 $DEPLOY_REMOTE/$DEPLOY_BRANCH"
git -C "$SOURCE_DIR" fetch "$DEPLOY_REMOTE" "$DEPLOY_BRANCH"
git -C "$SOURCE_DIR" merge --ff-only "$DEPLOY_REMOTE/$DEPLOY_BRANCH"
require_clean_repo

old_release="$(read_link_target "$CURRENT_LINK")"
old_previous="$(read_link_target "$PREVIOUS_LINK")"
[[ -n "$old_release" ]] || die "找不到 current release；請先執行 deploy/install.sh"

release_id="$(make_release_id)"
build_release "$release_id"
release_path="$RELEASES_DIR/$release_id"

if ! activate_and_verify_release "$release_path"; then
    log "更新失敗，回切 $(basename "$old_release")"
    restore_release_after_failure "$old_release" "$old_previous" || \
        log "警告：舊 release 自動恢復失敗，請立即檢查 systemd 與 Nginx"
    exit 1
fi

log "更新完成：$release_id"
