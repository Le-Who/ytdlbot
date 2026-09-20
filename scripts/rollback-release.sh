#!/bin/sh
set -eu

: "${PROJECT_ROOT:=/opt/ytdlbot}"
: "${COMPOSE_PROJECT:?COMPOSE_PROJECT is required}"
: "${PYTHON_BIN:=python3}"
: "${CURL_BIN:=curl}"
: "${MV_BIN:=mv}"
: "${DEPLOY_HEALTH_ATTEMPTS:=45}"
: "${DEPLOY_HEALTH_INTERVAL_SECONDS:=2}"
: "${LOCAL_READY_URL:=http://127.0.0.1:8000/health/ready}"
: "${DEPLOY_STATE_DIR:=$PROJECT_ROOT/.deploy}"
: "${ROLLBACK_MANIFEST:=$DEPLOY_STATE_DIR/rollback.manifest}"

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

manifest_field() {
  field=$1
  count=$(grep -c "^${field}=" "$ROLLBACK_MANIFEST" || true)
  [ "$count" -eq 1 ] ||
    fail "Rollback manifest must contain exactly one $field."
  sed -n "s/^${field}=//p" "$ROLLBACK_MANIFEST"
}

case "$COMPOSE_PROJECT" in
  ''|*[!A-Za-z0-9_.-]*) fail "COMPOSE_PROJECT is invalid." ;;
esac
[ "$DEPLOY_STATE_DIR" = "$PROJECT_ROOT/.deploy" ] ||
  fail "DEPLOY_STATE_DIR must remain inside the verified project."
case "$DEPLOY_HEALTH_ATTEMPTS:$DEPLOY_HEALTH_INTERVAL_SECONDS" in
  *[!0-9:]*) fail "Rollback health settings must be non-negative integers." ;;
esac
[ "$DEPLOY_HEALTH_ATTEMPTS" -gt 0 ] ||
  fail "DEPLOY_HEALTH_ATTEMPTS must be positive."
[ -f "$ROLLBACK_MANIFEST" ] && [ ! -L "$ROLLBACK_MANIFEST" ] ||
  fail "Rollback manifest is unavailable or unsafe."

previous_release=$(manifest_field RELEASE_SHA)
previous_image=$(manifest_field BOT_IMAGE)
previous_project=$(manifest_field COMPOSE_PROJECT)
previous_compose=$(manifest_field COMPOSE_FILE)
previous_override=$(manifest_field OVERRIDE_FILE)
previous_health_contract=$(manifest_field HEALTH_CONTRACT)
previous_health_url=$(manifest_field HEALTH_URL)
[ "$previous_project" = "$COMPOSE_PROJECT" ] ||
  fail "Rollback project mismatch."
[ "$previous_compose" = "$DEPLOY_STATE_DIR/rollback-compose.yml" ] ||
  fail "Rollback Compose path is outside managed state."
[ "$previous_override" = "$DEPLOY_STATE_DIR/rollback-bot.override.yml" ] ||
  fail "Rollback override path is outside managed state."
[ -f "$previous_compose" ] && [ ! -L "$previous_compose" ] ||
  fail "Rollback Compose configuration is unavailable or unsafe."
[ -f "$previous_override" ] && [ ! -L "$previous_override" ] ||
  fail "Rollback bot image override is unavailable or unsafe."
case "$previous_health_contract:$previous_health_url" in
  "release-ready:$LOCAL_READY_URL"|"legacy-health:${LOCAL_READY_URL%/health/ready}/health") ;;
  *) fail "Rollback health contract is invalid." ;;
esac

if [ "${DEPLOY_LOCK_HELD:-0}" != 1 ]; then
  mkdir -p "$DEPLOY_STATE_DIR"
  umask 077
  exec 9>"$DEPLOY_STATE_DIR/deploy.lock"
  if ! flock -n 9; then
    printf '%s\n' "Another project release is already running." >&2
    exit 1
  fi
fi

rollback_tmp="$PROJECT_ROOT/.docker-compose.rollback.$$"
cp "$previous_compose" "$rollback_tmp"
"$MV_BIN" -f "$rollback_tmp" "$PROJECT_ROOT/docker-compose.yml"

cd "$PROJECT_ROOT"
export BOT_IMAGE="$previous_image"
export ROLLBACK_BOT_IMAGE="$previous_image"
export APP_RELEASE="$previous_release"
docker compose -p "$COMPOSE_PROJECT" \
  -f "$PROJECT_ROOT/docker-compose.yml" \
  -f "$previous_override" \
  up -d --no-deps --no-build bot

attempt=1
while [ "$attempt" -le "$DEPLOY_HEALTH_ATTEMPTS" ]; do
  body=$("$CURL_BIN" --fail --silent --show-error --max-time 5 \
    "$previous_health_url" || true)
  if printf '%s' "$body" | "$PYTHON_BIN" -c '
import json
import sys

expected = sys.argv[1]
contract = sys.argv[2]
try:
    payload = json.load(sys.stdin)
except (ValueError, TypeError):
    raise SystemExit(1)
if contract == "legacy-health":
    healthy = payload.get("ok") is True
else:
    healthy = payload.get("ready") is True and payload.get("release") == expected
raise SystemExit(0 if healthy else 1)
' "$previous_release" "$previous_health_contract"; then
    printf 'Rollback restored release %s.\n' "$previous_release"
    exit 0
  fi
  attempt=$((attempt + 1))
  sleep "$DEPLOY_HEALTH_INTERVAL_SECONDS"
done

printf 'Rollback release %s did not become ready.\n' "$previous_release" >&2
exit 1
