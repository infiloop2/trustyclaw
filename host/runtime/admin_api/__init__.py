"""The trustyclaw-admin service process.

Owns two message surfaces and nothing else touches them:
- the operator TCP API on 127.0.0.1:ADMIN_API_PORT (service.py), and
- the app-backend Unix socket APP_BACKEND_ADMIN_SOCKET_PATH (app_backend_api.py).
The other modules here (orchestrator, agent CLI adapters, GitHub credential
and audit flows, upgrade polling) run in-process behind those surfaces; the
tools socket is reached only as a client through tools_client.py.
"""
