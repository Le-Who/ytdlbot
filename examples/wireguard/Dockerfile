FROM lscr.io/linuxserver/wireguard:1.0.20260223-r0-ls130@sha256:10db56ad834f94541307d5dfdbe87370e26d155bb341a5bf65789f970f24ab5c

ARG TARGETARCH
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) arch=x86_64; checksum=5b67b71d27ee3a179243c461f1c06eba4d4c2344ead13396e02e7da7aa28bc7a ;; \
      arm64) arch=aarch64; checksum=5172991c6e8ec87fa636604de2193e9e65991e445d0313de4cd50b1973ce79ce ;; \
      *) printf 'Unsupported WireGuard image architecture: %s\n' "$TARGETARCH" >&2; exit 1 ;; \
    esac; \
    package=/tmp/microsocks-1.0.5-r0.apk; \
    curl --fail --silent --show-error --location \
      "https://dl-cdn.alpinelinux.org/alpine/edge/testing/$arch/microsocks-1.0.5-r0.apk" \
      --output "$package"; \
    printf '%s  %s\n' "$checksum" "$package" | sha256sum -c -; \
    apk --no-network add --no-cache "$package"; \
    rm "$package"

COPY --chmod=0755 start-proxy.sh /usr/local/bin/start-wireguard-proxy
ENTRYPOINT ["/usr/local/bin/start-wireguard-proxy"]
