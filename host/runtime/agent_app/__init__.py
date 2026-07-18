"""The trustyclaw-agent-app service process.

Owns the agent app_api Unix socket AGENT_APP_SOCKET_PATH (api.py) and proxies
authorized calls to per-app loopback backend ports; app ownership derives
from the caller's kernel-attributed thread scope.
"""
