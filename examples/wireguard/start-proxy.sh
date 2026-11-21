#!/bin/sh
set -eu

if ! wg-quick up /config/wg0.conf; then
  printf '%s\n' 'WireGuard failed to start' >&2
  exit 1
fi
printf '%s\n' '=== WireGuard UP ==='
exec microsocks -p 1080
