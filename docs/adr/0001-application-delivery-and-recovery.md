# Deliver from the durable inbox and keep receipts authoritative

SQLite owns webhook admission, worker leases, and delivery receipts. A confirmed
or uncertain send stays authoritative across retries, restarts, and cache
expiry. Receipt-write failures reach the worker so a provider fallback cannot
accidentally repeat delivery. Telegram toasts, markup, progress edits, deletion,
and error reporting are best effort. They may fall back to simpler UI and can
fail independently of the recorded delivery result. Admin error notifications
trim the traceback to fit Telegram's message limit.

The worker retains its lease and heartbeat until owned side effects settle,
including a handler interrupted by repeated cancellation or a SQLite transaction
running in a thread. Startup synchronizes its last launch check with shutdown.
Shutdown attempts independent adapter cleanup within the container's remaining
grace. A deferred lifespan only closes resources it adopted and leaves bindings
owned by a newer lifespan intact.

Acceptance cancellation uses a side table to bind the correlation at webhook
admission and record cancellation independently of send receipts. A request
fences recovery and new delivery reservations for that job. Confirmation comes
from the pre-claim withdrawal transaction or the same live worker after joining
the job's cleanup. A confirmed cancellation uses a terminal base job state so
schema v4 rollback images leave it retired. A pending request still requires
reconciliation when an older worker takes over. See
[candidate timeout recovery](../acceptance.md#controlled-task-14-run).

Readiness uses a bounded, authenticated Local Bot API probe and reports a
sanitized result. Webhook admission also rate-limits the source IP. HTTP media
providers publish a file path only when the transfer has completed. See
[durable delivery and readiness](../deployment.md#durable-inbox-drain-and-restart-behavior)
and [offline acceptance](../acceptance.md#offline-acceptance).
