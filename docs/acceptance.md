# Media release acceptance

Acceptance has two deliberately separate layers:

1. **Offline release contracts** are deterministic and run in CI without calling
   public media providers.
2. **Controlled live evidence** is opt-in, runs from the production VPS in Task
   14, and is the only basis for production-provider or latency claims.

Passing the offline suite means that the implemented contracts work against local
fixtures and fakes. It does not prove that YouTube or any external provider works
from the current production IP.

## Offline acceptance

Run the aggregate gate with:

```sh
python -m pytest tests/acceptance/test_media_release.py --no-cov -q
```

The aggregate test executes existing release-critical behavior rather than
copying its assertions:

| Contract | Existing behavioral regression |
|---|---|
| Equivalent provider race | a valid second provider wins without waiting for a hung first provider |
| File-ID cache bypass | a cached Telegram `file_id` is delivered before resolver or transport work |
| Cache schema/equivalence | output kind, quality, clip, audio, auth, album order, and schema version remain isolated |
| Cancellation | a cancelled queue waiter returns its slot and leaves the queue reusable |
| Cancellation cleanup | race losers, HTTP streams, subprocess trees, leases, sockets, and partial files are bounded and released |
| Delivery boundaries | confined shared-volume paths use Local Bot API path delivery; external paths use streaming multipart |
| Shared topology | bot and Local Bot API share only the named media volume, while durable state remains bot-only |
| Local Bot API limit | the degraded cloud profile uses the exact decimal byte boundary before opening or sending |
| Readiness | release, 2,000 MB policy, durable store, and required Local Bot API state are exposed |
| Durable admission/recovery | Telegram update IDs deduplicate; checkpointed and failed jobs recover without replaying successful or uncertain sends |
| Deploy rollback | readiness failure and interrupted activation restore the captured release state |
| Immutable retry | an interrupted same-SHA promotion is reusable only when every allowlisted payload byte matches |
| Callback compatibility | v2 is emitted while the previous callback shape still decodes for the existing link-cache TTL |

The previous callback shape must remain accepted for at least
`LINK_TTL_MINUTES` (currently 60 minutes by default) after the release that starts
emitting v2 callbacks. Extending that decoder is harmless; removing it before the
TTL expires breaks already displayed Telegram buttons.

The full local verification checkpoint for this task is:

```sh
python -m pytest tests -q
ruff check app tests
mypy app
bash -n scripts/bootstrap-production.sh scripts/bootstrap-migrate-production.sh \
  scripts/deploy-release.sh scripts/preflight-production.sh \
  scripts/rollback-release.sh
```

Run `docker compose config` when Docker is installed. Lack of Docker is reported,
not emulated and not described as a successful Compose runtime check.

## The 24-link YouTube manifest

[`tests/fixtures/youtube-acceptance.json`](../tests/fixtures/youtube-acceptance.json)
contains exactly 12 Shorts and 12 ordinary-video candidates. Their public
metadata and recorded traits were checked without downloading the media, but
that metadata check is not production-IP evidence. The independently reviewed
set is checked in with `approval.status = "approved"` so the controlled Task 14
runner can use it; approval selects test material and does not claim delivery.

Before every Task 14 run, the operator must still verify that all 24 candidate
URLs remain public, that their traits remain suitable, and that the media is
safe to exercise. CI never changes approval fields automatically. The schema is
[`youtube-acceptance.schema.json`](../tests/fixtures/youtube-acceptance.schema.json).
The acceptance validator also requires unique URLs and aggregate coverage of:

- vertical and horizontal media;
- short and long duration;
- 720p and 1080p;
- separate audio/video streams;
- multiple audio tracks;
- a recent public upload;
- an expected delivered file larger than 50 MB.

Approval selects test material; it is not evidence that any route delivered it.

## Current verification boundary

The local yt-dlp/Deno/EJS/PO-token route and the provider adapters are implemented
and covered by offline contracts. Cobalt and SnapSave require explicit
contract-verification configuration before their routes are eligible. TikWM,
SSSTik, FxTwitter, Pinterest, gallery-dl, and yt-dlp availability still depends on
the public source and the network location.

As of this documentation task, there is **no checked-in production-IP evidence**
that a free independent external YouTube route performs complete delivery without
cookies, proxies, payment, or manual CAPTCHA. This remains an open Task 14
criterion. Metadata, a provider health endpoint, a web page, or a GitHub-hosted
diagnostic does not close it.

## Opt-in network diagnostics

`.github/workflows/integration.yml` always runs offline acceptance. Its existing
real yt-dlp/ffmpeg network diagnostics run only through an explicit
`workflow_dispatch` input, `run_live_diagnostics=true`. They execute from a
GitHub-hosted runner and are labelled diagnostic; they are not production-IP
acceptance and are not a required gate on every push.

The production live run is separately authorized. Do not make it automatic, add
personal cookies, switch to a paid service, or route it through an unapproved
proxy merely to produce a green report.

## Controlled Task 14 run

The currently authorized production check is a bounded representative smoke,
not the multi-hour statistical experiment originally proposed for Task 14. For
either runtime profile it runs only the first approved Short once with a cold
state. It does not run a warm repeat, the other 23 links, or three time windows.
The resulting report must mark latency p50/p95, the 25% improvement target, and
statistical success rate as `NOT_MEASURED` and `accepted=false`; the single
delivery result must never be extrapolated into a release-performance claim.

Run the collector only from the trusted production host in a private directory.
For `legacy-baseline`, `scripts/media-acceptance-docker-adapter.py` starts a
one-shot container from the exact attested image. It does not post to the running
bot, start the application lifespan, install a webhook, or share the production
Redis/cache state. The one-shot imports the legacy orchestrator and processes one
case synchronously; process exit is its terminal boundary. `BOT_TOKEN` and
`ADMIN_CHAT_ID` enter that container only through process substitution from the
running bot's environment. They never enter host stdout, an evidence file, or a
command argument. Do not put a secret, candidate URL, or signed media URL in an
environment variable or command argument.

For `candidate`, the collector first proves an exact digest/release binding,
quiescent metrics, and exact case-scoped cache eviction. It then posts one
synthetic update to the configured administrative delivery target and observes
the durable job, correlated events, metrics, and delivery outcome. The atomic
`IN_FLIGHT` reservation and audited-reconciliation rule are identical to the
full collector, so an unknown submit outcome is never replayed automatically.

First run the no-send preflight. Use `legacy-baseline` for the existing legacy
image (`/health`, legacy timing counters, `inf:<exact URL>` cache) and `candidate`
for the new image (`/health/ready`, durable JobStore, Local Bot API, exact 2,000
MB media policy, and MediaCache keys). Both profiles require the actual 2 GiB bot
cgroup limit and a running `bot` belonging to the named Compose project. This
candidate example does not submit an update:

```sh
RELEASE_SHA=0123456789abcdef0123456789abcdef01234567
EXPECTED_IMAGE_ID=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
printf '{}\n' | python3 scripts/media-acceptance-docker-adapter.py \
  --project-dir /opt/ytdlbot \
  --project-name ytdlbot \
  --compose-file docker-compose.yml \
  --expected-release "$RELEASE_SHA" \
  --expected-image-id "$EXPECTED_IMAGE_ID" \
  --runtime-profile candidate \
  preflight
```

`EXPECTED_IMAGE_ID` must come from the reviewed build/deployment record, not be
discovered and trusted by the same collection command. Candidate preflight also
requires `APP_RELEASE` to equal `RELEASE_SHA` and the configured image reference
to be pinned by registry digest.

After that no-send preflight, collect and finalize the one candidate smoke:

```sh
install -d -m 0700 /var/lib/ytdlbot/media-evidence
ADAPTER="python3 scripts/media-acceptance-docker-adapter.py --project-dir /opt/ytdlbot --project-name ytdlbot --compose-file docker-compose.yml --expected-release ${RELEASE_SHA} --expected-image-id ${EXPECTED_IMAGE_ID} --runtime-profile candidate"
python3 scripts/collect-media-release-evidence.py collect-window \
  --manifest tests/fixtures/youtube-acceptance.json \
  --output "/var/lib/ytdlbot/media-evidence/${RELEASE_SHA}-smoke.json" \
  --window window-1 \
  --release-sha "$RELEASE_SHA" \
  --correlation-prefix "media-smoke-${RELEASE_SHA}" \
  --timeout-seconds 300 \
  --collected-at 2026-09-20T22:20:00Z \
  --adapter-command "$ADAPTER" \
  --plan smoke

python3 scripts/collect-media-release-evidence.py finalize-smoke \
  --window-file "/var/lib/ytdlbot/media-evidence/${RELEASE_SHA}-smoke.json" \
  --output "/var/lib/ytdlbot/media-evidence/${RELEASE_SHA}-smoke-report.json" \
  --collected-at 2026-09-20T22:25:00Z
```

The current VPS legacy baseline has this reviewed deployment/image pair. Create
its explicit attestation in the private evidence directory:

```sh
install -d -m 0700 /var/lib/ytdlbot/media-evidence
cat > /var/lib/ytdlbot/media-evidence/legacy-attestation.json <<'JSON'
{"deployment_sha":"5c1aaa1b786a92e979f80b09033b71978cbae701","image_id":"sha256:2b109643e8cb04386b2508d112cbab02b1648e2b02f1827356039a59448ef3cf"}
JSON
chmod 0400 /var/lib/ytdlbot/media-evidence/legacy-attestation.json
```

Run the baseline preflight with that exact pair, never an unattested caller SHA:

```sh
BASELINE_SHA=5c1aaa1b786a92e979f80b09033b71978cbae701
BASELINE_IMAGE_ID=sha256:2b109643e8cb04386b2508d112cbab02b1648e2b02f1827356039a59448ef3cf
printf '{}\n' | python3 scripts/media-acceptance-docker-adapter.py \
  --project-dir /opt/ytdlbot \
  --project-name ytdlbot \
  --compose-file docker-compose.yml \
  --expected-release "$BASELINE_SHA" \
  --expected-image-id "$BASELINE_IMAGE_ID" \
  --legacy-attestation /var/lib/ytdlbot/media-evidence/legacy-attestation.json \
  --runtime-profile legacy-baseline \
  preflight
```

The no-send legacy preflight starts the same isolated image with Redis, webhook,
cookies, provider credentials, platform proxy settings, and every standard
upper/lowercase HTTP proxy environment variable cleared. It verifies the
orchestrator import, Local Bot API configuration, administrative delivery target,
legacy 900 MB policy, and 2 GiB cgroup limit, then exits without processing or
sending media. A project-scoped `flock` and deterministic labelled container name
prevent overlapping one-shot runners. The container is read-only and has private
tmpfs-backed temp and cache directories. On timeout or interruption the adapter
force-removes only the exact labelled evidence container and polls until Docker
proves it absent while the project lock is still held. Failure to prove absence
is a hard failure; the unresolved `IN_FLIGHT` reservation still blocks replay.

After preflight, collect exactly one smoke record. `window-1` is retained only as
the schema-compatible bounded run identifier; it is not one of three statistical
windows:

```sh
install -d -m 0700 /var/lib/ytdlbot/media-evidence
BASELINE_SHA=5c1aaa1b786a92e979f80b09033b71978cbae701
BASELINE_IMAGE_ID=sha256:2b109643e8cb04386b2508d112cbab02b1648e2b02f1827356039a59448ef3cf
ADAPTER="python3 scripts/media-acceptance-docker-adapter.py --project-dir /opt/ytdlbot --project-name ytdlbot --compose-file docker-compose.yml --expected-release ${BASELINE_SHA} --expected-image-id ${BASELINE_IMAGE_ID} --legacy-attestation /var/lib/ytdlbot/media-evidence/legacy-attestation.json --runtime-profile legacy-baseline"
python3 scripts/collect-media-release-evidence.py collect-window \
  --manifest tests/fixtures/youtube-acceptance.json \
  --output "/var/lib/ytdlbot/media-evidence/${BASELINE_SHA}-smoke.json" \
  --window window-1 \
  --release-sha "$BASELINE_SHA" \
  --correlation-prefix "media-smoke-${BASELINE_SHA}" \
  --timeout-seconds 300 \
  --collected-at 2026-09-20T12:00:00Z \
  --adapter-command "$ADAPTER" \
  --plan smoke

python3 scripts/collect-media-release-evidence.py finalize-smoke \
  --window-file "/var/lib/ytdlbot/media-evidence/${BASELINE_SHA}-smoke.json" \
  --output "/var/lib/ytdlbot/media-evidence/${BASELINE_SHA}-smoke-report.json" \
  --collected-at 2026-09-20T12:10:00Z
```

Legacy independent-provider telemetry is explicitly unavailable. Neither legacy
nor candidate smoke can satisfy comparative or statistical release acceptance;
the candidate smoke proves only its one representative delivery outcome.

Before the one-shot container starts, the output is atomically replaced with an
`IN_FLIGHT` reservation containing deterministic update/correlation IDs. After
an SSH loss, timeout, or unknown result, resume hard-stops and never launches the
case again. `Future.cancel()` is not application cancellation. A timeout asks the
adapter to stop only the deterministic, correctly labelled evidence container;
the reservation remains unresolved either way. Only after an operator has
independently reconciled the result may they record an audited decision:

```sh
python3 scripts/collect-media-release-evidence.py reconcile-in-flight \
  --output "/var/lib/ytdlbot/media-evidence/${BASELINE_SHA}-smoke.json" \
  --decision confirmed-not-accepted \
  --audited-by release-owner \
  --reconciled-at 2026-09-20T12:15:00Z
```

The adapter protocol is JSON on standard input/output. In smoke mode the
collector invokes `identity`, `observe-isolated`, and, only after a timeout,
`cancel`. Nonzero exit, invalid JSON, or output outside the closed enums fails
without echoing adapter stdout/stderr. The case URL travels only over stdin. The
isolated harness measures its own cgroup CPU and sums `VmRSS` for its process
tree, wraps the real legacy Telegram sender to confirm terminal delivery, and
reports only a closed sanitized observation. It never starts `app.main`, never
calls webhook APIs, never uses production Redis, never runs `FLUSHDB` or a cache
scan, and never deletes a shared media directory.

There is intentionally no fallback that guesses first-byte latency, derives CPU
from wall time, labels temporary-directory growth as downloaded bytes, or calls
a downloaded file a successful Telegram delivery. Legacy first-byte remains
`unavailable`; downloaded bytes may use only the exact file size observed at the
wrapped delivery boundary. Missing attribution stays `null` with closed
`unavailable` metadata.

A run counts as success only when the requested media is fully delivered through
Telegram with the requested kind, quality/clip/audio policy, album completeness,
and orientation. Resolver metadata or a downloaded file without confirmed
delivery is not success.

The smoke record collects:

- `resolve`, `first_byte`, `materialize`, and `deliver` latency in seconds;
- confirmed `full_delivery` and the failure stage when false;
- categorized HTTP 403 and 429 causes (never raw signed URLs or response secrets);
- downloaded and wasted bytes;
- measured process CPU seconds and peak resident memory as `peak_rss_bytes`;
  keep wall-clock transform workload separate when process CPU is unavailable;
- whether independent-route attempt telemetry is available, its exact-metric or
  correlated-event provenance, and—only then—whether the route was attempted and
  succeeded. Provider configuration alone is not an attempt. If neither signal
  exists (legitimate for the legacy image), all route outcomes are explicitly
  unavailable and the comparison fails closed.

Every available latency value must be finite and non-negative. A successful
delivery may record first-byte as `null` only with explicit `unavailable`
metadata; that is truthful evidence, but `measurement_complete=false` makes the
release comparison fail. Failed runs may use `null` for stages they did not
reach.

The smoke report performs no cohort aggregation. It preserves the exact runtime
image identity/reference and binding method plus
`current_memory_limit_bytes`. Only `representative_delivery_smoke` is measured;
p50/p95, 25% improvement, and statistical success-rate decisions are explicitly
not measured and not accepted.

## Redacted evidence format

The full multi-window contract remains documented by
[`media-release-evidence.schema.json`](../tests/fixtures/media-release-evidence.schema.json),
but it is not produced or accepted by the authorized smoke plan. A future full
run would require separate approval because it is intentionally long-running.
The pinned Draft 2020-12 validator and semantic checks continue to fail closed
for any such supplied full report.

The smoke report instead contains one redacted legacy or candidate run and four
explicit decisions:

- `representative_delivery_smoke` is `MEASURED` and reflects only that run;
- `latency_p50_p95` is `NOT_MEASURED` and not accepted;
- `candidate_improvement_25_percent` is `NOT_MEASURED` and not accepted;
- `statistical_success_rate` is `NOT_MEASURED` and not accepted.

Both formats preserve:

- exact release SHA and SHA-256 of the approved manifest;
- exact runtime image ID/reference and legacy-attestation or candidate-digest binding;
- an explicit production-IP attestation;
- the four stage latencies, full delivery, 403/429 causes, bytes, CPU, peak RSS,
  and independent-route outcome;
- the positive `current_memory_limit_bytes` used by the deployment;
- affirmative redaction flags for URL hashing and removal of tokens, signed query
  strings, and cookies.

HTTP failure causes and independent-route classes are closed enums, so a raw URL,
token, or arbitrary provider response cannot be smuggled into a nominally
redacted field. `full_delivery` and `failure_stage` must agree. The smoke command
accepts exactly one `short`/`cold` record and refuses to finalize an unresolved
`IN_FLIGHT` reservation.

The integration-marked evidence validator remains excluded by default. It runs
only when explicitly selected and given `YOUTUBE_ACCEPTANCE_MANIFEST` plus
`MEDIA_RELEASE_EVIDENCE` paths:

```sh
YOUTUBE_ACCEPTANCE_MANIFEST=/secure/run/youtube-acceptance.json \
MEDIA_RELEASE_EVIDENCE=/secure/run/candidate.json \
python -m pytest tests/acceptance/test_media_release.py \
  -m integration --no-cov -q
```

Only redacted evidence is eligible to enter
`artifacts/media-release/<sha>/`. Raw logs, Telegram tokens, cookies, provider
keys, signed CDN query strings, chat/user identifiers, and unredacted URLs do not
belong in Git.

## Failure scenarios covered offline

The focused offline contracts cover behavior for:

- local extractor failure while an eligible independent route is available;
- independent route failure while local extraction remains available;
- expired signed CDN URL and bounded re-resolution;
- missing audio, wrong orientation, and cancelled mux/conversion;
- durable inbox recovery across SIGTERM/SIGKILL;
- completed delivery skip and uncertain delivery non-replay;
- drain deadline and webhook preservation;
- successful activation, readiness rollback, stale SHA, concurrent deploy,
  interrupted SSH, and insufficient disk;
- bootstrap media/state mount migration while preserving Telegram session and
  Redis volumes and leaving neighboring projects untouched.

## Release decision

The comparison targets from the binding plan remain decision criteria, not
current claims: on platforms with two working providers, cold p95 should improve
by at least 25% versus the production baseline; full-delivery success must not
regress; CPU/RAM must remain within current limits; and racing must not create
duplicate sends. The one-case smoke cannot decide the first two criteria, so its
report records them as `NOT_MEASURED`/not accepted and must not be used to approve
a release on statistical grounds.

Neither acceptance nor rollback rewinds SQLite or Redis data. Database migrations
must remain backward-compatible with the previous bot image.
