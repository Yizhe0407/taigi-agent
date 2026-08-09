#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$SCRIPT_DIR/lib.sh"

require_commands curl grep sleep systemctl

VERIFY_ATTEMPTS="${VERIFY_ATTEMPTS:-30}"
VERIFY_DELAY_SECONDS="${VERIFY_DELAY_SECONDS:-1}"

fetch_with_retry() {
    local label="$1"
    local url="$2"
    local attempt response

    for ((attempt = 1; attempt <= VERIFY_ATTEMPTS; attempt += 1)); do
        if response="$(curl --fail --silent --show-error --max-time 10 "$url" 2>/dev/null)"; then
            printf '%s' "$response"
            return 0
        fi
        if (( attempt < VERIFY_ATTEMPTS )); then
            sleep "$VERIFY_DELAY_SECONDS"
        fi
    done

    curl --fail --silent --show-error --max-time 10 "$url" >/dev/null || true
    die "$label 在 $VERIFY_ATTEMPTS 次嘗試後仍無法連線"
}

log "確認 systemd service"
as_root systemctl is-active --quiet "$SYSTEMD_UNIT_NAME" || {
    as_root systemctl --no-pager --full status "$SYSTEMD_UNIT_NAME" || true
    die "$SYSTEMD_UNIT_NAME 未正常執行"
}

log "確認 backend health endpoint"
backend_response="$(fetch_with_retry "backend health endpoint" "http://127.0.0.1:$BACKEND_PORT/api/health")"
grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<<"$backend_response" || die "backend health response 不正確"

log "確認 Nginx health proxy"
proxy_response="$(fetch_with_retry "Nginx health proxy" "http://127.0.0.1:$WEB_PORT/api/health")"
grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<<"$proxy_response" || die "Nginx health proxy response 不正確"

log "確認 frontend SPA"
frontend_response="$(fetch_with_retry "frontend SPA" "http://127.0.0.1:$WEB_PORT/")"
grep -q '<div id="app"></div>' <<<"$frontend_response" || die "frontend index.html 不正確"

log "部署驗證通過"
