#!/usr/bin/env bash
set -euo pipefail

# Fixed, read-only release source. The admin service has no egress; this
# root-owned helper gives it one bounded public read and no caller-controlled
# URL, arguments, headers, or output destination.
exec /usr/bin/curl \
  --fail \
  --silent \
  --show-error \
  --location \
  --proto '=https' \
  --proto-redir '=https' \
  --tlsv1.2 \
  --connect-timeout 5 \
  --max-time 10 \
  --max-filesize 64 \
  'https://raw.githubusercontent.com/infiloop2/trustyclaw/refs/heads/main/VERSION'
