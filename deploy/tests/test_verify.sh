#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT
mkdir -p "$TEST_ROOT/bin" "$TEST_ROOT/state"
export FAKE_STATE_DIR="$TEST_ROOT/state"

cat >"$TEST_ROOT/bin/sudo" <<'EOF_FAKE'
#!/usr/bin/env bash
exec "$@"
EOF_FAKE

cat >"$TEST_ROOT/bin/systemctl" <<'EOF_FAKE'
#!/usr/bin/env bash
exit 0
EOF_FAKE

cat >"$TEST_ROOT/bin/sleep" <<'EOF_FAKE'
#!/usr/bin/env bash
exit 0
EOF_FAKE

cat >"$TEST_ROOT/bin/curl" <<'EOF_FAKE'
#!/usr/bin/env bash
set -u
url="${!#}"
case "$url" in
    *:8080/api/health)
        key=backend
        payload='{"status":"ok"}'
        ;;
    *:3000/api/health)
        key=proxy
        payload='{"status":"ok"}'
        ;;
    *:3000/)
        key=frontend
        payload='<div id="app"></div>'
        ;;
    *)
        exit 22
        ;;
esac

count_file="$FAKE_STATE_DIR/$key"
count=0
[[ ! -f "$count_file" ]] || count="$(cat "$count_file")"
count=$((count + 1))
printf '%s\n' "$count" >"$count_file"

if [[ "${FAIL_ALL:-0}" == "1" ]]; then
    exit 7
fi
if [[ "$key" == "backend" && "$count" -lt 3 ]]; then
    exit 7
fi
printf '%s' "$payload"
EOF_FAKE

chmod +x "$TEST_ROOT/bin/"*
export PATH="$TEST_ROOT/bin:$PATH"

APP_NAME=taigi-agent-test \
BACKEND_PORT=8080 \
WEB_PORT=3000 \
VERIFY_ATTEMPTS=4 \
VERIFY_DELAY_SECONDS=0 \
bash "$REPO_DIR/deploy/verify.sh"

[[ "$(cat "$TEST_ROOT/state/backend")" == "3" ]] || {
    echo "verify did not retry backend readiness" >&2
    exit 1
}

rm -f "$TEST_ROOT/state/"*
if APP_NAME=taigi-agent-test \
   BACKEND_PORT=8080 \
   WEB_PORT=3000 \
   VERIFY_ATTEMPTS=2 \
   VERIFY_DELAY_SECONDS=0 \
   FAIL_ALL=1 \
   bash "$REPO_DIR/deploy/verify.sh" >/dev/null 2>&1; then
    echo "verify unexpectedly passed after retry exhaustion" >&2
    exit 1
fi

printf 'deployment readiness retry tests passed\n'
