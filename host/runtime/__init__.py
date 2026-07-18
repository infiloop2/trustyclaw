"""TrustyClaw runtime installed on EC2, split one package per service process.

The boundary rule: every message surface (TCP port or Unix socket) is served
by exactly one package, and that package is the only code that parses
messages arriving on it. Shared libraries live in ``core`` and serve no
socket.

- ``admin_api``     trustyclaw-admin: operator TCP API + app-backend socket
- ``network_proxy`` trustyclaw-proxy: agent egress proxy (PROXY_PORT)
- ``tools``         trustyclaw-tools: agent-facing tools socket
- ``agent_network`` trustyclaw-agent-network: read-only introspection socket
- ``agent_app``     trustyclaw-agent-app: agent app_api socket
- ``agent_shim``    trustyclaw-agent: MCP stdio shim, client-side only
- ``core``          shared storage/state/policy libraries, no socket
- ``deploy``        bootstrap-run CLIs (migrations, effective config)
- ``root_helpers``  standalone CLIs invoked as root via sudo helpers

Socket paths and ports live in ``host.constants`` so servers, clients, and
the deploy verifier share one definition.
"""
