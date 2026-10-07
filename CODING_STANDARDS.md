# Coding standards

Read the sections that match the affected behavior. The code and configuration
links below locate the boundaries to inspect; detailed contracts and procedures
remain in the linked project documents.

## Shared media contracts

For provider, pipeline, cache, or delivery changes, start with
[media models](app/services/media/models.py),
[candidate validation](app/services/media/validation.py), and the affected
consumers. Extend these shared contracts rather than introducing a second
request or candidate representation in a bot handler or compatibility adapter.
Read [media acceptance](docs/acceptance.md) before changing their behavior.

- Carry the requested quality, audio format/language, clip, media kind,
  watermark policy, album selection/order, caller scope, and authorization scope
  through resolution, cache identity, transformation, and delivery.
- Keep authorized/session media isolated from public cache entries. Check
  [media cache](app/core/media_cache.py) when changing request identity or
  Telegram `file_id` reuse; a usable `file_id` bypasses provider work.
- Validate equivalence before selecting a provider winner. Preserve the bounded
  work in [provider racing](app/services/media/race.py) and the configured
  endpoint eligibility in [provider registry](app/services/media/registry.py).
  A faster response alone does not establish an acceptable candidate.
- When changing callback encoding, keep the existing decoder usable for the
  link-cache TTL specified in media acceptance.

## Resource ownership and failure handling

For download, conversion, concurrency, or cancellation changes, inspect
[media transport](app/services/media/transport.py),
[process supervision](app/core/process.py),
[resource budgets](app/core/resource_budget.py), and the affected caller.

- Keep media subprocesses in the existing request ownership/supervision path.
  Cancellation must await the owned work and release process trees, response
  streams, queue slots, leases, and partial files.
- Preserve bounded retries, timeouts, byte caps, and disk reservations. Keep
  redirect/DNS checks and source route identity across probes and transfers.
- Use the existing failure classifications and receipt boundaries. Surface a
  failed provider or transfer through its error/result path instead of treating
  it as an empty successful result.
- For performance changes, compare the same request semantics and environment
  before and after. Separate measured download, transform, and delivery costs;
  preserve the contract checks while measuring the affected path.

## Durable jobs and Telegram delivery

For webhook, worker, drain, recovery, or send changes, inspect
[job storage](app/core/job_store.py), [drain control](app/core/drain.py), and
[delivery](app/services/media/delivery.py). Read the durable inbox and rollback
sections of [deployment](docs/deployment.md).

- Acknowledge webhook admission only after durable transactional insertion, and
  preserve `update_id` deduplication and single-worker ownership.
- Keep successful receipts out of recovery sends. An uncertain receipt means
  Telegram may already have accepted the item; automatic replay can duplicate
  delivery. Only known failed work follows the bounded recovery policy.
- Preserve incoming update persistence during drain and checkpoint owned work
  for recovery. Normal release shutdown keeps the webhook configured.
- Keep store changes compatible with the previous bot image: image rollback
  preserves SQLite, Redis, Telegram sessions, and media volumes.

## Configuration, logs, and deployment files

For configuration or infrastructure changes, inspect
[configuration](app/core/config.py), [.env.example](.env.example), the affected
Compose or workflow file, and [deployment](docs/deployment.md).

- Keep production secrets in the protected runtime environment. Redact tokens,
  cookies, provider credentials, raw/signed URLs, and chat/user identifiers from
  logs and evidence; use the existing [logging boundary](app/core/logging.py).
- Preserve the explicit Local Bot API and degraded cloud delivery profiles and
  their decimal-byte limits. Keep required store/Local API failures visible in
  readiness rather than silently reducing an already selected request.
- Maintain the bot-only routine release transaction and project identity.
  Bootstrap migration and sidecar maintenance have their own documented scopes.
  Follow the allowlisted release payload and rollback procedure.
- Keep executable deployment scripts suitable for their Linux runtime, including
  LF line endings when exporting a Windows checkout.

## Verification

Use the checks in [README testing](README.md#testing),
[pytest/Ruff/mypy configuration](pyproject.toml), and
[pre-commit hooks](.pre-commit-config.yaml). Read the affected callers and use
behavioral regressions for request equivalence, cancellation, recovery, or
failure handling when changing those contracts.

Run tests in an isolated environment: [configuration](app/core/config.py) loads
`.env`, while [shared fixtures](tests/conftest.py) supply safe defaults only for
absent variables. Webhook fixtures use `TELEGRAM_SECRET_TOKEN=test-secret`.

Focused contract commands and the offline release gate are in
[media acceptance](docs/acceptance.md); narrow test runs use `--no-cov` there,
while the full suite retains its configured coverage gate. Shell and Compose
changes require their documented checks. Use `docker compose config --quiet`
for syntax validation so resolved secrets stay out of output, and report an
unavailable Docker runtime as an unrun check.

Keep live provider/production evidence opt-in under the acceptance procedure.
Use offline fixtures and fakes for deterministic contracts; a passing local
suite cannot establish current production provider reachability.
