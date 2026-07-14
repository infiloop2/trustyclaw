"""Dedicated tools service.

Tool packages make outbound HTTPS calls to third parties (Google, Brave) and
parse their responses, so they need internet egress and are the host code most
exposed to attacker-influenced data. This service runs them out of the admin
service: it runs as the dedicated ``trustyclaw-tools`` user, which is the only
non-root uid that executes tool packages with direct DNS and HTTPS, and
connects to Postgres as the ``trustyclaw-tools`` role. That role is limited to
the tool tables plus read access to the encryption key used for tool secrets.
The admin service therefore holds no internet egress and executes no
third-party tool action.

It serves the agent-facing tools socket (``tools/list`` and ``tools/call`` for
the MCP shim) and the operator delegation routes the admin service forwards for
the operations that need this service's egress (OAuth code exchange, token
revoke) or run tool code over third-party data (approved-action execution). All
tool credentials, config, approvals, and audit events live in the tool tables,
reached with the scoped role over the same peer-authenticated Postgres socket.
"""

from __future__ import annotations

from host.runtime import tools_api, tools_host


def main() -> int:
    # This service executes approved actions, so it owns their crash recovery:
    # an approval stuck in 'approved' when this service last stopped had its
    # single execute_approved call interrupted, so mark it failed before serving
    # (an unknown outcome spends a single-use approval). Owning it here, rather
    # than in admin startup, avoids racing a live execution when only the admin
    # service restarts.
    tools_host.recover_interrupted_approvals()
    tools_api.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
