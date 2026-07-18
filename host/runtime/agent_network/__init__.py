"""The trustyclaw-agent-network service process.

Owns the read-only network introspection Unix socket
AGENT_NETWORK_SOCKET_PATH (api.py). No egress, no filesystem state; its
database role reads only policy and network-event tables.
"""
