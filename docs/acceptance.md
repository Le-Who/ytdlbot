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
the file intentionally retains `approval.status = "pending"`. A populated
pending file cannot be mistaken for approved or production-IP evidence.

Before Task 14, a reviewer must verify that all 24 candidate URLs remain public,
that their traits are suitable for the controlled run, and that the media is
safe to exercise. The reviewer then sets `approved_by`, `approved_at`, and
`status = "approved"`. CI never promotes a pending fixture automatically. The
schema is [`youtube-acceptance.schema.json`](../tests/fixtures/youtube-acceptance.schema.json).
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
- whether an independent route was attempted and whether it succeeded.

All four latency values on a successful (`full_delivery = true`) run are
required and must be finite, non-negative numbers. Failed runs may use `null`
for stages they did not reach, but every recorded numeric latency is subject to
the same finite, non-negative constraint.

Aggregate four cohorts: Shorts/cold, Shorts/warm, videos/cold, and videos/warm.
For every stage report p50 and p95, sample count, full-delivery rate, 403/429 cause
counts, bytes, CPU, peak-RSS p50/p95/max, and independent-route success rate.
The evidence also records `current_memory_limit_bytes`; each cohort's
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
- `source = "production-vps"` and an explicit production-IP attestation;
- exactly the three windows `window-1`, `window-2`, and `window-3`;
- one cold and one warm record for every approved case in every window;
- the four stage latencies, full delivery, 403/429 causes, bytes, CPU, peak RSS,
  and independent-route outcome;
- the positive `current_memory_limit_bytes` used by the deployment;
- four Shorts/video × cold/warm summaries with latency p50/p95, peak-RSS
  p50/p95/max, and the derived within-limit decision;
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
