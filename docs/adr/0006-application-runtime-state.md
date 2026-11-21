# Separate ephemeral state and workload limits

Redis state storage is disposable. Preserve reads of older unprefixed JSON and
compress only larger payloads. Redis clear operations escape namespace glob
characters and confirm the prefix before deleting. Unprefixed storage owns the
whole database. Backends can fail open for ephemeral reads, writes, and token
buckets. The limiter uses one Lua transaction and a single string with expiry
to cut Redis command use.

Separate lightweight API queues from extractor and conversion queues so heavy
jobs cannot starve API work. Bound disk work independently. The janitor uses a
warning threshold for alerts and a critical threshold for cleanup and
admission control. Missing Telegram alerts and metrics updates leave maintenance
running. The legacy subprocess relay reads in 1 MiB chunks to limit the number
of read and write calls.

Production requires its configured Local Bot API. Development can run without
one. Legacy upload and download limit settings resolve to one consistent media
limit, and the old Instagram session setting remains readable when the pool
setting is absent. Legacy proxy setting formats remain accepted. The downloader
module continues re-exporting its original public names for existing callers.
Startup re-emits import-time cookie diagnostics after logging is configured.
See [required configuration](../deployment.md#required-configuration).
