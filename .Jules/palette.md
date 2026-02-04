## 2024-10-12 - Telegram Bot Formatting
**Learning:** Telegram bots support HTML parse mode which allows for much richer and scannable welcome messages (bold, lists) compared to plain text.
**Action:** Always check if `parse_mode='HTML'` is enabled for bot responses to allow for bolding and lists to improve readability.

## 2024-10-12 - User-Centric Bot Status
**Learning:** Users prefer 'Searching' or 'Processing' over technical terms like 'Analyzing' as it maps better to their mental model of retrieving a file.
**Action:** Use active verbs that describe the user's intent rather than the system's internal process in status messages.

## 2026-02-01 - Video Format Scannability
**Learning:** Adding visual anchors (emojis like 📺, 📹, 🎵) to dense technical lists (video formats) significantly improves scanability and helps users quickly distinguish quality tiers.
**Action:** Use consistent iconography for data types in list views (e.g., specific icons for resolution tiers).

## 2026-02-03 - Visual Progress in Text Interfaces
**Learning:** Users in chat interfaces often perceive "text-only" percentage updates (e.g. "45%") as "spammy" or easy to miss. Adding a block-character progress bar (████░░░) transforms the message into a recognizable "UI element" that anchors the eye and conveys status at a glance without reading numbers.
**Action:** When working with chatbots or CLI tools, always prefer visual block bars over raw numbers for long-running operations.
## 2026-02-02 - Localization Consistency
**Learning:** Mixing languages in feedback (e.g. English toasts in a Russian interface) breaks immersion and trust, even for micro-interactions like toasts.
**Action:** Always check existing UI language before adding new text and match the locale.
