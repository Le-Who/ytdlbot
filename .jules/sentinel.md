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
