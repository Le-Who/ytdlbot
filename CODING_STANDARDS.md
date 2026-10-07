# Coding standards

Read the branches selected by [AGENTS.md](AGENTS.md). Linked contracts and
procedures remain authoritative; code links identify their implementation.

## Changes and verification

Trace every changed interface to its consumers, including settings and dependency
consumers. Verify the behavior those callers need, not only the modified unit.
Update the existing setup, contract, or operations document when behavior changes.

Select checks from [README testing](README.md#testing),
[pytest/Ruff/mypy configuration](pyproject.toml), and
[pre-commit hooks](.pre-commit-config.yaml). Exercise changed failure and
cancellation paths with behavioral regressions. Report changed behavior, checks
performed, and every relevant unrun check with its concrete limitation.

Isolate test configuration: [configuration](app/core/config.py) loads `.env`,
while [shared fixtures](tests/conftest.py) set defaults only for absent variables.
Webhook fixtures expect `TELEGRAM_SECRET_TOKEN=test-secret`.

## Media contracts

Read [offline acceptance](docs/acceptance.md#offline-acceptance) before changing
media behavior, including cancellation and callback compatibility. Extend
[media models](app/services/media/models.py) through the shared pipeline;
providers resolve candidates while the pipeline owns preferences and sends.

- **Request or cache identity:** Trace every changed field through
  [media cache](app/core/media_cache.py), transformation, and delivery consumers
  against the acceptance contracts.
- **Provider routing or racing:** Use [candidate validation](app/services/media/validation.py),
  [provider registry](app/services/media/registry.py), and
  [provider racing](app/services/media/race.py) to preserve equivalence,
  configured eligibility, bounded work, and classified failures.
- **Downloads, conversion, or cancellation:** Reuse [transport](app/services/media/transport.py),
  [process supervision](app/core/process.py), and
  [resource budgets](app/core/resource_budget.py). Await owned work and release
  every owned resource on cancellation or failure; retain route identity and
  transport safety checks across probes, redirects, and transfers.

## Durable delivery

Read [admission, receipts, drain, and recovery](docs/deployment.md#durable-inbox-drain-and-restart-behavior)
before changing webhooks, workers, or sends. Inspect [job storage](app/core/job_store.py),
[drain control](app/core/drain.py), and [delivery](app/services/media/delivery.py)
as one boundary. Verify changed transitions against recovery consumers and
[rollback compatibility](docs/deployment.md#manual-rollback-with-the-same-scripts).

## Runtime and release boundaries

- **Settings or dependencies:** Inspect [configuration](app/core/config.py),
  [.env.example](.env.example), the affected requirement pins, and their Docker
  or workflow consumers. For production settings or readiness, read
  [required configuration](docs/deployment.md#required-configuration) and
  [delivery profiles](docs/deployment.md#readiness-and-local-bot-api-degradation).
- **Logs or evidence:** Use [logging](app/core/logging.py) and read the
  [logging/redaction contract](docs/deployment.md#shared-loki-logging) or
  [evidence format](docs/acceptance.md#redacted-evidence-format), as applicable.
- **Compose or release tooling:** Read [deployment](docs/deployment.md) and the
  current [release workflow](.github/workflows/deploy.yml). Read
  [bootstrap](docs/bootstrap-production.md) for media/state volume migration and
  [sidecar maintenance](docs/deployment.md#media-proxies-and-po-token-sidecar-maintenance)
  for provider-sidecar changes. Follow each operation's documented scope.
  Keep Linux deployment scripts LF when exporting a Windows checkout.

For shell/Compose changes, run the checks in
[offline acceptance](docs/acceptance.md#offline-acceptance). Use
`docker compose config --quiet` to validate syntax without printing resolved
secrets; report an unavailable Docker runtime as an unrun check.

## Performance evidence

Measure a baseline before optimizing, then compare the same request semantics
and environment afterward. Separate download, transform, and delivery costs;
retain contract checks while measuring the affected path. Production claims
follow the [acceptance evidence boundary](docs/acceptance.md#current-verification-boundary).
