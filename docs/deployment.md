# Deployment and rollback

This document describes the implemented release, maintenance, and rollback
procedures. Dated production checks are recorded separately in
[verification-2026-10-04.md](verification-2026-10-04.md); their scope does not
replace the complete [media acceptance procedure](acceptance.md).

## Release model

The routine release path is deliberately narrow:

1. Test the exact Git SHA.
2. Build the bot image once and address it by immutable registry digest.
   Activation runs automatically after the verification and build jobs succeed.
   The GitHub `production` environment retains its deployment secrets and
   variables without a required-reviewer approval gate.
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
| `MEDIA_PROXY_URLS` | Protected JSON SOCKS5 route array; never expose its credentials in diagnostics |
| `MEDIA_PROXY_PLATFORMS` | Platform allowlist for local extractor proxy fallback |
| `POT_PROVIDER_URL` | Project bgutil sidecar; plugin and server must have matching pinned versions |

Legacy `MAX_DL_MB` and `MAX_TG_UPLOAD_MB` are accepted only as an explicit,
non-conflicting migration to `MAX_MEDIA_FILE_MB`. They are not separate runtime
limits.

The protected GitHub deployment job requires `VPS_HOST`, `VPS_USERNAME`,
`VPS_SSH_KEY`, and the out-of-band verified `VPS_HOST_FINGERPRINT`. It reads the
public origin from `PUBLIC_BASE_URL`, falling back to the legacy `BASE_URL`
secret during migration. The project root is the repository variable
`VPS_PROJECT_PATH` when set and otherwise the fixed `/opt/ytdlbot`; it is never
derived from the uploaded release directory. Keep required reviewers disabled
on the `production` GitHub environment for automatic activation after a `vps`
push; retain the environment and its deployment credentials.

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

## Shared Loki logging

The bot opts into the existing host observability stack with the Compose label
`com.gemaibot.logs=true`; routine deployment does not recreate or reconfigure
Alloy, Loki, Grafana, or their Docker networks. Application and Uvicorn records
use the same JSON envelope: `schema_version`, RFC 3339 `timestamp`, `service`,
`environment`, lowercase `level`, and `message`, with bounded optional event,
request, and release fields. The production stream is expected to have
`service_name=ytdlbot`, `environment=production`, and `parse_status=valid` in
Loki.

Logs must not contain bot tokens, cookies, provider credentials, signed query
strings, raw media URLs, or chat/user identifiers. Validate ingestion with a
count query grouped by `service_name`, `environment`, `level`, and
`parse_status`; do not dump raw production messages merely to prove that the
stream exists.

Each successful materialization emits one correlated `media-measurement` event
for the winning candidate only. `bytes_downloaded` is the exact sum of the
materialized source streams; the nested `metrics` object records provider,
format IDs, dimensions, codecs, container, output bytes, and separate download
and transform durations. It contains no source URLs or signed query strings.
Handled private-message pipeline failures emit `media-pipeline-failed` and mark
the durable job failed instead of completed. The JSON formatter redacts Telegram
bot-token path segments, and `httpx`/`httpcore` request lines are suppressed at
INFO to keep Bot API URLs out of routine production logs.

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

## Media proxies and PO-token sidecar maintenance

`MEDIA_PROXY_URLS` is a JSON array of SOCKS5 URLs in primary/backup order, stored
only in the existing protected VPS `.env`. Do not print `docker compose config`
or container environment values when diagnosing credentials. YouTube uses the
pool first. Local yt-dlp/gallery-dl extractors for the configured known platforms
try direct first, then the pool after a transient/auth failure. Native API
providers retain their direct fast path. A stream source carries an opaque route
key; probes, redirects and downloads keep that route, with local DNS validation
and pinning. A transfer failure cools that platform/route for 60 seconds and the
single refresh obtains new URLs on the next available route. Proxy extraction
attempts are bounded to 20 seconds; direct fallback attempts to 3 seconds.
For yt-dlp, the network socket timeout is capped at 3 seconds and one extractor
retry, independently of the process budget. The provider resolve timeout covers
all configured proxy attempts, direct fallback and cleanup; the total race adds
4 seconds. With two proxies these limits are 44 and 48 seconds. This leaves time
for subprocess startup, YouTube's JS/PO-token work and a backup route after a
slow primary, while stalled sockets fail promptly. Route failure logs include
elapsed time, the applied budget and the error without proxy credentials.

Failed private/group requests, `/mp3`, `/mp4` and picker downloads expose a
`🔄 Повторить` button. The link cache stores the original media request and its
author/chat/status-message binding under a separate retry namespace, with the
normal link TTL. A retry preserves format, quality, audio language, clip and
output variant, edits the same status message, and permits one active attempt
per token. Success invalidates the token; another failure restores the button.
The durable callback retires its previous failed job before attempting delivery,
so a later worker restart does not replay a superseded attempt. SQLite retains
the retry chain, allowing recovery even if advancing the cached job ID fails.
Polling retries also claim their token in the SQLite delivery ledger before
sending. Only a known failed attempt releases that claim; a completed,
interrupted or uncertain attempt remains consumed even if Redis cleanup fails
and the process restarts.
Partial or uncertain Telegram deliveries do not expose a whole-request retry
that could duplicate already sent media.

Only delete cookie sets whose expiry has been demonstrated. The old global
`YTDLP_COOKIES_B64` contained invalid YouTube/Google cookies and was cleared on
October 4, 2026. Other platform cookies and Instagram sessions were preserved.

Routine releases replace only `bot`, preserving the tested rollback contract.
When changing `bgutil-ytdlp-pot-provider`, explicitly upgrade the project's
`bgutil-pot` service to the exact matching pinned server version from the tested
release Compose file. Under the same project deployment lock, verify the current
container's Compose project/service/root labels, capture its previous image for
rollback, pull the pinned image, and run `up -d --no-deps --no-build bgutil-pot`
with `--project-directory /opt/ytdlbot` and the release Compose file. Check `/ping`
from the bot's network and require the expected version before bot activation.
If the check fails, restore that service's previous image. Never use an
unqualified project-wide `up` or restart other applications on the shared host.

The October 4 pins are Python 3.12.15, Deno 2.9.7, yt-dlp 2026.8.19,
yt-dlp-ejs 0.8.0, and bgutil plugin/server 2.0.1. Treat `Dockerfile`,
`requirements.txt`, and `docker-compose.yml` as the source of truth for a later
release. Updating the bot image alone does not update the PO-token sidecar.

## YouTube download troubleshooting

YouTube's bot check may be surfaced as an access-denied extraction error even
for a public video. Distinguish source privacy from network blocking using the
bounded extraction result and the opaque route identifier. In the ordinary
pipeline, a YouTube access denial is transient and allows the next configured
proxy route; it does not permanently disable that provider as an auth failure.
The default pipeline has no independent external YouTube downloader.

Do not use a signed CDN URL extracted on one route for a direct or different
proxy download. The reported October 4 Short returned HTTP 403 in that mixed
route test. Probes, bounded HTTP ranges, downloads, and redirects must retain
their selected route. An expired URL may be refreshed once within the remaining
materialization deadline.

For wrong-language audio, inspect the redacted format ID, language, and
yt-dlp original-track markers rather than forcing Russian for every video.
The resolver recognizes `language_preference >= 10` or `original` in
`format_note`, excludes other audio candidates when an original is available,
and respects an explicitly requested language. Refreshed audible YouTube
candidates must retain a known selected language; ordinal format IDs alone are
not stable track identity. Muted animations have no audio-language constraint.

Default audible YouTube cache keys include `audio_track_policy` with value
`youtube-original-v1`. This bypasses older translated plans and Telegram file
IDs without clearing unrelated Redis data. Do not use a database-wide cache
flush to repair a single download.

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
