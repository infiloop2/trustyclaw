"""Agent app API tests: attribution, the Unix-socket service, and the shim.

The service runs in-process with an injected thread resolver (tests cannot run
inside real systemd thread scopes; the cgroup parser is covered directly
against sample cgroup content). The app backend is
a real loopback HTTP stub on the synthetic app's derived port, and the MCP
shim runs as a real subprocess, exactly as the agent harnesses launch it.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from host.constants import LOOPBACK
from host.runtime import agent_app_api, app_platform
from host.runtime.tools_mcp_shim import UnixHTTPConnection

REPO_ROOT = Path(__file__).resolve().parents[1]
# A high slot keeps the stub backend's derived port away from the bundled
# apps' and other suites' ports.
STUB_SLOT = 91
STUB_PORT = 7450 + STUB_SLOT


def write_app_package(root: Path, app_id: str, *, host_slot: int, agent_api: bool) -> None:
    app_dir = root / app_id
    app_dir.mkdir()
    (app_dir / "backend.py").write_text("")
    (app_dir / "migrations").mkdir()
    (app_dir / "ui").mkdir()
    manifest: dict[str, object] = {
        "host_slot": host_slot,
        "title": f"{app_id.title()} App",
        "backend": {"entrypoint": "backend.py"},
        "database": {"migrations": "migrations"},
        "ui": {"path": "ui"},
    }
    (app_dir / "agent.md").write_text(f"Instructions for {app_id}.")
    manifest["agent"] = {"instructions": "agent.md", "api": agent_api}
    (app_dir / "manifest.json").write_text(json.dumps(manifest))


class StubAppBackend(ThreadingHTTPServer):
    """Records every request the proxy forwards and echoes a canned reply."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.response_status = 200
        self.response_body: object = {"ok": True}
        super().__init__((LOOPBACK, STUB_PORT), _StubHandler)


class _StubHandler(BaseHTTPRequestHandler):
    server: StubAppBackend

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {key: value for key, value in self.headers.items()},
                "body": json.loads(raw) if raw else None,
            }
        )
        payload = json.dumps(self.server.response_body).encode()
        self.send_response(self.server.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


class ThreadScopeParsingTests(unittest.TestCase):
    def test_matches_only_thread_scopes_under_the_agent_slice(self) -> None:
        inside = "0::/trustyclaw_agent.slice/trustyclaw-agent-thread-workbench__ws-1.scope\n"
        match = agent_app_api._THREAD_SCOPE_RE.search(inside)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "workbench__ws-1")
        # A scope the agent could mint through a user manager lives under
        # user.slice, not trustyclaw_agent.slice, and must not attribute.
        forged = "0::/user.slice/user-47743.slice/user@47743.service/trustyclaw-agent-thread-workbench__ws-1.scope\n"
        self.assertIsNone(agent_app_api._THREAD_SCOPE_RE.search(forged))
        forged_nested_slice = (
            "0::/user.slice/user-47743.slice/user@47743.service/"
            "trustyclaw_agent.slice/trustyclaw-agent-thread-workbench__ws-1.scope\n"
        )
        self.assertIsNone(agent_app_api._THREAD_SCOPE_RE.search(forged_nested_slice))
        # Non-thread scopes in the agent slice (status probes, logins) carry
        # systemd's generated names and must not attribute either.
        probe = "0::/trustyclaw_agent.slice/run-r3f5a.scope\n"
        self.assertIsNone(agent_app_api._THREAD_SCOPE_RE.search(probe))
        # Nested cgroups created inside the scope still attribute to it.
        nested = "0::/trustyclaw_agent.slice/trustyclaw-agent-thread-agent_chat__chat.scope/child\n"
        nested_match = agent_app_api._THREAD_SCOPE_RE.search(nested)
        self.assertIsNotNone(nested_match)
        self.assertEqual(nested_match.group(1), "agent_chat__chat")

    def test_this_test_process_is_not_attributable(self) -> None:
        # The full pidfd path against a real pid: this process is alive but
        # runs outside any host thread scope, so attribution fails closed.
        with self.assertRaisesRegex(agent_app_api.AttributionError, "not inside a host thread scope"):
            agent_app_api.thread_id_for_pid(os.getpid())

    def test_a_dead_pid_is_not_attributable(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()
        with self.assertRaises(agent_app_api.AttributionError):
            agent_app_api.thread_id_for_pid(proc.pid)


class AgentAppApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.apps_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.apps_dir.cleanup)
        root = Path(self.apps_dir.name)
        write_app_package(root, "workbench", host_slot=STUB_SLOT, agent_api=True)
        write_app_package(root, "plainapp", host_slot=STUB_SLOT + 1, agent_api=False)
        app_root_patch = patch.object(app_platform, "APP_ROOT", root)
        app_root_patch.start()
        self.addCleanup(app_root_patch.stop)

class ResolveContextTests(AgentAppApiTestCase):
    def test_app_scoped_thread_resolves_to_its_app(self) -> None:
        context = agent_app_api.resolve_context("workbench__ws-1")
        self.assertEqual(context["app_id"], "workbench")
        self.assertEqual(context["thread_id"], "ws-1")
        self.assertEqual(context["port"], STUB_PORT)

    def test_operator_thread_fails_closed(self) -> None:
        with self.assertRaisesRegex(agent_app_api.AttributionError, "not app-scoped"):
            agent_app_api.resolve_context("chat")

    def test_app_without_agent_api_fails_closed(self) -> None:
        with self.assertRaisesRegex(agent_app_api.AttributionError, "does not offer"):
            agent_app_api.resolve_context("plainapp__chat")
        with self.assertRaises(agent_app_api.AttributionError):
            agent_app_api.resolve_context("goneapp__chat")


class AgentAppSocketTests(AgentAppApiTestCase):
    def start_server(
        self,
        agent_uids: frozenset[int] | None = None,
        thread_resolver=None,
    ) -> str:
        socket_dir = tempfile.TemporaryDirectory()
        self.addCleanup(socket_dir.cleanup)
        socket_path = str(Path(socket_dir.name) / "agent-app.sock")
        server = agent_app_api.AgentAppServer(
            socket_path,
            agent_uids if agent_uids is not None else frozenset({os.getuid()}),
            thread_resolver if thread_resolver is not None else (lambda pid: "workbench__ws-1"),
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return socket_path

    def start_stub_backend(self) -> StubAppBackend:
        stub = StubAppBackend()
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)
        return stub

    def http(self, socket_path: str, method: str, path: str, body: dict | None = None):
        connection = UnixHTTPConnection(socket_path)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            try:
                connection.request(method, path, body=payload)
            except BrokenPipeError:
                # Early responses close the connection before the body is fully
                # sent: the pre-body 429 on the call cap races the client's body
                # write. The response already delivered on the AF_UNIX socket
                # stays readable, so fall through and read it.
                pass
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_context_route_does_not_exist(self) -> None:
        socket_path = self.start_server()
        status, body = self.http(socket_path, "GET", "/context")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "Unknown path."})

    def test_rejects_peers_outside_the_agent_uid(self) -> None:
        socket_path = self.start_server(agent_uids=frozenset({os.getuid() + 1}))
        for method, path in (("GET", "/context"), ("POST", "/call")):
            status, body = self.http(socket_path, method, path, {} if method == "POST" else None)
            self.assertEqual(status, 403)
            self.assertIn("Peer not allowed", body["error"])

    def test_call_proxies_to_the_owning_app_with_trusted_markers(self) -> None:
        stub = self.start_stub_backend()
        stub.response_body = {"artifact": {"id": "tracker"}}
        socket_path = self.start_server()
        status, body = self.http(
            socket_path,
            "POST",
            "/call",
            {"method": "POST", "path": "/agent/artifacts", "body": {"title": "Tracker"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": 200, "body": {"artifact": {"id": "tracker"}}})
        (request,) = stub.requests
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/agent/artifacts")
        self.assertEqual(request["body"], {"title": "Tracker"})
        headers = request["headers"]
        self.assertEqual(headers["X-TrustyClaw-Agent-App-Proxy"], "workbench")
        self.assertEqual(headers["X-TrustyClaw-Agent-Thread"], "ws-1")

    def test_app_error_statuses_pass_through_for_in_turn_retry(self) -> None:
        stub = self.start_stub_backend()
        stub.response_status = 422
        stub.response_body = {"error": "title is required"}
        socket_path = self.start_server()
        status, body = self.http(
            socket_path, "POST", "/call", {"method": "POST", "path": "/agent/artifacts", "body": {}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": 422, "body": {"error": "title is required"}})

    def test_call_requires_the_agent_route_namespace(self) -> None:
        stub = self.start_stub_backend()
        socket_path = self.start_server()
        for method, path in (
            ("POST", "/threads"),
            ("GET", "/agent/../threads"),
            ("GET", "agent/x"),
            ("GET", "/agent/x?q=<script>"),
            ("TRACE", "/agent/x"),
        ):
            status, body = self.http(socket_path, "POST", "/call", {"method": method, "path": path})
            self.assertEqual(status, 400, f"{method} {path}")
        self.assertEqual(stub.requests, [])

    def test_unreachable_backend_is_a_502(self) -> None:
        # No stub backend is bound.
        socket_path = self.start_server()
        status, body = self.http(socket_path, "POST", "/call", {"method": "GET", "path": "/agent/x"})
        self.assertEqual(status, 502)
        self.assertIn("unavailable", body["error"])

    def test_call_from_operator_thread_is_404_and_reaches_no_backend(self) -> None:
        stub = self.start_stub_backend()
        socket_path = self.start_server(thread_resolver=lambda pid: "chat")
        status, _ = self.http(socket_path, "POST", "/call", {"method": "GET", "path": "/agent/x"})
        self.assertEqual(status, 404)
        self.assertEqual(stub.requests, [])

    def test_oversized_backend_response_is_refused_not_relayed(self) -> None:
        # The 1 MiB response cap is a host-enforced boundary against a
        # misbehaving app ballooning agent turns; the backend is reached but its
        # oversized reply is refused rather than passed back to the agent.
        stub = self.start_stub_backend()
        stub.response_body = {"blob": "y" * (agent_app_api.MAX_RESPONSE_BODY_BYTES + 1024)}
        socket_path = self.start_server()
        status, body = self.http(socket_path, "POST", "/call", {"method": "GET", "path": "/agent/x"})
        self.assertEqual(status, 502)
        self.assertIn("unavailable", body["error"])
        self.assertEqual(len(stub.requests), 1)

    def test_oversized_request_body_is_rejected_before_forwarding(self) -> None:
        # The 256 KiB request cap is enforced at the socket, before any backend
        # connection is opened.
        stub = self.start_stub_backend()
        socket_path = self.start_server()
        oversized = {
            "method": "POST",
            "path": "/agent/x",
            "body": {"blob": "y" * (agent_app_api.MAX_REQUEST_BODY_BYTES + 1024)},
        }
        status, body = self.http(socket_path, "POST", "/call", oversized)
        self.assertEqual(status, 413)
        self.assertEqual(stub.requests, [])

    def test_global_concurrency_cap_returns_429(self) -> None:
        socket_path = self.start_server()
        for _ in range(agent_app_api.MAX_CONCURRENT_CALLS):
            self.assertTrue(agent_app_api._CALL_SLOTS.acquire(blocking=False))
        try:
            status, _ = self.http(socket_path, "POST", "/call", {"method": "GET", "path": "/agent/x"})
        finally:
            for _ in range(agent_app_api.MAX_CONCURRENT_CALLS):
                agent_app_api._CALL_SLOTS.release()
        self.assertEqual(status, 429)


class McpShimAppApiTests(AgentAppApiTestCase):
    def start_shim(self, agent_app_socket: str, tools_socket: str | None = None) -> subprocess.Popen[str]:
        env = os.environ.copy()
        env["TRUSTYCLAW_AGENT_APP_SOCKET"] = agent_app_socket
        env["TRUSTYCLAW_TOOLS_SOCKET"] = tools_socket or str(Path(agent_app_socket).parent / "no-tools.sock")
        env["PYTHONPATH"] = str(REPO_ROOT)
        shim = subprocess.Popen(
            [sys.executable, "-m", "host.runtime.tools_mcp_shim"],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(shim.stdout.close)
        self.addCleanup(shim.wait)
        self.addCleanup(shim.stdin.close)
        return shim

    def rpc(self, shim: subprocess.Popen[str], message: dict) -> dict:
        shim.stdin.write(json.dumps(message) + "\n")
        shim.stdin.flush()
        line = shim.stdout.readline()
        self.assertTrue(line, "shim closed stdout unexpectedly")
        return json.loads(line)

    def start_server(self) -> str:
        socket_dir = tempfile.TemporaryDirectory()
        self.addCleanup(socket_dir.cleanup)
        socket_path = str(Path(socket_dir.name) / "agent-app.sock")
        server = agent_app_api.AgentAppServer(
            socket_path, frozenset({os.getuid()}), lambda pid: "workbench__ws-1"
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return socket_path

    def test_shim_always_lists_app_api_with_stable_description(self) -> None:
        missing_socket = str(Path(self.apps_dir.name) / "missing.sock")
        shim = self.start_shim(missing_socket)
        listing = self.rpc(shim, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {tool["name"]: tool for tool in listing["result"]["tools"]}
        self.assertIn("app_api", tools)
        self.assertIn("installed app backend associated with this app-created thread", tools["app_api"]["description"])
        self.assertNotIn("Workbench App", tools["app_api"]["description"])
        self.assertIn("documented by the current app instructions", tools["app_api"]["description"])
        self.assertEqual(tools["app_api"]["inputSchema"]["required"], ["method", "path"])

    def test_shim_listing_is_unchanged_without_app_attribution(self) -> None:
        socket_path = self.start_server()
        shim = self.start_shim(socket_path)
        listing = self.rpc(shim, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([tool["name"] for tool in listing["result"]["tools"]], ["app_api"])

    def test_shim_forwards_app_api_calls_and_surfaces_rejections(self) -> None:
        stub = StubAppBackend()
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)
        socket_path = self.start_server()
        shim = self.start_shim(socket_path)
        result = self.rpc(
            shim,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "app_api",
                    "arguments": {"method": "GET", "path": "/agent/state"},
                },
            },
        )["result"]
        self.assertFalse(result.get("isError", False))
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload, {"status": 200, "body": {"ok": True}})

        rejected = self.rpc(
            shim,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "app_api", "arguments": {"method": "GET", "path": "/threads"}},
            },
        )["result"]
        self.assertTrue(rejected["isError"])
        self.assertIn("path must match", rejected["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
