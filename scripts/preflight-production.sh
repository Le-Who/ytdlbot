#!/bin/sh
set -eu

: "${PROJECT_ROOT:=/opt/ytdlbot}"
: "${RELEASE_SHA:?RELEASE_SHA is required}"
: "${BOT_IMAGE:?BOT_IMAGE is required}"
: "${COMPOSE_PROJECT:?COMPOSE_PROJECT is required}"
: "${RELEASE_DIR:=$PROJECT_ROOT/releases/$RELEASE_SHA}"
: "${DEPLOY_MIN_FREE_BYTES:=1073741824}"
: "${DF_BIN:=df}"

unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES \
  COMPOSE_DISABLE_ENV_FILE

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

require_sha() {
  printf '%s\n' "$1" | grep -Eq '^[0-9a-f]{40}$' ||
    fail "$2 must be a full lowercase Git SHA."
}

require_digest_image() {
  printf '%s\n' "$1" |
    grep -Eq '^[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$' ||
    fail "BOT_IMAGE must be an immutable sha256 digest reference."
}

manifest_field() {
  field=$1
  count=$(grep -c "^${field}=" "$RELEASE_DIR/release.manifest" || true)
  [ "$count" -eq 1 ] ||
    fail "Release manifest must contain exactly one $field."
  sed -n "s/^${field}=//p" "$RELEASE_DIR/release.manifest"
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
case "$COMPOSE_PROJECT" in
  ''|*[!A-Za-z0-9_.-]*) fail "COMPOSE_PROJECT is invalid." ;;
esac
case "$DEPLOY_MIN_FREE_BYTES" in
  ''|*[!0-9]*) fail "DEPLOY_MIN_FREE_BYTES must be a non-negative integer." ;;
esac

require_sha "$RELEASE_SHA" RELEASE_SHA
require_digest_image "$BOT_IMAGE"

expected_release_dir="$PROJECT_ROOT/releases/$RELEASE_SHA"
[ "$RELEASE_DIR" = "$expected_release_dir" ] ||
  fail "RELEASE_DIR must be the exact project-scoped release path."
[ -d "$PROJECT_ROOT" ] || fail "PROJECT_ROOT does not exist."
[ ! -L "$PROJECT_ROOT" ] || fail "PROJECT_ROOT must not be a symlink."
[ -f "$PROJECT_ROOT/.env" ] && [ ! -L "$PROJECT_ROOT/.env" ] ||
  fail "Existing project .env secret file is required and must not be a symlink."
[ -f "$PROJECT_ROOT/docker-compose.yml" ] &&
  [ ! -L "$PROJECT_ROOT/docker-compose.yml" ] ||
  fail "Existing project Compose configuration is required."
[ -d "$RELEASE_DIR" ] && [ ! -L "$RELEASE_DIR" ] ||
  fail "Release directory is unavailable or unsafe."

for command_name in docker curl flock sha256sum find sort grep sed awk df; do
  command -v "$command_name" >/dev/null 2>&1 ||
    fail "$command_name is required for deployment preflight."
done

if find "$RELEASE_DIR" -type l -print | grep -q .; then
  fail "Release allowlist does not permit symlinks."
fi
actual_files=$(
  cd "$RELEASE_DIR"
  find . -type f -print | LC_ALL=C sort
)
expected_files=$(printf '%s\n' \
  './docker-compose.yml' \
  './release.manifest' \
  './scripts/bootstrap-production.sh' \
  './scripts/deploy-release.sh' \
  './scripts/preflight-production.sh' \
  './scripts/rollback-release.sh' |
  LC_ALL=C sort)
[ "$actual_files" = "$expected_files" ] ||
  fail "Release payload violates the file allowlist."

manifest_release=$(manifest_field RELEASE_SHA)
manifest_image=$(manifest_field BOT_IMAGE)
manifest_project=$(manifest_field COMPOSE_PROJECT)
manifest_compose_sha=$(manifest_field COMPOSE_SHA256)
[ "$manifest_release" = "$RELEASE_SHA" ] ||
  fail "Release manifest SHA mismatch."
[ "$manifest_image" = "$BOT_IMAGE" ] ||
  fail "Release manifest image mismatch."
[ "$manifest_project" = "$COMPOSE_PROJECT" ] ||
  fail "Release manifest project mismatch."
printf '%s\n' "$manifest_compose_sha" | grep -Eq '^[0-9a-f]{64}$' ||
  fail "Release manifest Compose checksum is invalid."
actual_compose_sha=$(sha256sum "$RELEASE_DIR/docker-compose.yml" | awk '{print $1}')
[ "$actual_compose_sha" = "$manifest_compose_sha" ] ||
  fail "Release manifest Compose checksum mismatch."

available_kb=$("$DF_BIN" -Pk "$PROJECT_ROOT" | awk 'NR == 2 {print $4}')
case "$available_kb" in
  ''|*[!0-9]*) fail "Could not determine deployment disk budget." ;;
esac
available_bytes=$((available_kb * 1024))
[ "$available_bytes" -ge "$DEPLOY_MIN_FREE_BYTES" ] ||
  fail "Insufficient disk space for release activation and rollback."

cd "$PROJECT_ROOT"
bot_container=$(BOT_IMAGE="$BOT_IMAGE" APP_RELEASE="$RELEASE_SHA" \
  docker compose -p "$COMPOSE_PROJECT" ps -q bot)
redis_container=$(BOT_IMAGE="$BOT_IMAGE" APP_RELEASE="$RELEASE_SHA" \
  docker compose -p "$COMPOSE_PROJECT" ps -q redis)
tg_api_container=$(BOT_IMAGE="$BOT_IMAGE" APP_RELEASE="$RELEASE_SHA" \
  docker compose -p "$COMPOSE_PROJECT" ps -q tg-api)
[ -n "$bot_container" ] && [ -n "$redis_container" ] &&
  [ -n "$tg_api_container" ] ||
  fail "The verified production bot, Redis, and Local Bot API topology must exist."

verify_container_identity() {
  container_id=$1
  service=$2
  identity=$(docker inspect --format \
    '{{ index .Config.Labels "com.docker.compose.project" }}|{{ index .Config.Labels "com.docker.compose.service" }}' \
    "$container_id")
  [ "$identity" = "$COMPOSE_PROJECT|$service" ] ||
    fail "Container identity does not match $COMPOSE_PROJECT/$service."
}

verify_container_identity "$bot_container" bot
verify_container_identity "$redis_container" redis
verify_container_identity "$tg_api_container" tg-api

project_working_dir=$(docker inspect --format \
  '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' \
  "$bot_container")
[ "$project_working_dir" = "$PROJECT_ROOT" ] ||
  fail "Verified Compose project root does not match PROJECT_ROOT."

redis_volume=$(docker inspect --format \
  '{{range .Mounts}}{{if eq .Destination "/data"}}{{println .Name}}{{end}}{{end}}' \
  "$redis_container" | sed -n '1p')
tg_api_volume=$(docker inspect --format \
  '{{range .Mounts}}{{if eq .Destination "/var/lib/telegram-bot-api"}}{{println .Name}}{{end}}{{end}}' \
  "$tg_api_container" | sed -n '1p')
[ -n "$redis_volume" ] && [ -n "$tg_api_volume" ] ||
  fail "Persistent Redis and Telegram session volumes must already exist."

BOOTSTRAP_MODE=check "$RELEASE_DIR/scripts/bootstrap-production.sh"

BOT_IMAGE="$BOT_IMAGE" APP_RELEASE="$RELEASE_SHA" \
  docker compose -p "$COMPOSE_PROJECT" -f "$RELEASE_DIR/docker-compose.yml" \
  config --quiet >/dev/null

printf 'Production preflight passed for %s (%s).\n' \
  "$RELEASE_SHA" "$COMPOSE_PROJECT"
