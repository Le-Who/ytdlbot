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

The approved 24-link set is run from the production VPS in exactly three time
windows identified by the fixed, non-secret ordinals `window-1`, `window-2`,
and `window-3`. Free-form or token-shaped window identifiers are rejected.
Every case is exercised once with a cold cache and once with a warm cache in
each window. Shorts and ordinary videos are reported separately.

Run the collector only from the trusted production host, in a private directory,
against the already running bot container. The checked-in
`scripts/media-acceptance-docker-adapter.py` is the security boundary around that
container: its helper reads `BOT_TOKEN`, `ADMIN_CHAT_ID`, and
`TELEGRAM_SECRET_TOKEN` and posts the webhook inside `bot`. Those values never
enter adapter output or the collector process. The adapter samples the container
and Prometheus endpoint and returns only the closed, sanitized JSON contract
described below. Do not put a secret, a candidate URL, or a signed media URL in a
command argument or environment variable.

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

The legacy gate records the actual health/media policy instead of pretending it
has candidate readiness or a 2,000 MB upload limit. A webhook HTTP 200 is only
asynchronous acceptance. The legacy image has no correlation-aware success log:
observation therefore accepts exactly one download total plus one success/failed
delta only while the whole pipeline is isolated. A download failure may finish
before delivery; a download success is not terminal until the upload-duration
counter records an HTTP attempt. The adapter then waits another quiet grace with
stable terminal counters, no yt-dlp/ffmpeg child, and no established Bot API
`:8081` connection. Missing fence capability stops the run.

One invocation collects or resumes one window. Use the actual tested release SHA
and a different real UTC period for each fixed window (the example shows
`window-1`; repeat later for `window-2` and `window-3` with their own timestamp):

```sh
install -d -m 0700 /var/lib/ytdlbot/media-evidence
RELEASE_SHA=0123456789abcdef0123456789abcdef01234567
EXPECTED_IMAGE_ID=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
ADAPTER="python3 scripts/media-acceptance-docker-adapter.py --project-dir /opt/ytdlbot --project-name ytdlbot --compose-file docker-compose.yml --expected-release ${RELEASE_SHA} --expected-image-id ${EXPECTED_IMAGE_ID} --runtime-profile candidate"
python3 scripts/collect-media-release-evidence.py collect-window \
  --manifest tests/fixtures/youtube-acceptance.json \
  --output "/var/lib/ytdlbot/media-evidence/${RELEASE_SHA}-window-1.json" \
  --window window-1 \
  --release-sha "$RELEASE_SHA" \
  --correlation-prefix "media-release-${RELEASE_SHA}" \
  --timeout-seconds 900 \
  --collected-at 2026-09-20T12:00:00Z \
  --adapter-command "$ADAPTER"
```

Append `--external-free-provider <metric-label>` or
`--external-configured-provider <metric-label>` to `ADAPTER` only for a route
whose class was reviewed for that release. An external winner without an approved
classification aborts collection; the adapter does not silently report it as an
unattempted route. Omitting both flags is therefore safe when only local `ytdlp`
is eligible, but it cannot close the independent-route acceptance criterion.

Before webhook submission, the output is atomically replaced with an `IN_FLIGHT`
reservation containing deterministic update/correlation IDs. After an SSH loss,
timeout, or unknown submit result, resume hard-stops and never resubmits that
pair. `Future.cancel()` is not application cancellation. Only after an operator
has independently proved that the webhook was not accepted may they append an
audited reconciliation and permit a retry:

```sh
python3 scripts/collect-media-release-evidence.py reconcile-in-flight \
  --output "/var/lib/ytdlbot/media-evidence/${RELEASE_SHA}-window-1.json" \
  --decision confirmed-not-accepted \
  --audited-by release-owner \
  --reconciled-at 2026-09-20T12:15:00Z
```

The output is also atomically replaced after every successful case/cache record,
so a normal resume skips completed pairs. Once all three windows contain 48
records, merge and validate them with:

```sh
python3 scripts/collect-media-release-evidence.py finalize \
  --manifest tests/fixtures/youtube-acceptance.json \
  --schema tests/fixtures/media-release-evidence.schema.json \
  --window-file "/var/lib/ytdlbot/media-evidence/${RELEASE_SHA}-window-1.json" \
  --window-file "/var/lib/ytdlbot/media-evidence/${RELEASE_SHA}-window-2.json" \
  --window-file "/var/lib/ytdlbot/media-evidence/${RELEASE_SHA}-window-3.json" \
  --output "/var/lib/ytdlbot/media-evidence/${RELEASE_SHA}.json" \
  --collected-at 2026-09-22T18:00:00Z
```

The adapter protocol is JSON on standard input/output. The collector invokes
`<adapter> identity`, `evict-case`, `observe`, and `cancel`; nonzero exit, invalid
JSON, or output outside the closed enums fails the run without echoing adapter
stdout/stderr. `identity` returns only the verified release/container/image
identity and binding method, runtime profile, positive cgroup memory limit,
capabilities, and
`production_ip_attested=true`; it never returns a token or chat ID. `evict-case`
receives the case URL on standard input. In `legacy-baseline` it deletes only
`inf:<exact URL>`. In `candidate` it additionally derives the exact metadata,
default signed-URL, and item-zero Telegram file-ID keys through the running
MediaCache implementation. It never uses `FLUSHDB`, Redis scan/pattern deletion,
a whole-media-directory deletion, or affects another case. If exact attribution
is unavailable it returns `safe=false`, and the collector stops before
submitting the cold webhook.

`observe` receives the secret-free synthesized Telegram update on standard
input. The in-container helper inserts the configured administrative chat and
secret, posts the update with the supplied `X-Correlation-ID`, samples before/after
Prometheus counters and per-phase sums/counts, follows sanitized correlated job
and delivery events, reads cgroup CPU usage, and sums `VmRSS` for every PID in
the container cgroup. It must return
`attribution_confirmed=true` only when the deltas belong to that case and a
terminal outcome is confirmed: candidate runs require a finalized successful
JobStore delivery with its returned message ID plus the success metric; legacy
runs use the real download result and upload-duration counters under the process
and Bot API network fences described above. Before every run, candidate must
have no accepted/running/checkpointed jobs and no active media work; legacy must
also have zero active downloads, no media child process, and no active Bot API
upload connection. Relevant global counters must remain unchanged for a quiet
grace interval. The collector rejects any phase/result delta or unrelated job
that reveals concurrent pipeline activity, including work accepted before the
evidence case. HTTP details are reduced to
status 403/429 plus the schema's cause enum, and provider data is reduced to the
independent route class. `cancel` attempts only an exact correlation-owned
application cancellation. The current runtime exposes no such hook, so a timeout
returns `cancelled=false`; the collector stops the entire window and does not
start another case. Fake-Docker tests exercise both runtime profiles without a
Docker daemon.

There is intentionally no fallback that guesses cold-cache state, attributes a
process-wide metric during concurrent traffic, derives CPU from wall time,
labels temporary-directory growth as downloaded bytes or first-byte latency, or
declares delivery from a downloaded file. Candidate first-byte uses the Task 11
metric only when its delta is attributable; legacy first-byte uses only a
correlated progress event. Downloaded bytes use only a structured per-request
byte value (legacy may use a correlated exact delivered file size). Otherwise
the value is `null` with closed `unavailable` metadata and the comparison fails
closed. If the production installation cannot provide exact case eviction,
correlated job/delivery outcome, or bounded cancel, the run remains unverified
rather than emitting nominal evidence.

A run counts as success only when the requested media is fully delivered through
Telegram with the requested kind, quality/clip/audio policy, album completeness,
and orientation. Resolver metadata or a downloaded file without confirmed
delivery is not success.

For each case and cache state, collect:

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

Aggregate four cohorts: Shorts/cold, Shorts/warm, videos/cold, and videos/warm.
For every stage report p50 and p95, sample count, full-delivery rate, 403/429 cause
counts, bytes, CPU, peak-RSS p50/p95/max, and independent-route success rate.
The evidence also records the exact runtime image identity/reference and binding
method plus `current_memory_limit_bytes`; each cohort's
`within_current_memory_limit` decision must equal whether its measured maximum
RSS is at or below that limit and must be true for acceptance. This makes the
same schema suitable for comparing baseline and candidate evidence without
changing the current deployment limit. Keep per-window results so one favorable
period cannot hide a later provider block.

## Redacted evidence format

The machine-readable contract is
[`media-release-evidence.schema.json`](../tests/fixtures/media-release-evidence.schema.json).
The acceptance gate validates both files with the pinned Draft 2020-12
`jsonschema` validator and format checks before applying cross-record semantic
checks. It requires:

- exact release SHA and SHA-256 of the approved manifest;
- exact runtime image ID/reference and legacy-attestation or candidate-digest binding;
- `source = "production-vps"` and an explicit production-IP attestation;
- exactly the three windows `window-1`, `window-2`, and `window-3`;
- one cold and one warm record for every approved case in every window;
- the four stage latencies, full delivery, 403/429 causes, bytes, CPU, peak RSS,
  and independent-route outcome;
- the positive `current_memory_limit_bytes` used by the deployment;
- four Shorts/video × cold/warm summaries with latency p50/p95, peak-RSS
  p50/p95/max, measurement completeness, and the derived within-limit decision;
- affirmative redaction flags for URL hashing and removal of tokens, signed query
  strings, and cookies.

HTTP failure causes and independent-route classes are closed enums, so a raw URL,
token, or arbitrary provider response cannot be smuggled into a nominally
redacted field. Each window must contain exactly one cold and one warm run for
all 24 cases. `full_delivery` and `failure_stage` must agree, summary counts and
rates must equal their underlying runs, and p50/p95 use nearest-rank values over
the non-null stage samples in the corresponding Shorts/video and cold/warm
cohort. Peak-RSS summaries use the same nearest-rank p50/p95 rule, their `max`
must equal the largest cohort run, and acceptance rejects both an inconsistent
decision and a candidate that exceeds the current memory limit.

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

## Failure scenarios required in the controlled run

In addition to the 24-link measurements, record behavior for:

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

The comparison targets from the binding plan are decision criteria, not current
claims: on platforms with two working providers, cold p95 should improve by at
least 25% versus the production baseline; full-delivery success must not regress;
CPU/RAM must remain within current limits; and racing must not create duplicate
sends. A failed criterion invokes the documented image/config rollback.

Neither acceptance nor rollback rewinds SQLite or Redis data. Database migrations
must remain backward-compatible with the previous bot image.
