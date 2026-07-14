"""Constants shared across deploy, the runtime services, and the smoke tests.

Defined once here so a port or the loopback address cannot drift between the
proxy, the admin API, the bootstrap templates, and the smoke harness.
"""

from __future__ import annotations

LOOPBACK = "127.0.0.1"
ADMIN_API_PORT = 7443
# Request/response body cap shared by the admin API surfaces and the app
# backend proxy hop.
MAX_REQUEST_BODY_BYTES = 1024 * 1024
PROXY_PORT = 7445
APP_PORT_BASE = 7450
