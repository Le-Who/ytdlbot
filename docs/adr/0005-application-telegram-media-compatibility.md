# Fit Telegram playback and upload constraints

The GIF-style video delivery uses a muted MP4. Its pipe uses fragmented MP4 for
incremental reads, while downloaded MP4 files use fast start. Pipe mode uses
yt-dlp's internal downloader because aria2c requires a file output. Legacy video
menus use yt-dlp's sidecar JPEG for previews when it exists. Native GIF export
has its own small conversion budget and caps the source size because palette
generation is CPU intensive.

The legacy cloud path prefers stream-copy splits to preserve quality and avoid
encoding. The segment target leaves room for mux overhead. Large inputs or an
unusable split fall through to two-pass compression. Local Bot API uploads skip
the cloud fitting path. Legacy URL sends use Telegram's smaller server-fetch
size ceiling and fall back to owned local files when the size hint fails.

The compatibility pass checks codec, pixel format, and tag, including 10-bit
H.264 and the formats that TikTok can return under an MP4 filename. It retains
the source when re-encoding fails. For slideshow video, use matching audio
directly where possible and estimate duration from a bounded header read. Fit
the generated frames to the codec's dimension constraints. Send photos through
the photo endpoint and batch albums at Telegram's item limit.

See [delivery profiles](../deployment.md#readiness-and-local-bot-api-degradation).
