# Deployment and rollback

This document describes the release machinery implemented in this repository. It
does not claim that a candidate image, provider, or migration has been verified
from the production VPS. That evidence belongs to the controlled Task 14 run.

## Release model

The routine release path is deliberately narrow:

1. Test the exact Git SHA.
2. Build the bot image once and address it by immutable registry digest.
3. Upload only the allowlisted release payload to a fresh, project-scoped staging
   directory.
4. Verify the payload, current production topology, disk budget, release SHA,
   image digest, and branch freshness.
5. Drain the single bot worker for a bounded interval.
6. Replace only the `bot` service with `docker compose -p <project> up -d
   --no-deps --no-build bot`.
7. Require local and public readiness, the exact `APP_RELEASE`, a functional
   Local Bot API probe, and the expected webhook URL.
8. Commit the release manifest or restore the captured bot image and Compose
   configuration.

Routine deployment does not run `compose down`, pull or recreate dependency
services, remove orphans, restart Docker or the host, edit Caddy or Portainer,
globally prune images, or regenerate `.env`.

## Required production topology

Production is a single active bot worker backed by the existing Compose project.
The saved project name is part of the release identity; changing a directory name
must not create a second set of volumes.

| Resource | Bot mount | Dependency mount | Purpose |
|---|---|---|---|
| `<project>_media` | `/srv/ytdlbot/media` read/write | Local Bot API: the same path read-only | Leased media files and Local Bot API path delivery |
| `<project>_state` | `/srv/ytdlbot/state` read/write | — | SQLite WAL inbox, jobs, receipts, release-safe state |
| `<project>_tg-api-data` | — | `/var/lib/telegram-bot-api` read/write | Existing Telegram Local API session |
| `<project>_redis-data` | — | `/data` read/write | Existing Redis cache data |

The bot runs as fixed UID/GID `10001:10001`. The `volume-init` service is a
root-only one-shot initializer; routine deploys must not rerun it or recreate the
Local Bot API, Redis, or provider services.

### Bootstrap-only volume migration

Adding the media/state mounts and attaching the shared media path to Local Bot API
is a separately authorized, one-time migration. It may briefly recreate the
affected ytdlbot dependency container, but must preserve the existing Telegram
session and Redis volume. It must not touch neighboring Compose projects or host
services.

Use the candidate release's
[`scripts/bootstrap-migrate-production.sh`](../scripts/bootstrap-migrate-production.sh)
for this one-time transaction. It takes the same project lock as a release,
captures the current Compose file and exact running bot/Local API/Redis image
IDs, creates only the project-scoped media and state volumes, runs the candidate
`volume-init`, and recreates only `tg-api` and `bot` on the captured images. It
does not restart Redis or any neighboring project. It verifies the previous
bot's bounded health contract before and after migration, then atomically records
the topology through `bootstrap-production.sh`.

The rollback trap is armed before the active Compose file changes. A failed
initializer, service activation, health probe, or handled termination restores
the captured Compose file and exact prior bot/Local API images; the project
volumes are retained for inspection rather than deleted. The operator
must then investigate before retrying. Exact invocation and evidence are
documented in [bootstrap-production.md](bootstrap-production.md). Every routine
preflight reruns `bootstrap-production.sh` in `BOOTSTRAP_MODE=check`; a missing
or changed mount, owner, project label, or initializer result blocks activation.

## Required configuration

Secrets remain in the existing nonsymlink project `.env` on the VPS. The release
payload never contains or rewrites that file. Important runtime values are:

| Variable | Production contract |
|---|---|
| `BOT_TOKEN` | Existing Telegram secret; never print it |
| `WEBHOOK_URL` / `TELEGRAM_SECRET_TOKEN` | Existing webhook URL and secret |
| `APP_RELEASE` | Exact tested 40-character Git SHA |
| `BOT_IMAGE` | Immutable `registry/image@sha256:<digest>` reference |
| `TELEGRAM_LOCAL_ENDPOINT` | Internal Local Bot API endpoint |
| `TELEGRAM_LOCAL_REQUIRED` | `1` in production |
| `MAX_MEDIA_FILE_MB` | Operator limit, at most `2000`; MB is exactly 1,000,000 bytes |
| `MEDIA_DIR` | `/srv/ytdlbot/media` |
| `YTDLBOT_JOB_DB` | `/srv/ytdlbot/state/jobs.sqlite3` |

Legacy `MAX_DL_MB` and `MAX_TG_UPLOAD_MB` are accepted only as an explicit,
non-conflicting migration to `MAX_MEDIA_FILE_MB`. They are not separate runtime
limits.

## Readiness and Local Bot API degradation

- `/health/live` proves that the process serves HTTP.
- `/health/ready` reports `APP_RELEASE`, the active delivery profile and byte
  limit, exact durable schema/write capability, worker ownership, and a bounded,
  authenticated Local Bot API `getMe` probe.
- The probe coalesces concurrent callers and expires cached results; its token is
  neither returned nor logged.
- Failure of the required Local Bot API or durable store makes readiness fail and
  prevents release commit. An optional external provider outage does not make the
  entire bot unready.
- Production does not silently downgrade an already selected large item to cloud
  Bot API rules. It reports the required delivery profile as unavailable. Cloud
  mode is an explicit degraded profile for compatible files only.

The 2,000 MB protocol ceiling is not a promise that disk, time, memory, or the
source provider can handle every 2,000 MB item. The pipeline still reserves disk,
streams with byte caps, and applies materialization and stall deadlines.

## Durable inbox, drain, and restart behavior

The webhook returns success only after `update_id` and the minimal retained update
payload are transactionally inserted into the SQLite WAL inbox. A write failure
returns a retryable response. One worker owns the queue; Redis remains a cache and
is not the authoritative inbox.

During shutdown the drain controller stops starting heavy work while continuing
to persist incoming updates. Active work receives a bounded 30-second drain
budget; Compose allows 45 seconds before forced termination. At the deadline the
worker checkpoints or cancels supervised work and the next worker recovers it.
Normal release shutdown does not delete the webhook.

Delivery receipts are durable per logical item:

- `success`: do not send the item again;
- `failed`: eligible for the bounded restart recovery policy;
- `uncertain`: Telegram may have accepted the send, so automatic replay is
  forbidden to avoid duplicates.

Retained webhook payloads are bounded and periodically purged. Temporary media is
resumed only when its lease, integrity, and source validity still pass checks.

## Automated release

The protected deployment workflow is the canonical path. It pins first-party
actions, installs pinned dependencies, tests the same SHA, builds and smokes one
image, resolves its digest, rejects stale branch runs, uploads an allowlisted
payload, and invokes `scripts/deploy-release.sh` over verified SSH. Deployment
concurrency and the project-scoped host `flock` reject overlapping activations.

The uploaded release directory contains exactly:

```text
docker-compose.yml
release.manifest
scripts/bootstrap-migrate-production.sh
scripts/bootstrap-production.sh
scripts/deploy-release.sh
scripts/preflight-production.sh
scripts/rollback-release.sh
```

If the same SHA directory already exists after an SSH interruption, it is reused
only when ownership, canonical path, symlink policy, allowlist, manifest fields,
and every payload checksum match the newly uploaded staging payload. It is never
overwritten with different bytes.

## Manual activation with the same scripts

Use this only for an already tested, built, and uploaded release. Obtain the exact
tested branch SHA and immutable image digest from CI; do not substitute an
arbitrary working-tree `HEAD` or a mutable tag.

```sh
export PROJECT_ROOT=/opt/ytdlbot
export COMPOSE_PROJECT=ytdlbot
export RELEASE_SHA=<40-character-tested-sha>
export EXPECTED_BRANCH_SHA=<current-protected-branch-sha>
export BOT_IMAGE=ghcr.io/<owner>/<repo>@sha256:<64-hex-digest>
export PUBLIC_BASE_URL=https://<public-bot-origin>
export RELEASE_DIR="$PROJECT_ROOT/releases/$RELEASE_SHA"

sh "$RELEASE_DIR/scripts/preflight-production.sh"
sh "$RELEASE_DIR/scripts/deploy-release.sh"
```

`deploy-release.sh` reruns preflight while holding the project lock, so invoking
both commands is intentional: the first is an operator-visible check and the
second owns the atomic release transaction.

## Manual rollback with the same scripts

The failed/candidate release directory contains the rollback script used by the
automatic trap. It reads only the project-scoped `.deploy/rollback.manifest` and
the captured Compose/image/current-manifest files.

```sh
export PROJECT_ROOT=/opt/ytdlbot
export COMPOSE_PROJECT=ytdlbot
export FAILED_RELEASE_SHA=<sha-whose-release-script-captured-rollback-state>

sh "$PROJECT_ROOT/releases/$FAILED_RELEASE_SHA/scripts/rollback-release.sh"
```

Rollback restores the exact captured bot image through a bot-only Compose
override, the prior Compose configuration, and the authoritative previous
`current.manifest` (or its explicitly recorded legacy absence). It then waits for
the recorded readiness contract. A failed rollback exits nonzero and requires
operator investigation.

Rollback does **not** rewind SQLite, Redis, Telegram sessions, media volumes, or
database schemas. Store migrations must remain backward-compatible with the
previous image. Never restore an older database snapshot as part of image
rollback.

## Availability expectation

There is one bot consumer. Drain plus durable recovery limits disruption and
prevents acknowledged jobs from being silently lost, but this release does not
promise zero downtime. Expect a bounded single-instance pause while the old bot
drains and the new bot becomes ready.

## Troubleshooting boundaries

- Read `.deploy/current.manifest` and `.deploy/rollback.manifest` without exposing
  secret environment values.
- Check `/health/ready` locally and through the public route; require the expected
  release SHA.
- Check `getWebhookInfo` from inside the bot container without printing the token.
- A provider-specific 403/429 is not proof that the bot or Local Bot API is down.
- Do not “repair” a failed release with `compose down`, volume deletion, `.env`
  regeneration, a global prune, or host-service restarts.
- Production provider and performance claims require the redacted acceptance
  evidence described in [acceptance.md](acceptance.md).
