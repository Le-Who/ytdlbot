# Bind GIF exports to the media Telegram confirmed

A link token can identify several outgoing messages. When the callback includes
a Telegram file identity, bind the export and its cache key to that actual
source. Reuse a local copy only while it matches the version that Telegram
received. Use token-only recovery for older callbacks with no file identity.

This permits GIF export if Telegram's cloud download limit or a Local Bot API
outage blocks recovery. The identity index owns metadata. The cache or
materialization owns the file. A newly generated animation can transfer into
a bounded file-cache owner after confirmed delivery so its export uses the
same bytes. Existing success or uncertain receipts remain authoritative even
after the link-cache entry expires. See [GIF lifecycle regressions](../acceptance.md#offline-acceptance).
