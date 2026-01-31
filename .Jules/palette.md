## 2024-10-12 - Telegram Bot Formatting
**Learning:** Telegram bots support HTML parse mode which allows for much richer and scannable welcome messages (bold, lists) compared to plain text.
**Action:** Always check if `parse_mode='HTML'` is enabled for bot responses to allow for bolding and lists to improve readability.

## 2024-10-12 - User-Centric Bot Status
**Learning:** Users prefer 'Searching' or 'Processing' over technical terms like 'Analyzing' as it maps better to their mental model of retrieving a file.
**Action:** Use active verbs that describe the user's intent rather than the system's internal process in status messages.
