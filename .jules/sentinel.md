## 2026-01-31 - [High] Insecure URL Validation
**Vulnerability:** The application used loose string matching (`any(platform in text_lower ...)`) to validate user-provided URLs. This allowed attackers to bypass validation by embedding a supported domain name anywhere in the URL (e.g., query parameters or path), enabling SSRF or unintended use of `yt-dlp`.
**Learning:** Checking for substrings is insufficient for security validation. Attackers can easily manipulate URL structures to satisfy partial matches while pointing to malicious destinations.
**Prevention:** Always parse URLs using standard libraries (like `urllib.parse`) and validate specific components (hostname, scheme) against an allowlist. Handle subdomains and port numbers correctly during validation.
## 2026-01-31 - [Medium] Denial of Service via Subprocess Deadlock
**Vulnerability:** A deadlock in subprocess communication could be triggered by a malicious or unusually verbose input causing the stderr buffer to fill up. This would cause the application thread to hang indefinitely, consuming resources and potentially leading to a Denial of Service (DoS).
**Learning:** Synchronous waiting for stderr (`await proc.wait()`) while the process is blocked on writing to stderr creates a deadlock. Buffer limits (typically 64KB) are easily reached with verbose logging or errors.
**Prevention:** Consume stderr asynchronously and continuously (e.g., using `asyncio.create_task` loop) while processing stdout, ensuring the subprocess is never blocked on I/O.
## 2026-02-04 - [Critical] Unauthenticated Telegram Webhook
**Vulnerability:** The `/webhook` endpoint accepted POST requests without validating the `X-Telegram-Bot-Api-Secret-Token` header. This allowed any attacker knowing the webhook URL to inject fake updates, potentially masquerading as users or admins.
**Learning:** Frameworks often expose endpoints that require specific security headers (like webhooks) but don't enforce them by default. Relying solely on the secrecy of the URL is insufficient.
**Prevention:** Always verify the integrity and origin of webhook requests using the platform's provided signature or secret token mechanism. Use `secrets.compare_digest` for constant-time comparison.
