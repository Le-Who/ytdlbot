# Production topology bootstrap gate

Routine releases never create, recreate, or update Redis, Telegram Bot API,
`volume-init`, networks, or named volumes. Before the first routine release,
an operator must complete the separately approved Compose topology migration
and preserve the existing project name, Redis volume, and Telegram session
volume.

After that migration is healthy, record the one-time bootstrap evidence from
the candidate release directory:

```sh
PROJECT_ROOT=/opt/ytdlbot \
COMPOSE_PROJECT=ytdlbot \
BOT_IMAGE=ghcr.io/<owner>/<repo>@sha256:<candidate-digest> \
APP_RELEASE=<tested-sha> \
BOOTSTRAP_MODE=record \
sh /opt/ytdlbot/releases/<tested-sha>/scripts/bootstrap-production.sh
```

The gate is non-mutating with respect to services. It verifies the live
Compose project identity, exact project-scoped Redis/session/media/state
volumes, the shared read-only media mount on Local Bot API, a successful
`volume-init`, and UID/GID 10001 ownership before atomically recording
`/opt/ytdlbot/.deploy/bootstrap.manifest`.

Every routine release repeats those checks in `BOOTSTRAP_MODE=check` and
refuses activation if either the live topology or recorded evidence differs.
Changing dependency services remains a separate, explicitly authorized
bootstrap operation; the routine release command stays bot-only with
`--no-deps --no-build`.
