#!/usr/bin/env bash
set -euo pipefail
cd /mnt/trustyclaw-agent/agent-home
# The transient scope puts the runtime and everything it spawns into the
# resource-limited trustyclaw_agent.slice instead of the admin API's service
# cgroup. systemd-run --scope runs the command as its own child, so the stdio
# pipes and the stdin-EOF shutdown path are unchanged. BindsTo restores the
# lifecycle coupling the cgroup move removed: when the admin API service
# stops, restarts, or crashes, systemd stops the scope too, so no orphaned
# runtime keeps mutating agent-home after its task was recovered as failed.
exec systemd-run --quiet --collect --scope --slice=trustyclaw_agent.slice \
  --property=BindsTo=trustyclaw-admin-api.service \
  /usr/sbin/runuser -u trustyclaw-agent -- env \
  HOME=/mnt/trustyclaw-agent/agent-home \
  HTTP_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  HTTPS_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  ALL_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  NO_PROXY=127.0.0.1,localhost \
  NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/trustyclaw-network-proxy.crt \
  /usr/local/bin/codex app-server --listen stdio://
