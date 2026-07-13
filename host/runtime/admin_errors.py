"""Shared admin API error type.

``ApiError`` lives here, not in ``admin_api``, so it is a single class no matter
how ``admin_api`` is loaded. The admin service runs as ``python3 -m
host.runtime.admin_api`` (module name ``__main__``), while modules it dispatches
to -- ``tools_admin_api``, ``app_backend_admin_api`` -- reach it with ``from
host.runtime import admin_api`` (module name ``host.runtime.admin_api``), which is
a second instance. If each defined its own ``ApiError``, an ``ApiError`` raised in
one and caught by the other's ``except ApiError`` would not match, and the request
would fall through to a generic 500. Importing the class from here gives every
loader the same type.
"""

from __future__ import annotations

from http import HTTPStatus


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
