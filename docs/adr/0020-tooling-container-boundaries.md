# Build the bot image in CI and keep dependency ports private

CI builds the bot image and publishes it to GHCR for digest-pinned activation. The runtime stage carries the builder's Deno binary for YouTube's JavaScript challenge and runs with UID/GID 10001 so the initialized media and state volumes are writable. The shell uses `exec` so Uvicorn is PID 1 and receives the container stop signal directly.

Host Caddy connects to the bot through its loopback-bound port. The bot reaches Redis, Local Bot API, and the bgutil PO-token server by Compose service name. These dependencies stay on the Compose network. The bgutil sidecar supplies the Proof-of-Origin tokens used by the local YouTube extractor's bot-check path.

[Deployment and rollback](../deployment.md)
