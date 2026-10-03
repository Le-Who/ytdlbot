# Proxy fallbacks and dependency maintenance

The user authorized implementation, production activation, removal of the proven
expired YouTube/Google cookie set, and compatible stable dependency updates.

## Design

One process-owned `MediaProxyPool` holds configured credentials and route health.
Only opaque route keys travel with media sources; credentials stay out of media
records, cache keys, logs, and committed configuration. YouTube resolves via the
primary proxy immediately, then the backup. Local yt-dlp/gallery-dl extraction for
Instagram, Facebook, TikTok, Pinterest, VK, RuTube, and Twitter keeps its direct or
direct path first and retries the pool after a transient/auth block. For these
configured platforms the new pool owns routing instead of legacy standalone
proxy variables, so extracted URLs and stream transfers share one route.
All stream probes, redirects, and downloads use the source's selected route. A
failed route is cooled down per platform; refreshing after a transfer failure
obtains fresh URLs on the next route. SSRF validation and pinned local DNS remain
in force. Native independent provider traffic remains on its existing fast path.

## Work

- [x] Add failing route-selection, timeout/cancellation, transport affinity,
  redirect/DNS safety, log-redaction, and gallery fallback tests.
- [x] Implement `app/services/media/proxies.py`, source route keys, provider
  retries, transport binding, and composition/configuration.
- [x] Update stable compatible runtime/CI pins and align bgutil plugin/server;
  run focused tests, the full suite, linters/type checks, and release gates.
- [x] Request an independent read-only code review and address material findings.
- [ ] Store only proxy rows 1 and 4 in the existing protected VPS environment;
  clear `YTDLP_COOKIES_B64`, which contains the invalid YouTube/Google session.
- [ ] Build and activate through the existing immutable release workflow. Update
  only the project's PO-token sidecar to its matching pinned image as explicit
  maintenance; preserve neighboring applications and valid platform cookies.
- [ ] Verify deployed readiness, exact versions, primary/backup resolution, full
  Shorts materialization through the actual pipeline, and sanitized logs.

## Validation

Use fake extractors and bounded fake streaming clients to establish real route
selection and URL affinity. The regressions must fail before implementation.
Run `python -m pytest --no-cov tests/media tests/test_cookies_manager.py` initially;
then the complete repository suite with its configured coverage requirement.
Production verification uses the supplied public Short without sending unsolicited
Telegram messages. No raw proxy URLs, passwords, or signed CDN URLs enter reports.
