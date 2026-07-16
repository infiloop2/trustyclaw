"""Dedicated agent-app service.

Serves the agent-facing app API socket (``POST /call`` for the MCP shim) as
the dedicated ``trustyclaw-agent-app`` user: the one uid
besides the admin service that nftables allows to open new connections to app
backend ports. It has no database access or internet egress. Keeping this out
of the admin service means the agent-facing socket surface adds nothing to the
admin plane, and keeping it out of the tools service keeps app traffic away
from the only uid with internet egress. See ``agent_app_api`` for the
attribution and proxy model.
"""

from __future__ import annotations

from host.runtime import agent_app_api


def main() -> int:
    agent_app_api.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
