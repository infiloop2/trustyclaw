"""The trustyclaw-tools service process.

Owns the agent-facing tools Unix socket TOOLS_SOCKET_PATH (api.py); callers
are authenticated by kernel peer credentials. tools_host.py executes the
bundled tool packages inside this egress-capable service user.
"""
