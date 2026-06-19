"""Constants shared across deploy, the runtime services, and the smoke tests.

Defined once here so a port or the loopback address cannot drift between the
proxy, the admin API, the bootstrap templates, and the smoke harness.
"""

from __future__ import annotations

LOOPBACK = "127.0.0.1"
ADMIN_API_PORT = 7443
PROXY_PORT = 7445
