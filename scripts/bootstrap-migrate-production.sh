#!/bin/sh
set -eu

: "${PROJECT_ROOT:=/opt/ytdlbot}"
: "${RELEASE_SHA:?RELEASE_SHA is required}"
: "${COMPOSE_PROJECT:?COMPOSE_PROJECT is required}"
: "${RELEASE_DIR:=$PROJECT_ROOT/releases/$RELEASE_SHA}"
: "${DEPLOY_STATE_DIR:=$PROJECT_ROOT/.deploy}"
: "${PYTHON_BIN:=python3}"
: "${CURL_BIN:=curl}"
: "${MV_BIN:=mv}"
: "${BOOTSTRAP_HEALTH_ATTEMPTS:=30}"
: "${BOOTSTRAP_HEALTH_INTERVAL_SECONDS:=2}"
: "${LOCAL_READY_URL:=http://127.0.0.1:8000/health/ready}"

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

case "$PROJECT_ROOT" in
  /*) ;;
  *) fail "PROJECT_ROOT must be absolute." ;;
esac
case "$PROJECT_ROOT" in
  /|//*|*/|*//*|*/./*|*/.|*/../*|*/..|*[!A-Za-z0-9._/-]*)
    fail "PROJECT_ROOT must be a normalized shell-safe project path."
    ;;
esac
printf '%s\n' "$RELEASE_SHA" | grep -Eq '^[0-9a-f]{40}$' ||
  fail "RELEASE_SHA must be a full lowercase Git SHA."
case "$COMPOSE_PROJECT" in
  ''|*[!A-Za-z0-9_.-]*) fail "COMPOSE_PROJECT is invalid." ;;
esac
[ "$RELEASE_DIR" = "$PROJECT_ROOT/releases/$RELEASE_SHA" ] ||
  fail "RELEASE_DIR must be the exact project-scoped release path."
[ "$DEPLOY_STATE_DIR" = "$PROJECT_ROOT/.deploy" ] ||
  fail "DEPLOY_STATE_DIR must remain inside the verified project."
case "$BOOTSTRAP_HEALTH_ATTEMPTS:$BOOTSTRAP_HEALTH_INTERVAL_SECONDS" in
  *[!0-9:]*) fail "Bootstrap health settings must be non-negative integers." ;;
esac
[ "$BOOTSTRAP_HEALTH_ATTEMPTS" -gt 0 ] ||
  fail "BOOTSTRAP_HEALTH_ATTEMPTS must be positive."
[ -d "$PROJECT_ROOT" ] && [ ! -L "$PROJECT_ROOT" ] ||
  fail "Bootstrap project root is unavailable or unsafe."
[ -f "$PROJECT_ROOT/docker-compose.yml" ] &&
  [ ! -L "$PROJECT_ROOT/docker-compose.yml" ] ||
  fail "Current Compose configuration is unavailable or unsafe."
[ -f "$RELEASE_DIR/docker-compose.yml" ] &&
  [ ! -L "$RELEASE_DIR/docker-compose.yml" ] ||
  fail "Candidate Compose configuration is unavailable or unsafe."
[ -x "$RELEASE_DIR/scripts/bootstrap-production.sh" ] ||
  fail "Candidate bootstrap record gate is unavailable."

mkdir -p "$DEPLOY_STATE_DIR"
[ ! -L "$DEPLOY_STATE_DIR" ] ||
  fail "Bootstrap state directory must not be a symlink."
bootstrap_manifest="$DEPLOY_STATE_DIR/bootstrap.manifest"
[ ! -e "$bootstrap_manifest" ] && [ ! -L "$bootstrap_manifest" ] ||
  fail "Bootstrap evidence already exists; routine release must use check mode."
umask 077
exec 9>"$DEPLOY_STATE_DIR/deploy.lock"
if ! flock -n 9; then
  printf '%s\n' "Another project release is already running." >&2
  exit 1
fi

cd "$PROJECT_ROOT"
bot_container=$(docker compose -p "$COMPOSE_PROJECT" ps -q bot)
tg_api_container=$(docker compose -p "$COMPOSE_PROJECT" ps -q tg-api)
redis_container=$(docker compose -p "$COMPOSE_PROJECT" ps -q redis)
[ -n "$bot_container" ] && [ -n "$tg_api_container" ] &&
  [ -n "$redis_container" ] || fail "Current Compose topology is incomplete."

verify_identity() {
  container_id=$1
  service=$2
  identity=$(docker inspect --format \
    '{{ index .Config.Labels "com.docker.compose.project" }}|{{ index .Config.Labels "com.docker.compose.service" }}' \
    "$container_id")
  [ "$identity" = "$COMPOSE_PROJECT|$service" ] ||
    fail "Current container identity mismatch for $service."
}

verify_identity "$bot_container" bot
verify_identity "$tg_api_container" tg-api
verify_identity "$redis_container" redis

project_working_dir=$(docker inspect --format \
  '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' \
  "$bot_container")
[ "$project_working_dir" = "$PROJECT_ROOT" ] ||
  fail "Verified Compose project root does not match PROJECT_ROOT."

mount_value() {
  container_id=$1
  destination=$2
  docker inspect --format \
    "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{println .Name \"|\" .RW}}{{end}}{{end}}" \
    "$container_id" | sed -n '1{s/ | /|/;p;}'
}

media_volume="${COMPOSE_PROJECT}_media"
state_volume="${COMPOSE_PROJECT}_state"
tg_api_volume="${COMPOSE_PROJECT}_tg-api-data"
redis_volume="${COMPOSE_PROJECT}_redis-data"
[ "$(mount_value "$tg_api_container" /var/lib/telegram-bot-api)" = "$tg_api_volume|true" ] ||
  fail "Current Telegram session volume does not match the project."
[ "$(mount_value "$redis_container" /data)" = "$redis_volume|true" ] ||
  fail "Current Redis volume does not match the project."

previous_bot_image=$(docker inspect --format '{{.Image}}' "$bot_container")
previous_tg_api_image=$(docker inspect --format '{{.Image}}' "$tg_api_container")
previous_redis_image=$(docker inspect --format '{{.Image}}' "$redis_container")
previous_release=$(docker inspect --format \
  '{{range .Config.Env}}{{println .}}{{end}}' "$bot_container" |
  sed -n 's/^APP_RELEASE=//p' | sed -n '1p')
[ -n "$previous_bot_image" ] && [ -n "$previous_tg_api_image" ] &&
  [ -n "$previous_redis_image" ] || fail "Current service images cannot be captured."
case "$previous_release" in
  ''|*[!A-Za-z0-9._-]*) previous_release=legacy ;;
esac

health_contract=''
health_url=''
ready_body=$("$CURL_BIN" --fail --silent --show-error --max-time 5 \
  "$LOCAL_READY_URL" || true)
if printf '%s' "$ready_body" | "$PYTHON_BIN" -c '
import json
import sys

expected = sys.argv[1]
try:
    payload = json.load(sys.stdin)
except (TypeError, ValueError):
    raise SystemExit(1)
raise SystemExit(
    0 if payload.get("ready") is True and payload.get("release") == expected else 1
)
' "$previous_release"; then
  health_contract=release-ready
  health_url=$LOCAL_READY_URL
else
  legacy_url=${LOCAL_READY_URL%/health/ready}/health
  legacy_body=$("$CURL_BIN" --fail --silent --show-error --max-time 5 \
    "$legacy_url" || true)
  if printf '%s' "$legacy_body" | "$PYTHON_BIN" -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except (TypeError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if payload.get("ok") is True else 1)
'; then
    health_contract=legacy-health
    health_url=$legacy_url
  else
    fail "Current bot has no bounded health contract for bootstrap rollback."
  fi
fi

candidate_tmp="$PROJECT_ROOT/.docker-compose.bootstrap-candidate.$$"
cp "$RELEASE_DIR/docker-compose.yml" "$candidate_tmp"
BOOTSTRAP_BOT_IMAGE="$previous_bot_image" \
BOOTSTRAP_TG_API_IMAGE="$previous_tg_api_image" \
BOT_IMAGE="$previous_bot_image" APP_RELEASE="$previous_release" \
  docker compose -p "$COMPOSE_PROJECT" -f "$candidate_tmp" config --quiet ||
  fail "Candidate Compose configuration is invalid."

rollback_compose="$DEPLOY_STATE_DIR/bootstrap-rollback-compose.yml"
image_override="$DEPLOY_STATE_DIR/bootstrap-images.override.yml"
rollback_tmp="$rollback_compose.tmp.$$"
override_tmp="$image_override.tmp.$$"
cp "$PROJECT_ROOT/docker-compose.yml" "$rollback_tmp"
"$MV_BIN" -f "$rollback_tmp" "$rollback_compose"
{
  printf 'services:\n'
  printf '  bot:\n'
  printf '%s\n' '    image: "${BOOTSTRAP_BOT_IMAGE:?captured bot image required}"'
  printf '  tg-api:\n'
  printf '%s\n' '    image: "${BOOTSTRAP_TG_API_IMAGE:?captured Local API image required}"'
  printf '  volume-init:\n'
  printf '%s\n' '    image: "${BOOTSTRAP_BOT_IMAGE:?captured bot image required}"'
} >"$override_tmp"
"$MV_BIN" -f "$override_tmp" "$image_override"
bootstrap_rollback_manifest="$DEPLOY_STATE_DIR/bootstrap-rollback.manifest"
manifest_tmp="$bootstrap_rollback_manifest.tmp.$$"
{
  printf 'COMPOSE_PROJECT=%s\n' "$COMPOSE_PROJECT"
  printf 'BOT_IMAGE=%s\n' "$previous_bot_image"
  printf 'TG_API_IMAGE=%s\n' "$previous_tg_api_image"
  printf 'REDIS_IMAGE=%s\n' "$previous_redis_image"
  printf 'MEDIA_VOLUME=%s\n' "$media_volume"
  printf 'STATE_VOLUME=%s\n' "$state_volume"
  printf 'TG_API_VOLUME=%s\n' "$tg_api_volume"
  printf 'REDIS_VOLUME=%s\n' "$redis_volume"
  printf 'HEALTH_CONTRACT=%s\n' "$health_contract"
  printf 'HEALTH_URL=%s\n' "$health_url"
} >"$manifest_tmp"
"$MV_BIN" -f "$manifest_tmp" "$bootstrap_rollback_manifest"

check_old_bot_health() {
  attempt=1
  while [ "$attempt" -le "$BOOTSTRAP_HEALTH_ATTEMPTS" ]; do
    body=$("$CURL_BIN" --fail --silent --show-error --max-time 5 \
      "$health_url" || true)
    if printf '%s' "$body" | "$PYTHON_BIN" -c '
import json
import sys

expected = sys.argv[1]
contract = sys.argv[2]
try:
    payload = json.load(sys.stdin)
except (TypeError, ValueError):
    raise SystemExit(1)
healthy = payload.get("ok") is True if contract == "legacy-health" else (
    payload.get("ready") is True and payload.get("release") == expected
)
raise SystemExit(0 if healthy else 1)
' "$previous_release" "$health_contract"; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep "$BOOTSTRAP_HEALTH_INTERVAL_SECONDS"
  done
  return 1
}

ROLLBACK_ARMED=1
rollback_bootstrap() {
  printf '%s\n' "Bootstrap migration failed; restoring prior config and services." >&2
  restore_tmp="$PROJECT_ROOT/.docker-compose.bootstrap-rollback.$$"
  cp "$rollback_compose" "$restore_tmp"
  "$MV_BIN" -f "$restore_tmp" "$PROJECT_ROOT/docker-compose.yml"
  rm -f "$bootstrap_manifest"
  export BOOTSTRAP_BOT_IMAGE="$previous_bot_image"
  export BOOTSTRAP_TG_API_IMAGE="$previous_tg_api_image"
  export BOT_IMAGE="$previous_bot_image"
  export APP_RELEASE="$previous_release"
  export BOOTSTRAP_ROLLBACK=1
  docker compose -p "$COMPOSE_PROJECT" \
    -f "$PROJECT_ROOT/docker-compose.yml" -f "$image_override" \
    up -d --no-deps --no-build tg-api bot && check_old_bot_health
}

on_exit() {
  status=$?
  trap - EXIT HUP INT TERM
  rm -f "$candidate_tmp"
  if [ "$status" -ne 0 ] && [ "$ROLLBACK_ARMED" -eq 1 ]; then
    if ! rollback_bootstrap; then
      printf '%s\n' "Bootstrap rollback failed; inspect project-scoped state." >&2
    fi
  fi
  exit "$status"
}
trap on_exit EXIT
trap 'exit 130' HUP INT TERM

docker volume create "$media_volume" >/dev/null
docker volume create "$state_volume" >/dev/null
"$MV_BIN" -f "$candidate_tmp" "$PROJECT_ROOT/docker-compose.yml"
candidate_tmp=''

export BOOTSTRAP_BOT_IMAGE="$previous_bot_image"
export BOOTSTRAP_TG_API_IMAGE="$previous_tg_api_image"
export BOT_IMAGE="$previous_bot_image"
export APP_RELEASE="$previous_release"
docker compose -p "$COMPOSE_PROJECT" \
  -f "$PROJECT_ROOT/docker-compose.yml" -f "$image_override" \
  up --no-deps --no-build --abort-on-container-exit \
  --exit-code-from volume-init volume-init
docker compose -p "$COMPOSE_PROJECT" \
  -f "$PROJECT_ROOT/docker-compose.yml" -f "$image_override" \
  up -d --no-deps --no-build tg-api bot

check_old_bot_health || fail "Old bot image did not recover after topology migration."
BOOTSTRAP_MODE=record "$RELEASE_DIR/scripts/bootstrap-production.sh"

ROLLBACK_ARMED=0
trap - EXIT HUP INT TERM
printf 'Bootstrap migration completed and recorded for %s.\n' "$COMPOSE_PROJECT"
