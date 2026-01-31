## 2026-01-31 - [High] Insecure URL Validation
**Vulnerability:** The application used loose string matching (`any(platform in text_lower ...)`) to validate user-provided URLs. This allowed attackers to bypass validation by embedding a supported domain name anywhere in the URL (e.g., query parameters or path), enabling SSRF or unintended use of `yt-dlp`.
**Learning:** Checking for substrings is insufficient for security validation. Attackers can easily manipulate URL structures to satisfy partial matches while pointing to malicious destinations.
**Prevention:** Always parse URLs using standard libraries (like `urllib.parse`) and validate specific components (hostname, scheme) against an allowlist. Handle subdomains and port numbers correctly during validation.
