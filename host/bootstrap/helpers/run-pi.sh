#!/usr/bin/env bash
set -euo pipefail
cd /mnt/trustyclaw-agent/agent-home

# This launcher is authoritative for pinning the Pi harness surface. The
# caller states policy only; this script builds the enforcement:
#
#   region=<aws-region> (REQUIRED first argument) -> exported as AWS_REGION so
#       Pi's AWS SDK signs for and calls exactly the operator-configured
#       Bedrock region. The network proxy separately enforces that shared
#       region, so a mistake here cannot reach another region.
#
#   Fixed flags -> the provider is always amazon-bedrock, and extension,
#       skill, prompt-template, and theme discovery are disabled
#       (--no-extensions honors explicit -e paths), so the only tools are
#       Pi's built-in read/bash/edit/write plus the bundled tools that the
#       root-owned pi_tools_bridge.js extension registers from the MCP shim,
#       and the only provider is the guarded one. AGENTS.md context files
#       still load (the host installs them root-owned and immutable).
#
# The orchestrator reads the operator's region from policy state for agent
# turns (see host/runtime/admin_api/pi_agent.py).
case "${1:-}" in
  region=*)
    region="${1#region=}"
    if ! [[ "${region}" =~ ^[a-z]{2}-[a-z]+-[0-9]$ ]]; then
      echo "run-pi: invalid region: ${region}" >&2
      exit 64
    fi
    ;;
  *)
    echo "run-pi: first argument must be region=<aws-region>" >&2
    exit 64
    ;;
esac
shift

# The transient scope puts the runtime and everything it spawns into the
# resource-limited trustyclaw_agent.slice; BindsTo couples it to the admin API
# service lifecycle (see run-claude-code.sh for the full rationale). A leading
# "--thread-scope <thread_id>" pair names the scope after the host thread so
# the agent-app service can derive ownership from the kernel-owned cgroup name.
unit_args=()
if [ "${1:-}" = "--thread-scope" ]; then
  if ! [[ "${2:-}" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
    echo "invalid --thread-scope thread id: ${2:-<missing>}" >&2
    exit 64
  fi
  unit_args=(--unit "trustyclaw-agent-thread-$2")
  shift 2
fi
# The agent process NEVER holds the operator's AWS credential. Pi signs its
# Bedrock requests with fixed dummy AWS values that carry no provider
# capability; the network proxy checks the request boundary and re-signs each
# allowed request with the operator's real key, which lives encrypted in the
# database and is readable only by the admin and proxy roles. The access key
# id is Pi's own routing identity (host/network_integrations/bedrock/
# manifest.py), so the proxy attributes each request's reported token usage
# to the Pi runtime.
# PI_OFFLINE disables Pi's startup update and catalog fetches (the proxy would
# deny them; the pinned package ships its model catalog). IMDS is disabled so
# the AWS SDK never waits on the unreachable instance-metadata endpoint before
# reading the injected signing identity.
export AWS_ACCESS_KEY_ID="AKIATRUSTYCLAWPIBDRK"
export AWS_SECRET_ACCESS_KEY="trustyclaw-bedrock-dummy-secret"
exec systemd-run --quiet --collect --scope --slice=trustyclaw_agent.slice \
  "${unit_args[@]}" \
  --property=BindsTo=trustyclaw-admin-api.service \
  /usr/sbin/runuser -u trustyclaw-agent -- env \
  HOME=/mnt/trustyclaw-agent/agent-home \
  HTTP_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  HTTPS_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  ALL_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  NO_PROXY=127.0.0.1,localhost \
  NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/trustyclaw-network-proxy.crt \
  AWS_REGION="${region}" \
  AWS_EC2_METADATA_DISABLED=true \
  PI_OFFLINE=1 \
  /usr/local/bin/pi --provider amazon-bedrock \
  --no-extensions --no-skills --no-prompt-templates --no-themes \
  --extension /opt/trustyclaw-host/host/runtime/agent_shim/pi_tools_bridge.js \
  "$@"
