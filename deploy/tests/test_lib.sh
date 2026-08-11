#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT

export APP_NAME=taigi-agent-test
export APP_USER="$(id -un)"
export APP_GROUP="$(id -gn)"
export DEPLOY_ROOT="$TEST_ROOT/deploy"
export STATE_DIR="$TEST_ROOT/state"
export ENV_DIR="$TEST_ROOT/etc"
export ENV_FILE="$ENV_DIR/taigi-agent.env"

# shellcheck source=deploy/lib.sh
source "$REPO_DIR/deploy/lib.sh"

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

write_required_env() {
    local destination="$1"
    cat >"$destination" <<'ENV'
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_MODEL=qwen-4b
ADMIN_TOKEN=abcdefghijklmnopqrstuvwxyz012345
ENV
}

good_env="$TEST_ROOT/good.env"
write_required_env "$good_env"
cat >>"$good_env" <<'ENV'
CF_ACCESS_CLIENT_ID=client-id
CF_ACCESS_CLIENT_SECRET=client-secret
ASR_BASE_URL=http://127.0.0.1:8001
ASR_MODEL=asr-model
ENV
validate_env_file "$good_env"

bad_cf_env="$TEST_ROOT/bad-cf.env"
write_required_env "$bad_cf_env"
printf 'CF_ACCESS_CLIENT_ID=client-id\n' >>"$bad_cf_env"
(validate_env_file "$bad_cf_env") >/dev/null 2>&1 && fail "incomplete Cloudflare Access pair passed validation"

bad_turn_env="$TEST_ROOT/bad-turn.env"
cp "$good_env" "$bad_turn_env"
printf 'CLOUDFLARE_TURN_KEY_ID=turn-key-id\n' >>"$bad_turn_env"
(validate_env_file "$bad_turn_env") >/dev/null 2>&1 && fail "incomplete Cloudflare TURN pair passed validation"

bad_asr_env="$TEST_ROOT/bad-asr.env"
write_required_env "$bad_asr_env"
printf 'ASR_MODEL=asr-model\n' >>"$bad_asr_env"
(validate_env_file "$bad_asr_env") >/dev/null 2>&1 && fail "incomplete ASR pair passed validation"

# Keep the transaction test portable to the macOS development host. Production
# uses the GNU mv-based atomic_symlink implementation on Ubuntu.
atomic_symlink() {
    local target="$1"
    local link_path="$2"
    rm -f -- "$link_path"
    ln -s "$target" "$link_path"
}

readlink() {
    if [[ "${1:-}" == "-f" ]]; then
        shift
        python3 - "$1" <<'PY'
import os
import sys
print(os.path.realpath(sys.argv[1]))
PY
    else
        command readlink "$@"
    fi
}

installed_template_dir=""
install_service_configs() {
    installed_template_dir="$1"
}

FAIL_RESTART_FOR=""
restart_services() {
    local active
    active="$(readlink -f "$CURRENT_LINK")"
    [[ -z "$FAIL_RESTART_FOR" || "$active" != "$FAIL_RESTART_FOR" ]]
}

runtime_dir="$TEST_ROOT/runtime"
mkdir -p "$runtime_dir"
cat >"$runtime_dir/verify.sh" <<'EOF_VERIFY'
#!/usr/bin/env bash
exit 0
EOF_VERIFY
chmod +x "$runtime_dir/verify.sh"
SCRIPT_DIR="$runtime_dir"

mkdir -p "$RELEASES_DIR"
for release_name in older old new; do
    release="$RELEASES_DIR/$release_name"
    mkdir -p "$release/deploy/systemd" "$release/deploy/nginx"
    : >"$release/deploy/systemd/taigi-agent.service.in"
    : >"$release/deploy/nginx/taigi-agent.conf.in"
    : >"$release/.release-complete"
done

older_release="$(readlink -f "$RELEASES_DIR/older")"
old_release="$(readlink -f "$RELEASES_DIR/old")"
new_release="$(readlink -f "$RELEASES_DIR/new")"
atomic_symlink "$old_release" "$CURRENT_LINK"
atomic_symlink "$older_release" "$PREVIOUS_LINK"

activate_and_verify_release "$new_release"
[[ "$(readlink -f "$CURRENT_LINK")" == "$new_release" ]] || fail "new release was not activated"
[[ "$(readlink -f "$PREVIOUS_LINK")" == "$old_release" ]] || fail "previous link was not updated"
[[ "$installed_template_dir" == "$new_release/deploy" ]] || fail "new release templates were not installed"

atomic_symlink "$old_release" "$CURRENT_LINK"
atomic_symlink "$older_release" "$PREVIOUS_LINK"
FAIL_RESTART_FOR="$new_release"
if activate_and_verify_release "$new_release"; then
    fail "simulated restart failure unexpectedly passed"
fi
restore_release_after_failure "$old_release" "$older_release"
[[ "$(readlink -f "$CURRENT_LINK")" == "$old_release" ]] || fail "old release was not restored"
[[ "$(readlink -f "$PREVIOUS_LINK")" == "$older_release" ]] || fail "previous link was not restored"
[[ "$installed_template_dir" == "$old_release/deploy" ]] || fail "old release templates were not restored"

printf 'deployment helper tests passed\n'
