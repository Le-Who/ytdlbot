# Keep production evidence redacted and fail closed on timeouts

The collector and Docker adapter emit closed error categories, adapter actions, or exit statuses because subprocess I/O and request fields may contain credentials and URLs. Correlated raw logs stay inside the adapter for parsing. An isolated harness failure emits an unsafe result with a nonzero exit. The separate developer diagnostic displays provider and plugin errors directly and allows cancellation to propagate.

On candidate timeout, the adapter requests cancellation through the durable
correlation binding and accepts confirmation only after withdrawal or worker
cleanup. The isolated legacy profile can confirm removal of its own container.
Either result stops collection and leaves the `IN_FLIGHT` reservation in place.
Reconcile it before attempting the case again because Telegram may already have
received part of the request.

[Controlled Task 14 run](../acceptance.md#controlled-task-14-run) · [Scoped candidate cancellation](https://github.com/Le-Who/ytdlbot/issues/152)
