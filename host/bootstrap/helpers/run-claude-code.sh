#!/usr/bin/env bash
set -euo pipefail
cd /mnt/trustyclaw-agent/agent-home
exec /usr/sbin/runuser -u trustyclaw-agent -- env \
  HOME=/mnt/trustyclaw-agent/agent-home \
  CLAUDE_CONFIG_DIR=/mnt/trustyclaw-agent/agent-home/.claude \
  HTTP_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  HTTPS_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  ALL_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  NO_PROXY=127.0.0.1,localhost \
  NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/trustyclaw-network-proxy.crt \
  CLAUDE_CODE_CERT_STORE=system \
  /usr/local/bin/claude "$@"
