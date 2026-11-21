# Preserve the requested media identity across cache and fallback

An exact resolution refers to the short display edge in either orientation.
Every equivalent fallback enforces it. A separate audio and video pair stays
a mux plan until materialization. An explicit MP3 selection produces MP3 bytes.
For YouTube, prefer metadata that marks the original audio, then keep the same
language through extract, retry, refresh, and cache lookup. yt-dlp can renumber
duplicate format IDs, and older cached outputs can contain an auto-dub.

An album is an ordered sequence of selected items. An optional slideshow
soundtrack is a separate explicit output. A cached first item alone cannot
establish a complete album, and a refreshed candidate already projected to
selected items must retain its order without applying the selection again.

FxTwitter can expose the original attachment dimensions alongside a different
selected variant. Read the variant dimensions first and probe materialized
bytes when needed. An untouched FxTwitter video can be silent because the
provider lacks reliable audio-presence metadata. Requests for audio or a
transform still use the actual stream requirements. Accept known container
signatures as a bounded first check before full inspection.

The legacy format picker favors premuxed, H.264-compatible representations to
avoid extra muxing and improve playback. It can show URL-only Facebook formats
and infer conventional dimensions from `sd` and `hd` IDs. Keep deterministic
ordering when discarding duplicate resolutions. See [media contracts](../acceptance.md#offline-acceptance).
