#!/usr/bin/env bash
set -euo pipefail
exec /usr/sbin/runuser -u trustyclaw-agent -- \
  env HOME=/mnt/trustyclaw-agent/agent-home \
  /usr/bin/python3 /opt/trustyclaw-host/host/runtime/root_helpers/upload_agent_file.py "$@"
