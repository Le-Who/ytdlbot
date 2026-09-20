#!/bin/sh
set -eu

: "${PROJECT_ROOT:=/opt/ytdlbot}"
: "${RELEASE_SHA:?RELEASE_SHA is required}"
: "${EXPECTED_BRANCH_SHA:?EXPECTED_BRANCH_SHA is required}"
: "${BOT_IMAGE:?BOT_IMAGE is required}"
: "${COMPOSE_PROJECT:?COMPOSE_PROJECT is required}"
: "${PUBLIC_BASE_URL:?PUBLIC_BASE_URL is required}"
: "${RELEASE_DIR:=$PROJECT_ROOT/releases/$RELEASE_SHA}"
: "${DEPLOY_STATE_DIR:=$PROJECT_ROOT/.deploy}"
: "${PYTHON_BIN:=python3}"
: "${CURL_BIN:=curl}"
: "${MV_BIN:=mv}"
: "${DEPLOY_HEALTH_ATTEMPTS:=45}"
: "${DEPLOY_HEALTH_INTERVAL_SECONDS:=2}"
: "${LOCAL_READY_URL:=http://127.0.0.1:8000/health/ready}"

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

require_sha() {
  printf '%s\n' "$1" | grep -Eq '^[0-9a-f]{40}$' ||
    fail "$2 must be a full lowercase Git SHA."
}

require_sha "$RELEASE_SHA" RELEASE_SHA
require_sha "$EXPECTED_BRANCH_SHA" EXPECTED_BRANCH_SHA
case "$PROJECT_ROOT" in
  /*) ;;
  *) fail "PROJECT_ROOT must be absolute." ;;
esac
case "$PROJECT_ROOT" in
  /|//*|*/|*//*|*/./*|*/.|*/../*|*/..|*[!A-Za-z0-9._/-]*)
    fail "PROJECT_ROOT must be a normalized shell-safe project path."
    ;;
esac
[ "$RELEASE_DIR" = "$PROJECT_ROOT/releases/$RELEASE_SHA" ] ||
  fail "RELEASE_DIR must be the exact project-scoped release path."
[ "$DEPLOY_STATE_DIR" = "$PROJECT_ROOT/.deploy" ] ||
  fail "DEPLOY_STATE_DIR must remain inside the verified project."
case "$COMPOSE_PROJECT" in
  ''|*[!A-Za-z0-9_.-]*) fail "COMPOSE_PROJECT is invalid." ;;
esac
case "$PUBLIC_BASE_URL" in
  https://*) ;;
  *) fail "PUBLIC_BASE_URL must be an https origin." ;;
esac
public_authority=${PUBLIC_BASE_URL#https://}
case "$public_authority" in
  ''|*[[:space:]]*|*/*)
    fail "PUBLIC_BASE_URL must not contain a path or whitespace."
    ;;
esac
case "$DEPLOY_HEALTH_ATTEMPTS:$DEPLOY_HEALTH_INTERVAL_SECONDS" in
  *[!0-9:]*) fail "Deployment health settings must be non-negative integers." ;;
esac
[ "$DEPLOY_HEALTH_ATTEMPTS" -gt 0 ] ||
  fail "DEPLOY_HEALTH_ATTEMPTS must be positive."

if [ "$EXPECTED_BRANCH_SHA" != "$RELEASE_SHA" ]; then
  printf 'Skipping stale release %s; branch HEAD is %s.\n' \
    "$RELEASE_SHA" "$EXPECTED_BRANCH_SHA"
  exit 0
fi

mkdir -p "$DEPLOY_STATE_DIR"
[ ! -L "$DEPLOY_STATE_DIR" ] ||
  fail "Deployment state directory must not be a symlink."
umask 077
exec 9>"$DEPLOY_STATE_DIR/deploy.lock"
if ! flock -n 9; then
  printf '%s\n' "Another project release is already running." >&2
  exit 1
fi

"$RELEASE_DIR/scripts/preflight-production.sh"

cd "$PROJECT_ROOT"
bot_container=$(BOT_IMAGE="$BOT_IMAGE" APP_RELEASE="$RELEASE_SHA" \
  docker compose -p "$COMPOSE_PROJECT" ps -q bot)
[ -n "$bot_container" ] || fail "Current bot container is unavailable."
previous_image=$(docker inspect --format '{{.Image}}' "$bot_container")
previous_release=$(docker inspect --format \
  '{{range .Config.Env}}{{println .}}{{end}}' "$bot_container" |
  sed -n 's/^APP_RELEASE=//p' | sed -n '1p')
[ -n "$previous_image" ] ||
  fail "Current bot image cannot be captured for rollback."
case "$previous_release" in
  ''|*[!A-Za-z0-9._-]*) previous_release=legacy ;;
esac

previous_health_contract=''
previous_health_url=''
previous_ready_body=$("$CURL_BIN" --fail --silent --show-error --max-time 5 \
  "$LOCAL_READY_URL" || true)
if printf '%s' "$previous_ready_body" | "$PYTHON_BIN" -c '
import json
import sys

expected = sys.argv[1]
try:
    payload = json.load(sys.stdin)
except (ValueError, TypeError):
    raise SystemExit(1)
raise SystemExit(
    0 if payload.get("ready") is True and payload.get("release") == expected else 1
)
' "$previous_release"; then
  previous_health_contract=release-ready
  previous_health_url=$LOCAL_READY_URL
else
  legacy_health_url=${LOCAL_READY_URL%/health/ready}/health
  previous_live_body=$("$CURL_BIN" --fail --silent --show-error --max-time 5 \
    "$legacy_health_url" || true)
  if printf '%s' "$previous_live_body" | "$PYTHON_BIN" -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except (ValueError, TypeError):
    raise SystemExit(1)
raise SystemExit(0 if payload.get("ok") is True else 1)
'; then
    previous_health_contract=legacy-health
    previous_health_url=$legacy_health_url
  else
    fail "Current bot has no bounded health contract for safe rollback."
  fi
fi

rollback_compose_tmp="$DEPLOY_STATE_DIR/rollback-compose.yml.tmp.$$"
rollback_manifest_tmp="$DEPLOY_STATE_DIR/rollback.manifest.tmp.$$"
rollback_override_tmp="$DEPLOY_STATE_DIR/rollback-bot.override.yml.tmp.$$"
current_manifest="$DEPLOY_STATE_DIR/current.manifest"
rollback_current_manifest="$DEPLOY_STATE_DIR/rollback-current.manifest"
previous_current_manifest_state=absent
if [ -e "$current_manifest" ] || [ -L "$current_manifest" ]; then
  [ -f "$current_manifest" ] && [ ! -L "$current_manifest" ] ||
    fail "Current release manifest is unsafe."
  rollback_current_tmp="$rollback_current_manifest.tmp.$$"
  cp "$current_manifest" "$rollback_current_tmp"
  mv -f "$rollback_current_tmp" "$rollback_current_manifest"
  previous_current_manifest_state=present
fi
cp "$PROJECT_ROOT/docker-compose.yml" "$rollback_compose_tmp"
mv -f "$rollback_compose_tmp" "$DEPLOY_STATE_DIR/rollback-compose.yml"
{
  printf 'services:\n'
  printf '  bot:\n'
  printf '%s\n' '    image: "${ROLLBACK_BOT_IMAGE:?captured previous bot image required}"'
} >"$rollback_override_tmp"
mv -f "$rollback_override_tmp" "$DEPLOY_STATE_DIR/rollback-bot.override.yml"
{
  printf 'RELEASE_SHA=%s\n' "$previous_release"
  printf 'BOT_IMAGE=%s\n' "$previous_image"
  printf 'COMPOSE_PROJECT=%s\n' "$COMPOSE_PROJECT"
  printf 'COMPOSE_FILE=%s\n' "$DEPLOY_STATE_DIR/rollback-compose.yml"
  printf 'OVERRIDE_FILE=%s\n' "$DEPLOY_STATE_DIR/rollback-bot.override.yml"
  printf 'HEALTH_CONTRACT=%s\n' "$previous_health_contract"
  printf 'HEALTH_URL=%s\n' "$previous_health_url"
  printf 'CURRENT_MANIFEST_STATE=%s\n' "$previous_current_manifest_state"
  printf 'CURRENT_MANIFEST_FILE=%s\n' "$rollback_current_manifest"
} >"$rollback_manifest_tmp"
mv -f "$rollback_manifest_tmp" "$DEPLOY_STATE_DIR/rollback.manifest"

docker pull "$BOT_IMAGE"

ROLLBACK_ARMED=1
on_exit() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$status" -ne 0 ] && [ "$ROLLBACK_ARMED" -eq 1 ]; then
    printf 'Activation failed; restoring the previous bot image and config.\n' >&2
    if ! DEPLOY_LOCK_HELD=1 \
      "$RELEASE_DIR/scripts/rollback-release.sh"; then
      printf 'Rollback failed; inspect project-scoped release state.\n' >&2
    fi
  fi
  exit "$status"
}
trap on_exit EXIT
trap 'exit 130' HUP INT TERM

candidate_tmp="$PROJECT_ROOT/.docker-compose.candidate.$$"
cp "$RELEASE_DIR/docker-compose.yml" "$candidate_tmp"
"$MV_BIN" -f "$candidate_tmp" "$PROJECT_ROOT/docker-compose.yml"

export APP_RELEASE="$RELEASE_SHA"
export BOT_IMAGE
docker compose -p "$COMPOSE_PROJECT" up -d --no-deps --no-build bot

check_ready() {
  ready_url=$1
  attempt=1
  while [ "$attempt" -le "$DEPLOY_HEALTH_ATTEMPTS" ]; do
    body=$("$CURL_BIN" --fail --silent --show-error --max-time 5 "$ready_url" || true)
    if printf '%s' "$body" | "$PYTHON_BIN" -c '
import json
import sys

expected = sys.argv[1]
try:
    payload = json.load(sys.stdin)
except (ValueError, TypeError):
    raise SystemExit(1)
local = payload.get("local_bot_api") or {}
healthy = (
    payload.get("ready") is True
    and payload.get("release") == expected
    and local.get("required") is True
    and local.get("configured") is True
    and local.get("functional_probe") is True
)
raise SystemExit(0 if healthy else 1)
' "$RELEASE_SHA"; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep "$DEPLOY_HEALTH_INTERVAL_SECONDS"
  done
  return 1
}

check_ready "$LOCAL_READY_URL" || fail "Local readiness/version check failed."
check_ready "${PUBLIC_BASE_URL%/}/health/ready" ||
  fail "Public readiness/version check failed."

docker compose -p "$COMPOSE_PROJECT" exec -T bot python -c '
import json
import os
import urllib.request

endpoint = os.environ["TELEGRAM_LOCAL_ENDPOINT"].rstrip("/")
token = os.environ["BOT_TOKEN"]
expected = os.environ["WEBHOOK_URL"].rstrip("/") + "/webhook"
request = urllib.request.Request(
    f"{endpoint}/bot{token}/getWebhookInfo", method="POST"
)
with urllib.request.urlopen(request, timeout=5) as response:
    payload = json.load(response)
actual = (payload.get("result") or {}).get("url")
if payload.get("ok") is not True or actual != expected:
    raise SystemExit(1)
' >/dev/null

current_manifest_tmp="$current_manifest.tmp.$$"
cp "$RELEASE_DIR/release.manifest" "$current_manifest_tmp"
"$MV_BIN" -f "$current_manifest_tmp" "$current_manifest"
ROLLBACK_ARMED=0
printf 'Activated immutable release %s for project %s.\n' \
  "$RELEASE_SHA" "$COMPOSE_PROJECT"
