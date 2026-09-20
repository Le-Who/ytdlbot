#!/bin/sh
set -eu

: "${PROJECT_ROOT:=/opt/ytdlbot}"
: "${COMPOSE_PROJECT:?COMPOSE_PROJECT is required}"
: "${DEPLOY_STATE_DIR:=$PROJECT_ROOT/.deploy}"
: "${BOOTSTRAP_MODE:=check}"
: "${MV_BIN:=mv}"

fail() {
  printf '%s\n' "$*" >&2
  exit 2
}

manifest_field() {
  field=$1
  count=$(grep -c "^${field}=" "$bootstrap_manifest" || true)
  [ "$count" -eq 1 ] ||
    fail "Bootstrap evidence must contain exactly one $field."
  sed -n "s/^${field}=//p" "$bootstrap_manifest"
}

case "$BOOTSTRAP_MODE" in
  check|record) ;;
  *) fail "BOOTSTRAP_MODE must be check or record." ;;
esac
case "$COMPOSE_PROJECT" in
  ''|*[!A-Za-z0-9_.-]*) fail "COMPOSE_PROJECT is invalid." ;;
esac
[ "$DEPLOY_STATE_DIR" = "$PROJECT_ROOT/.deploy" ] ||
  fail "Bootstrap evidence must remain inside the verified project."
[ -d "$PROJECT_ROOT" ] && [ ! -L "$PROJECT_ROOT" ] ||
  fail "Bootstrap project root is unavailable or unsafe."

cd "$PROJECT_ROOT"
bot_container=$(docker compose -p "$COMPOSE_PROJECT" ps -q bot)
tg_api_container=$(docker compose -p "$COMPOSE_PROJECT" ps -q tg-api)
redis_container=$(docker compose -p "$COMPOSE_PROJECT" ps -q redis)
init_container=$(docker compose -p "$COMPOSE_PROJECT" ps -q -a volume-init)
[ -n "$bot_container" ] && [ -n "$tg_api_container" ] &&
  [ -n "$redis_container" ] && [ -n "$init_container" ] ||
  fail "Bootstrap topology is incomplete."

verify_identity() {
  container_id=$1
  service=$2
  identity=$(docker inspect --format \
    '{{ index .Config.Labels "com.docker.compose.project" }}|{{ index .Config.Labels "com.docker.compose.service" }}' \
    "$container_id")
  [ "$identity" = "$COMPOSE_PROJECT|$service" ] ||
    fail "Bootstrap container identity mismatch for $service."
}

verify_identity "$bot_container" bot
verify_identity "$tg_api_container" tg-api
verify_identity "$redis_container" redis
verify_identity "$init_container" volume-init

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
[ "$(mount_value "$bot_container" /srv/ytdlbot/media)" = "$media_volume|true" ] ||
  fail "Bootstrap bot media mount is missing or not writable."
[ "$(mount_value "$bot_container" /srv/ytdlbot/state)" = "$state_volume|true" ] ||
  fail "Bootstrap bot state mount is missing or not writable."
[ "$(mount_value "$tg_api_container" /srv/ytdlbot/media)" = "$media_volume|false" ] ||
  fail "Bootstrap Local Bot API media mount is not the shared read-only volume."
[ "$(mount_value "$tg_api_container" /var/lib/telegram-bot-api)" = "$tg_api_volume|true" ] ||
  fail "Bootstrap Telegram session volume does not match the verified project."
[ "$(mount_value "$redis_container" /data)" = "$redis_volume|true" ] ||
  fail "Bootstrap Redis volume does not match the verified project."

init_state=$(docker inspect --format '{{.State.Status}}|{{.State.ExitCode}}' "$init_container")
[ "$init_state" = "exited|0" ] ||
  fail "Bootstrap volume initializer has not completed successfully."
docker compose -p "$COMPOSE_PROJECT" exec -T bot python -c '
import os
from pathlib import Path

for raw in ("/srv/ytdlbot/media", "/srv/ytdlbot/state"):
    path = Path(raw)
    stat = path.stat()
    if (stat.st_uid, stat.st_gid) != (10001, 10001) or not os.access(path, os.W_OK):
        raise SystemExit(1)
' >/dev/null || fail "Bootstrap media/state ownership evidence failed."

bootstrap_manifest="$DEPLOY_STATE_DIR/bootstrap.manifest"
if [ "$BOOTSTRAP_MODE" = record ]; then
  mkdir -p "$DEPLOY_STATE_DIR"
  [ ! -L "$DEPLOY_STATE_DIR" ] ||
    fail "Bootstrap state directory must not be a symlink."
  umask 077
  bootstrap_tmp="$bootstrap_manifest.tmp.$$"
  {
    printf 'BOOTSTRAP_SCHEMA=1\n'
    printf 'COMPOSE_PROJECT=%s\n' "$COMPOSE_PROJECT"
    printf 'MEDIA_VOLUME=%s\n' "$media_volume"
    printf 'STATE_VOLUME=%s\n' "$state_volume"
    printf 'TG_API_VOLUME=%s\n' "$tg_api_volume"
    printf 'REDIS_VOLUME=%s\n' "$redis_volume"
    printf 'BOT_UID_GID=10001:10001\n'
  } >"$bootstrap_tmp"
  "$MV_BIN" -f "$bootstrap_tmp" "$bootstrap_manifest"
  printf 'Recorded bootstrap evidence for %s without changing services.\n' \
    "$COMPOSE_PROJECT"
  exit 0
fi

[ -f "$bootstrap_manifest" ] && [ ! -L "$bootstrap_manifest" ] ||
  fail "Bootstrap evidence is missing; run the separate bootstrap gate first."
[ "$(manifest_field BOOTSTRAP_SCHEMA)" = 1 ] || fail "Bootstrap schema mismatch."
[ "$(manifest_field COMPOSE_PROJECT)" = "$COMPOSE_PROJECT" ] ||
  fail "Bootstrap project evidence mismatch."
[ "$(manifest_field MEDIA_VOLUME)" = "$media_volume" ] ||
  fail "Bootstrap media volume evidence mismatch."
[ "$(manifest_field STATE_VOLUME)" = "$state_volume" ] ||
  fail "Bootstrap state volume evidence mismatch."
[ "$(manifest_field TG_API_VOLUME)" = "$tg_api_volume" ] ||
  fail "Bootstrap Telegram volume evidence mismatch."
[ "$(manifest_field REDIS_VOLUME)" = "$redis_volume" ] ||
  fail "Bootstrap Redis volume evidence mismatch."
[ "$(manifest_field BOT_UID_GID)" = 10001:10001 ] ||
  fail "Bootstrap ownership evidence mismatch."

printf 'Bootstrap topology gate passed for %s.\n' "$COMPOSE_PROJECT"
