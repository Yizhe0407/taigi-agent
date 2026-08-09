#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$SCRIPT_DIR/lib.sh"

require_commands curl grep systemctl

log "確認 systemd service"
as_root systemctl is-active --quiet "$SYSTEMD_UNIT_NAME" || {
    as_root systemctl --no-pager --full status "$SYSTEMD_UNIT_NAME" || true
    die "$SYSTEMD_UNIT_NAME 未正常執行"
}

log "確認 backend health endpoint"
backend_response="$(curl --fail --silent --show-error --max-time 10 "http://127.0.0.1:$BACKEND_PORT/api/health")"
grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<<"$backend_response" || die "backend health response 不正確"

log "確認 Nginx health proxy"
proxy_response="$(curl --fail --silent --show-error --max-time 10 "http://127.0.0.1:$WEB_PORT/api/health")"
grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<<"$proxy_response" || die "Nginx health proxy response 不正確"

log "確認 frontend SPA"
frontend_response="$(curl --fail --silent --show-error --max-time 10 "http://127.0.0.1:$WEB_PORT/")"
grep -q '<div id="app"></div>' <<<"$frontend_response" || die "frontend index.html 不正確"

log "部署驗證通過"
