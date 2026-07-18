#!/usr/bin/env bash
set -euo pipefail
cd /mnt/trustyclaw-agent/agent-home

# This launcher is authoritative for translating the operator's web-search
# decision into Claude CLI flags. The caller states policy only; this script
# builds the enforcement. The decision is this script's REQUIRED first argument:
#
#   web-search=off -> append a --settings override that denies the WebSearch
#                     tool. This is the highest-precedence CLI settings layer,
#                     nothing is written to disk, and the agent cannot influence
#                     the launched command.
#   web-search=on  -> add nothing; the tool stays available to Claude.
#
# The orchestrator derives on/off from the operator toggle for agent turns (see
# host/runtime/admin_api/claude_code.py). Non-agent maintenance calls (auth, usage) run no
# model turn that could use the tool, so they pass web-search=off to keep the
# deny-by-default posture. The network proxy enforces the same operator toggle
# independently, so a mistake here cannot let web search past the proxy.
case "${1:-}" in
  web-search=on) web_search_settings=() ;;
  web-search=off) web_search_settings=(--settings '{"permissions":{"deny":["WebSearch"]}}') ;;
  *)
    echo "run-claude-code: first argument must be web-search=on or web-search=off" >&2
    exit 64
    ;;
esac
shift

# Claude Code 2.1.206 classifies the account-limit fetch behind `/usage` as
# nonessential traffic. Suppressing that traffic makes the command exit zero
# while omitting every usage window, which leaves the admin UI with no fresh
# snapshot. Keep telemetry/feedback/auto-update suppression for agent and auth
# processes, but let this one host-owned maintenance command fetch its data.
claude_environment=(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1)
if [ "${1:-}" = "-p" ] && [ "${2:-}" = "/usage" ]; then
  claude_environment=()
fi

# The transient scope puts the runtime and everything it spawns into the
# resource-limited trustyclaw_agent.slice instead of the admin API's service
# cgroup. systemd-run --scope runs the command as its own child, so the stdio
# pipes and the stdin-EOF shutdown path are unchanged. BindsTo restores the
# lifecycle coupling the cgroup move removed: when the admin API service
# stops, restarts, or crashes, systemd stops the scope too, so no orphaned
# runtime keeps mutating agent-home after its task was recovered as failed.
#
# A leading "--thread-scope <thread_id>" pair (consumed here, never passed to
# the CLI) names the scope trustyclaw-agent-thread-<thread_id>.scope. The
# agent-app service derives app ownership from the host-reserved app prefix in
# that kernel-owned scope name, so the name comes from this root helper and is
# validated as a host thread id.
unit_args=()
if [ "${1:-}" = "--thread-scope" ]; then
  if ! [[ "${2:-}" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
    echo "invalid --thread-scope thread id: ${2:-<missing>}" >&2
    exit 64
  fi
  unit_args=(--unit "trustyclaw-agent-thread-$2")
  shift 2
fi
exec systemd-run --quiet --collect --scope --slice=trustyclaw_agent.slice \
  "${unit_args[@]}" \
  --property=BindsTo=trustyclaw-admin-api.service \
  /usr/sbin/runuser -u trustyclaw-agent -- env \
  HOME=/mnt/trustyclaw-agent/agent-home \
  CLAUDE_CONFIG_DIR=/mnt/trustyclaw-agent/agent-home/.claude \
  HTTP_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  HTTPS_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  ALL_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  NO_PROXY=127.0.0.1,localhost \
  NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/trustyclaw-network-proxy.crt \
  CLAUDE_CODE_CERT_STORE=system \
  "${claude_environment[@]}" \
  /usr/local/bin/claude "${web_search_settings[@]}" "$@"
