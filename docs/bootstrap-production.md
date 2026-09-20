# Production topology bootstrap gate

Routine releases never create, recreate, or update Redis, Telegram Bot API,
`volume-init`, networks, or named volumes. Before the first routine release, an
operator must explicitly authorize the separate Compose topology migration. The
release workflow uploads, but never automatically invokes, the migration script.

Run the transaction from the already uploaded immutable candidate directory:

```sh
export PROJECT_ROOT=/opt/ytdlbot
export COMPOSE_PROJECT=ytdlbot
export RELEASE_SHA=<40-character-tested-sha>
export RELEASE_DIR="$PROJECT_ROOT/releases/$RELEASE_SHA"
sh "$RELEASE_DIR/scripts/bootstrap-migrate-production.sh"
```

The migration holds `/opt/ytdlbot/.deploy/deploy.lock`, captures the exact
running bot, Local Bot API, and Redis image IDs plus the current Compose file,
and verifies the current bot has either the legacy `/health` contract or the
release-aware `/health/ready` contract. It creates only `<project>_media` and
`<project>_state`, installs the candidate Compose file atomically, runs
`volume-init`, and recreates only `tg-api` and `bot` using their captured images.
Redis, its data volume, the Telegram session volume, host services, and other
Compose projects are untouched.

After the old bot is healthy on the migrated topology, the script invokes the
non-mutating `bootstrap-production.sh` record gate. That gate verifies project
identity, exact project-scoped volumes, the shared read-only media mount on Local
Bot API, successful `volume-init`, and UID/GID 10001 ownership before atomically
writing `/opt/ytdlbot/.deploy/bootstrap.manifest`.

Failure or a handled termination after mutation restores the captured Compose
file and exact prior bot/Local API images, then rechecks the old health contract.
It deliberately does not delete newly created volumes or roll back durable data.
Inspect `/opt/ytdlbot/.deploy/bootstrap-rollback.manifest` and service logs before
retrying. SIGKILL cannot run a shell trap; in that case use the captured rollback
files and the documented project-scoped recovery procedure before any routine
release.

Every routine release repeats those checks in `BOOTSTRAP_MODE=check` and
refuses activation if either the live topology or recorded evidence differs.
Changing dependency services remains a separate, explicitly authorized
bootstrap operation; the routine release command stays bot-only with
`--no-deps --no-build`.
