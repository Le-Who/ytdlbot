## 2024-10-12 - Telegram Bot Formatting
**Learning:** Telegram bots support HTML parse mode which allows for much richer and scannable welcome messages (bold, lists) compared to plain text.
**Action:** Always check if `parse_mode='HTML'` is enabled for bot responses to allow for bolding and lists to improve readability.

## 2024-10-12 - User-Centric Bot Status
**Learning:** Users prefer 'Searching' or 'Processing' over technical terms like 'Analyzing' as it maps better to their mental model of retrieving a file.
**Action:** Use active verbs that describe the user's intent rather than the system's internal process in status messages.

## 2026-02-01 - Video Format Scannability
**Learning:** Adding visual anchors (emojis like 📺, 📹, 🎵) to dense technical lists (video formats) significantly improves scanability and helps users quickly distinguish quality tiers.
**Action:** Use consistent iconography for data types in list views (e.g., specific icons for resolution tiers).

## 2026-02-04 - Progress Indicators in Chat Interfaces
**Learning:** In chat interfaces where real-time UI updates are limited, text-based visual progress bars (e.g., `[████░░]`) provide critical feedback for long-running processes, reducing user uncertainty compared to just percentage numbers.
**Action:** Implement text-based visual bars for any process taking longer than 5 seconds in chat-based UIs.
