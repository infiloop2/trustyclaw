"""Browser-facing admin API reverse proxy for installed app backends.

App UI frames cannot call host admin routes directly. They post requests to the
admin shell, which sends those requests to ``/v1/apps/<app_id>/api/...`` with an
app bridge marker. This module owns that route surface and the loopback HTTP
hop to the app backend service.
"""

from __future__ import annotations

import http.client
from http import HTTPStatus
import json
from typing import Any
from urllib.parse import urlencode

from host.constants import LOOPBACK, MAX_REQUEST_BODY_BYTES
from host.runtime import app_platform
from host.runtime.admin_errors import ApiError


APP_API_PROXY_TIMEOUT_SECONDS = 10


def route_app_request(
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: Any,
) -> Any:
    # Bridge-tagged requests were already scoped to this app's API prefix by
    # admin_api.route() before dispatch reaches here.
    parts = path.strip("/").split("/")
    if len(parts) >= 4 and parts[0] == "v1" and parts[1] == "apps" and parts[3] == "api":
        app = app_platform.app_by_id(parts[2])
        if app is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "app not found")
        suffix = "/" + "/".join(parts[4:]) if len(parts) > 4 else "/"
        return proxy_app_api(app, method, suffix, query, body)
    raise ApiError(HTTPStatus.NOT_FOUND, "app route not found")


def proxy_app_api(
    app: app_platform.AppManifest,
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: Any,
) -> Any:
    encoded_body = None if body is None else json.dumps(body, sort_keys=True).encode()
    headers = {"X-TrustyClaw-App-Proxy": app.id}
    if encoded_body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(encoded_body))
    target = path
    if query:
        target += "?" + urlencode([(key, value) for key, values in query.items() for value in values])
    conn: http.client.HTTPConnection | None = None
    try:
        conn = http.client.HTTPConnection(LOOPBACK, app.port, timeout=APP_API_PROXY_TIMEOUT_SECONDS)
        conn.request(method, target, body=encoded_body, headers=headers)
        response = conn.getresponse()
        raw = response.read(MAX_REQUEST_BODY_BYTES + 1)
    except OSError as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"app backend unavailable: {app.id}") from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if len(raw) > MAX_REQUEST_BODY_BYTES:
        raise ApiError(HTTPStatus.BAD_GATEWAY, "app backend response too large")
    try:
        data = json.loads(raw.decode() or "{}")
    except json.JSONDecodeError as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, "app backend returned invalid JSON") from exc
    if response.status >= 400:
        message = data.get("error", {}).get("message") if isinstance(data, dict) else None
        raise ApiError(HTTPStatus(response.status), message or "app backend request failed")
    return data
