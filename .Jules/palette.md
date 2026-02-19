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

## 2026-02-05 - Interaction State Recovery
**Learning:** In stateless bot flows (like selecting a download format), users frequently mis-click. Without a "Back" button, they are forced to restart the entire process (re-send link), creating high friction. Using cached metadata to restore previous menu states turns a "fatal error" into a simple correction.
**Action:** In multi-step bot interactions, always implement a "Back" button that reconstructs the previous view using cached session data instead of asking for re-input.

## 2026-02-06 - Preventing Dead Ends
**Learning:** In async flows (like file uploads), error states often leave users stranded without a way to retry or access an alternative solution (e.g., direct link). Providing a "Download Link" button on failure ensures the user can still achieve their goal.
**Action:** Always provide "Back" and "Alternative Action" buttons on error screens to prevent user frustration.

## 2026-02-07 - Immediate Feedback in Bots
**Learning:** In async bot flows, silent operations (like starting a download) cause user uncertainty. A 'toast' notification (callback answer with text) provides immediate confirmation that bridging the gap before the first message edit.
**Action:** Always provide immediate feedback (toast or message edit) upon button interaction to confirm the command was received.

## 2026-02-07 - Cleaner Labels
**Learning:** Technical suffixes like "(best)" or "(128k)" in format labels add cognitive load. Users generally assume "Audio" implies the best available quality unless specified otherwise.
**Action:** Remove technical jargon from format labels where possible; use simple, descriptive terms like "Audio" or "HD".

## 2026-02-08 - Dismissive Actions in Chat UIs
**Learning:** Users often trigger bot commands by mistake or change their mind after seeing options. Without a clear "Close" or "Dismiss" action, the chat history becomes cluttered with stale interactive elements.
**Action:** Always include a "Close" (❌) button in persistent bot menus to allow users to clean up their interface.

## 2026-02-09 - Heartbeat Animations in Chat UIs
**Learning:** In long-running processes (like downloads), a static icon (⏳) can feel frozen even if a progress bar updates. Toggling between two states (⏳/⌛) on every update creates a "heartbeat" effect that reassures the user the process is alive and active, without requiring text changes.
**Action:** Use simple ASCII/Emoji animations for indeterminate or long-running states in text interfaces to signal liveness.
