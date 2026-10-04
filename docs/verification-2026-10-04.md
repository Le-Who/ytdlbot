# Media download verification on October 4 2026

This record covers the recent media selection changes, the configured proxy
routes, dependency updates, and the original-audio fix. It records one reported
YouTube Short checked from the production VPS and the associated release checks.
It does not replace the full [24-link acceptance procedure](acceptance.md).

## Releases and behavior

| Date | Commit | Change |
|---|---|---|
| September 20 to 21 | `37c75af` through `07be85e` | Shared JSON logging, bounded candidate smoke, and scoped immutable release verification |
| September 22 | `584f837` | Prefer lighter compatible video plans for default command-scoped requests; refine FxTwitter handling |
| September 23 | `7931bd8` | Default Shorts prefer 720p, then 1080p; eligible untouched FxTwitter videos may be silent |
| September 28 | `a20ec58` | Keep a cheap TikTok reserve lookup active during a selected yt-dlp download |
| October 4 | `6ca8d64` / [PR 142](https://github.com/Le-Who/ytdlbot/pull/142) | Sticky SOCKS5 routes, bounded fallback, and compatible dependency updates |
| October 4 | `dd6f0ec` / [PR 143](https://github.com/Le-Who/ytdlbot/pull/143) | Bounded YouTube HTTP ranges and cancellation of unread transfers |
| October 4 | `133d804` / [PR 144](https://github.com/Le-Who/ytdlbot/pull/144) | Original YouTube audio, cache policy version, and language preservation on refresh |

Explicit quality and audio-language requests retain their existing constraints.
The original-audio policy applies to the identified original language of each
video; it does not force Russian globally. If extraction identifies no original,
the provider retains the available audio tracks and their usual ordering.

## Production release verification

The [release workflow](https://github.com/Le-Who/ytdlbot/actions/runs/37176392579)
completed verification, immutable image build, and activation for
`133d80444035d93d3be5e0f9298fa7adf48a7e7b`. Activation followed the required
GitHub `production` reviewer approval. The post-activation local readiness
response reported:

- `ready=true` and the exact release SHA above;
- Local Bot API delivery profile with a 2,000 MB decimal limit;
- writable durable store, schema version 4, and WAL journal mode;
- a required and successful Local Bot API functional probe.

The separate post-activation public readiness check also returned `ready=true`
and the same SHA. `getWebhookInfo` matched the application's configured origin
plus `/webhook`, with zero pending updates. The activation workflow checks these
contracts before committing the release manifest. Routine activation replaced
the bot service; it did not reset Redis, SQLite, Telegram sessions, or media
volumes.

The original-audio patch was independently reviewed. Before its squash merge,
the full Linux suite ran on `dca801f` using Python 3.12.15 and the production
runtime dependencies in an isolated container: **1,278 passed, 4 skipped,
5 deselected, and 37 subtests passed**, with **75.79% coverage**. Release CI then
verified the merged SHA with its bounded release-critical gates.

## Runtime and dependency pins

The source of truth for the tested release is
[`Dockerfile`](../Dockerfile), [`requirements.txt`](../requirements.txt),
[`requirements-ci.txt`](../requirements-ci.txt), and
[`docker-compose.yml`](../docker-compose.yml).

| Component | Tested pin |
|---|---|
| Container Python | 3.12.15 |
| Deno | 2.9.7 |
| yt-dlp | 2026.8.19 |
| yt-dlp-ejs | 0.8.0 |
| bgutil Python plugin and PO-token server | 2.0.1 for both |
| python-telegram-bot | 22.8 |
| FastAPI / Uvicorn | 0.142.2 / 0.54.0 |
| gallery-dl | 1.32.15 |
| curl_cffi | 0.16.3 |
| python-dotenv / cachetools / msgspec | 1.2.4 / 7.2.0 / 0.22.0 |
| Redis Python client / instaloader | 8.1.0 / 4.15.3 |
| pytest / Hypothesis | 9.1.1 / 6.168.3 |

The running PO-token server was previously 1.3.1 despite a newer configured
plugin/server pair. Bot-only activation does not update that dependency service;
the scoped maintenance procedure brought the existing server to 2.0.1 before
the proxy release. The global cookie set demonstrated to contain invalid
YouTube/Google cookies was cleared. Other platform cookies and Instagram
sessions were preserved.

## Proxy routes and measured downloads

The case is the public Short `LSie-2eT6gw`, titled
“Самая милая собеседница”, with a reported duration of 120 seconds. Credentials
and signed CDN URLs are excluded from this record. Primary and backup refer to
the configured route order, not to independent provider backends.

YouTube metadata extraction and materialization succeeded through both selected
SOCKS5 routes. Downloading directly from a CDN URL extracted through a proxy
returned HTTP 403, confirming that this case needs the selected route preserved
through extraction and transfer.

| Route | Download before bounded-range change | Download after change | Metadata extraction after change |
|---|---:|---:|---:|
| Primary | 123.17 s | 31.85 s | 3.49 s |
| Backup | 105.45 s | 32.65 s | 4.35 s |

These are individual measurements for this case, excluding metadata extraction
from the download column. They are not p50/p95, a cohort speedup, or a success-rate
estimate. They preceded the original-audio fix and therefore do not establish
correct audio selection.

The configured pool uses proxies first for YouTube. Local yt-dlp/gallery-dl
extractors for Instagram, Facebook, TikTok, Pinterest, VK, Rutube, and X/Twitter
try direct access first and use the pool after a transient or access-denied
failure. Native API providers retain their direct paths. Other platforms were
covered by routing contracts; this record does not claim equivalent live
downloads for every platform.

## Original audio verification

Live metadata exposed English audio with `language_preference=-1` and Russian
audio marked original with `language_preference=10`. Both medium AAC tracks had
the same bitrate. The old candidate order selected `298+140-0` with `en-US`
before `298+140-1` with `ru`.

With the fix, every candidate for this request used Russian original audio. A
real materialization selected `298+140-1` and produced:

- H.264 video, 720 by 1280;
- AAC audio and approximately 120 seconds of media;
- 35,043,040 output bytes;
- 32.00 seconds of download time in the pre-activation verification.

The video's decoded audio was compared with a separately downloaded original
AAC track. Both produced this SHA-256:

```text
9abe7a8c8081046d31f0e223b9e0bccf6bba57b1356a5bd9b3db21229fcc9c1f
```

The same check was repeated in the activated `133d804` production container.
It again selected `298+140-1`, produced 35,043,040 bytes, and matched the original
decoded-audio hash. That download took 33.57 seconds. Its new request cache key
differed from the old translated-download key.

The default audible YouTube request key includes
`audio_track_policy=youtube-original-v1`, bypassing earlier translated plans
and Telegram file IDs. Candidate fallbacks exclude dubs when an original is
identified. Refreshed audible candidates retain a known selected language;
changed or missing language metadata is rejected before opening the new URL.
Muted animation refreshes are unaffected.

## Remaining acceptance scope

This record establishes the described extraction, materialization, audio, and
release checks for one case. It does not assert a new Telegram delivery result,
the full 24-link run, multiple time windows, p50/p95 latency, statistical success
rate, or the original 25% cohort improvement criterion.

The default pipeline still has no implemented independent external YouTube
provider. `independent-youtube` remains a routing slot. The two SOCKS5 routes use
the same local yt-dlp backend, and their successful downloads do not satisfy a
free independent route without proxies, cookies, payment, or manual CAPTCHA.
