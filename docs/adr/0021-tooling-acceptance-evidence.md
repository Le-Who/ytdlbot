# Keep production evidence redacted and fail closed on timeouts

The collector and Docker adapter emit closed error categories, adapter actions, or exit statuses because subprocess I/O and request fields may contain credentials and URLs. Correlated raw logs stay inside the adapter for parsing. An isolated harness failure emits an unsafe result with a nonzero exit. The separate developer diagnostic displays provider and plugin errors directly and allows cancellation to propagate.

The candidate runtime lacks a correlation-to-task cancellation API. On timeout the adapter declines to confirm cancellation, the collector stops the window, and its durable `IN_FLIGHT` reservation blocks replay pending reconciliation. The isolated legacy profile can confirm removal of its own container. The reservation still requires reconciliation because the send could already have reached Telegram.

[Controlled Task 14 run](../acceptance.md#controlled-task-14-run) · [Scoped candidate cancellation](https://github.com/Le-Who/ytdlbot/issues/152)
