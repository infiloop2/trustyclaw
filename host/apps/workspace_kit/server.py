"""HTTP surface and process entrypoint for a workspace_kit app.

The ``Handler`` authenticates the two host proxies (operator UI and agent app
API) and dispatches to the engine's routers. ``serve(config)`` binds the engine
to the app's config, starts the single run-worker thread, and serves the
threading HTTP server on the app's loopback port.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

from host.apps.workspace_kit import engine
from host.apps.workspace_kit.config import WorkspaceAppConfig


LOGGER = logging.getLogger(__name__)


class Handler(BaseHTTPRequestHandler):
    server_version = "TrustyClawWorkspaceKit/0.1"

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            body = self._read_body()
            if parsed.path == "/agent" or parsed.path.startswith("/agent/"):
                # Agent calls arrive only through the host's agent-app proxy,
                # which derives the app and thread from kernel-owned scope
                # state. The marker below is host-asserted, never
                # agent-claimed (docs/architecture/apps/agent-app-api.md).
                self._require_agent_proxy()
                response = engine.route_agent_request(
                    method,
                    parsed.path,
                    body,
                    thread_id=self.headers.get("X-TrustyClaw-Agent-Thread", ""),
                )
            else:
                self._require_host_proxy()
                response = engine.route_ui_request(method, parsed.path, body, parse_qs(parsed.query))
            self._send_json(HTTPStatus.OK, response)
        except engine.AppError as exc:
            self._send_json(exc.status, {"error": {"message": exc.message}})
        except Exception:
            LOGGER.exception("unexpected workspace app request failure")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {"message": "internal server error"}},
            )

    def _require_host_proxy(self) -> None:
        if self.headers.get("X-TrustyClaw-App-Proxy") != engine.APP_ID:
            raise engine.AppError(HTTPStatus.UNAUTHORIZED, "missing host app proxy marker")

    def _require_agent_proxy(self) -> None:
        if self.headers.get("X-TrustyClaw-Agent-App-Proxy") != engine.APP_ID:
            raise engine.AppError(HTTPStatus.UNAUTHORIZED, "missing host agent proxy marker")

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise engine.AppError(HTTPStatus.BAD_REQUEST, "malformed Content-Length") from exc
        if length < 0:
            raise engine.AppError(HTTPStatus.BAD_REQUEST, "malformed Content-Length")
        if length > engine.MAX_REQUEST_BODY_BYTES:
            raise engine.AppError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        if length == 0:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise engine.AppError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}") from exc

    def _send_json(self, status: HTTPStatus, body: Any) -> None:
        data = json.dumps(body, sort_keys=True).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(data)


def serve(config: WorkspaceAppConfig) -> int:
    """Bind the engine to config, start the run worker, and serve forever."""
    engine.configure(config)
    threading.Thread(
        target=engine.run_worker_loop, name=f"{config.app_id}-run-worker", daemon=True
    ).start()
    ThreadingHTTPServer((config.host, config.port), Handler).serve_forever()
    return 0
