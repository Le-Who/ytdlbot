# Exercise recovery across a runtime ownership change

Use a reopened SQLite store with the prior cache state and clear process-local retry markers to model restart. Model a hard crash by advancing the lease clock without checkpointing or releasing ownership. Redis cache writes can log an error and return normally, so tests simulate silent cache failure and assert that SQLite reservations still govern delivery. Cached values encoded as unprefixed JSON remain readable alongside the prefixed MessagePack formats.

Drive two actual lifespan entries against the same SQLite and Redis resources. Reject overlap before acquiring resources or installing bindings, and allow the next lifespan to start once the previous handler and deferred cleanup settle. Redis client reuse requires the mock to model reconnect on ping following a completed close. Capture the drain value at startup so a later environment change cannot break cleanup, and release ownership if configuration validation fails.

Force competing sends to complete their preflight reads before either reserves a receipt, then keep the first send in flight while the second attempts to reserve it. Preserve uncertainty when finalization fails. Report the storage failure to the worker and keep the callback's UI return value. Show the initial spinner without showing success, a retry button, or a fallback send after durable sending has entered an uncertain state. Releasing an ordinary active callback claim permits later use of the existing button.

[Durable inbox, drain, and restart behavior](../deployment.md#durable-inbox-drain-and-restart-behavior)
