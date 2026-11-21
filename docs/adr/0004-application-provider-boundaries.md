# Validate provider routes independently

Resolver and download traffic share the same production DNS validation, redirect
checks, byte bounds, and credential stripping. SOCKS routes retain the same
locally resolved IP. Request scope grants cookie access only through an explicit
provider configuration. Legacy callers continue to use their configured cookies
and proxies. Instagram CDN transfer uses a fresh session without authentication
cookies after the post metadata is resolved.

An error from one Cobalt origin cannot establish what another configured origin
will do. Keep adapter exceptions local, classify unknown failures as internal,
and reserve part of the request deadline for sequential fallback. A fully cooled
proxy pool permits one recovery attempt per request. Optional probes, metadata,
and UI observers can fail without invalidating completed media.

The pipeline can run a lightweight TikTok resolver while yt-dlp attempts a native
CDN. Legacy paths use gallery-dl after an API extraction failure and a new
yt-dlp source when the selected CDN fails or yields an unusable codec. TikWM
chooses the advertised high-quality variant and leaves codec checks to
downstream probing. Its signed URL cache uses a lifetime below the advertised
expiry. Optional slideshow audio can fail while completed images remain usable.

The authorized Instagram adapter tries the web and mobile endpoints with its
session pool. Health checks quarantine rejected sessions, and an unavailable
optional tray does not discard available stories. The Pinterest adapter can
resolve an MP4 from embedded JSON when its Open Graph fields omit a video.
Legacy VK uses the mobile host and translates a `badbrowser.php` redirect into
a specific access message. yt-dlp runs in the application's Python environment,
and its optional extraction JSON avoids redundant resolution. Its extraction
command includes an empty TikTok `app_info` extractor argument for the legacy
adapter. YouTube PO tokens come from an explicitly configured provider, and
yt-dlp selects a supported client.

See [provider state](../../README.md#provider-state) and
[media proxy configuration](../deployment.md#media-proxies-and-po-token-sidecar-maintenance).
