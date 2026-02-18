## 2026-01-31 - [High] Insecure URL Validation
**Vulnerability:** The application used loose string matching (`any(platform in text_lower ...)`) to validate user-provided URLs. This allowed attackers to bypass validation by embedding a supported domain name anywhere in the URL (e.g., query parameters or path), enabling SSRF or unintended use of `yt-dlp`.
**Learning:** Checking for substrings is insufficient for security validation. Attackers can easily manipulate URL structures to satisfy partial matches while pointing to malicious destinations.
**Prevention:** Always parse URLs using standard libraries (like `urllib.parse`) and validate specific components (hostname, scheme) against an allowlist. Handle subdomains and port numbers correctly during validation.
## 2026-01-31 - [Medium] Denial of Service via Subprocess Deadlock
**Vulnerability:** A deadlock in subprocess communication could be triggered by a malicious or unusually verbose input causing the stderr buffer to fill up. This would cause the application thread to hang indefinitely, consuming resources and potentially leading to a Denial of Service (DoS).
**Learning:** Synchronous waiting for stderr (`await proc.wait()`) while the process is blocked on writing to stderr creates a deadlock. Buffer limits (typically 64KB) are easily reached with verbose logging or errors.
**Prevention:** Consume stderr asynchronously and continuously (e.g., using `asyncio.create_task` loop) while processing stdout, ensuring the subprocess is never blocked on I/O.
## 2026-02-03 - [Medium] Subprocess Deadlock via Unconsumed Stdout
**Vulnerability:** Even when `stderr` is consumed asynchronously, leaving `stdout` as `PIPE` without consuming it (or redirecting to `DEVNULL`) can still cause a deadlock if the subprocess writes to stdout (e.g., progress bars).
**Learning:** `asyncio.create_subprocess_exec` with `PIPE` requires **all** piped streams to be actively read. Disabling output flags in the command (like `--quiet`) is not always sufficient if the tool forces output (like `--progress`).
**Prevention:** Explicitly set `stdout=asyncio.subprocess.DEVNULL` for subprocesses where output is not needed, or ensure a consumer task is running for it. Always ensure subprocesses are killed in a `finally` block to unblock stream readers.

## 2026-02-02 - [High] Unauthenticated Webhook Endpoint
**Vulnerability:** The Telegram webhook endpoint `/webhook` was publicly accessible without any authentication. This allowed unauthorized actors to inject fake updates.
**Learning:** Manual webhook integration in FastAPI/Starlette requires explicit validation of the `X-Telegram-Bot-Api-Secret-Token` header using constant-time comparison.
**Prevention:** Use `secrets.compare_digest` to validate the secret token in the webhook handler.

## 2026-02-05 - [High] HTML Injection in Telegram Messages
**Vulnerability:** User-controlled input (video title) was inserted directly into an HTML-formatted Telegram message string without sanitization. This allowed attackers (or accidentally malicious titles) to inject invalid HTML tags, causing the Telegram API to reject the message and potentially disrupting service availability or spoofing content.
**Learning:** When using `parse_mode='HTML'` (or Markdown) in messaging APIs, all dynamic content must be treated as untrusted and properly escaped. Assuming that third-party data (like YouTube titles) is safe or "plain text" is a common oversight.
**Prevention:** Always use `html.escape()` for any variable interpolated into an HTML string sent to Telegram. Validate or sanitize all external inputs before rendering them in a markup format.

## 2026-02-07 - [High] SSL Certificate Verification Disabled
**Vulnerability:** The `yt-dlp` command and service configuration had SSL certificate verification disabled via `--no-check-certificate` and `'nocheckcertificate': True`. This exposed the application to Man-In-The-Middle (MITM) attacks when communicating with video platforms or fetching metadata.
**Learning:** Disabling SSL verification removes the primary defense against interception and tampering of HTTPS traffic. While often done to "fix" connection issues with legacy or misconfigured servers, it creates a significant security risk for the entire application.
**Prevention:** Never disable SSL certificate verification in production. If connection issues occur, investigate the root cause (e.g., outdated CA certificates, local network issues) rather than bypassing security checks. Ensure the system trust store is up to date.

## 2026-02-12 - [High] Missing Rate Limiting
**Vulnerability:** The rate limiting function was stubbed out (`return True`), allowing malicious users to flood the service with requests (DoS), exhausting CPU (parsing) and network bandwidth (downloading).
**Learning:** Stubbed security controls (often left from debugging) are silent vulnerabilities. Global concurrency limits (`Semaphore`) are insufficient to prevent a single user from starving others.
**Prevention:** Implement per-user rate limiting (e.g., Fixed Window or Leaky Bucket) and verify it with tests. Ensure security features are not disabled in production code.
