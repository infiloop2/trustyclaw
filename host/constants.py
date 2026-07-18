"""Constants shared across deploy, the runtime services, and the smoke tests.

Defined once here so a port or the loopback address cannot drift between the
proxy, the admin API, the bootstrap templates, and the smoke harness.
"""

from __future__ import annotations

LOOPBACK = "127.0.0.1"
# The public TrustyClaw repository: the source the GitHub provisioning
# delivery pins commits from.
PUBLIC_GITHUB_REPOSITORY = "infiloop2/trustyclaw"
# The one fixed environment variable deploy and reconfigure read the
# Cloudflare Tunnel token from when a Cloudflare operator endpoint is
# configured. Secrets never ride in CLI arguments.
OPERATOR_TUNNEL_TOKEN_ENV_NAME = "TRUSTYCLAW_CLOUDFLARE_TUNNEL_TOKEN"
ADMIN_API_PORT = 7443
# Request/response body cap shared by the admin API surfaces and the app
# backend proxy hop.
MAX_REQUEST_BODY_BYTES = 1024 * 1024
PROXY_PORT = 7445
APP_PORT_BASE = 7450

# Unix socket endpoints. Each socket is served by exactly one runtime service
# package (see host/runtime/__init__.py); the default paths live here so the
# server, its clients, and the deploy verifier cannot drift apart. Servers and
# clients honor the matching TRUSTYCLAW_*_SOCKET environment override in tests.
TOOLS_SOCKET_PATH = "/run/trustyclaw-tools/tools.sock"
AGENT_APP_SOCKET_PATH = "/run/trustyclaw-agent-app/agent-app.sock"
AGENT_NETWORK_SOCKET_PATH = "/run/trustyclaw-agent-network/agent-network.sock"
APP_BACKEND_ADMIN_SOCKET_PATH = "/run/trustyclaw-admin-api/app-backend.sock"

# Core service accounts with pinned uids (gid always equals uid) so durable
# EBS file owners stay meaningful across root-volume replacement. Bootstrap
# renders these into the provisioning script (deploy fails if a base image
# already allocated one of the ids) and host.bootstrap.verify_deploy asserts
# the live /etc/passwd matches after provisioning. App accounts derive
# separately from each app's host_slot (see host.runtime.core.app_platform).
SERVICE_ACCOUNTS = {
    "trustyclaw-admin": 47741,
    "trustyclaw-proxy": 47742,
    "trustyclaw-agent": 47743,
    "cloudflared": 47744,
    "postgres": 47745,
    "trustyclaw-tools": 47746,
    "trustyclaw-agent-app": 47747,
    "trustyclaw-agent-network": 47748,
}
