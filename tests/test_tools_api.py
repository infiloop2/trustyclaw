"""Agent tools surface tests: the Unix-socket service and the MCP stdio shim.

The service runs in-process against the scratch database; the shim runs as a
real subprocess speaking newline-delimited JSON-RPC, exactly as the agent
harnesses launch it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pg_harness
from test_tools_host import FakeTool

from host.runtime import state, tools_api, tools_host
from host.runtime.tools_mcp_shim import UnixHTTPConnection

REPO_ROOT = Path(__file__).resolve().parents[1]


class ToolsApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        registry_patch = patch.dict(tools_host.BUNDLED_TOOLS, {"fake_notes": FakeTool()})
        registry_patch.start()
        self.addCleanup(registry_patch.stop)
        with state.mutation() as cur:
            state.save_tool_config_value(cur, "fake_notes", "FAKE_NOTES_TOKEN", "token-1")
            state.set_tool_enabled(cur, "fake_notes", True)


class ActionListingTests(ToolsApiTestCase):
    def test_lists_only_enabled_tools_plus_the_synthetic_tools(self) -> None:
        listing = tools_api.action_listing()
        names = [entry["name"] for entry in listing]
        self.assertEqual(names, [
            "fake_notes_read_note",
            "fake_notes_crash_note",
            "fake_notes_write_note",
            "list_bundled_tools",
            "check_tool_approval",
        ])
        read_note = listing[0]
        self.assertIn("Fake Notes", read_note["description"])
        self.assertEqual(read_note["input_schema"]["type"], "object")

    def test_exposes_every_bundled_action_contract_as_agent_context(self) -> None:
        with patch.object(state, "enabled_tool_ids", return_value=set(tools_host.BUNDLED_TOOLS)):
            by_name = {entry["name"]: entry for entry in tools_api.action_listing()}

        for tool_id, tool in tools_host.BUNDLED_TOOLS.items():
            for action in tool.manifest.actions:
                with self.subTest(tool_id=tool_id, action=action.id):
                    listed = by_name[f"{tool_id}_{action.id}"]
                    self.assertEqual(
                        listed["description"],
                        f"{tool.manifest.display_name}: {action.description}",
                    )
                    self.assertEqual(listed["input_schema"], action.input_schema)

        self.assertIn("individual tradable questions", by_name["polymarket_list_markets"]["description"])
        self.assertIn("umbrella topics", by_name["polymarket_list_events"]["description"])
        self.assertIn("not public-post", by_name["instagram_get_recent_media"]["description"])
        self.assertIn("not an objective global ranking", by_name["instagram_discovery_get_trending_reels"]["description"])
        self.assertIn("not a LinkedIn feed", by_name["linkedin_discovery_search_posts"]["description"])

    def test_list_bundled_tools_reports_the_catalog_with_enablement(self) -> None:
        # Enabled and disabled bundled tools both appear, distinguished by the
        # enabled flag, so the agent can ask the operator to enable an existing
        # tool instead of rebuilding it.
        result = tools_api.call_action("list_bundled_tools", {})
        self.assertEqual(result["status"], "executed")
        by_id = {entry["tool_id"]: entry for entry in result["result"]["tools"]}
        self.assertTrue(by_id["fake_notes"]["enabled"])
        self.assertEqual(by_id["fake_notes"]["action_ids"], ["read_note", "crash_note", "write_note"])
        gmail = by_id["gmail"]
        self.assertFalse(gmail["enabled"])
        self.assertEqual(gmail["connection"], "oauth")
        self.assertEqual(gmail["display_name"], "Gmail")
        self.assertIn("search_messages", gmail["action_ids"])

    def test_call_action_resolves_names_and_rejects_unknowns(self) -> None:
        result = tools_api.call_action("fake_notes_read_note", {})
        self.assertEqual(result["status"], "executed")
        with self.assertRaisesRegex(tools_host.ToolCallError, "Unknown tool"):
            tools_api.call_action("fake_notes_missing", {})
        with self.assertRaisesRegex(tools_host.ToolCallError, "not enabled"):
            tools_api.call_action("gmail_search_messages", {})  # resolvable but disabled
        with self.assertRaisesRegex(tools_host.ToolCallError, "must be a string"):
            tools_api.call_action(7, {})

    def test_check_tool_approval_reports_status(self) -> None:
        pending = tools_api.call_action("fake_notes_write_note", {"text": "hello"})
        self.assertEqual(pending["status"], "pending_approval")
        # The token is folded into the id; no separate field.
        self.assertNotIn("approval_check_token", pending)
        check_input = {"approval_id": pending["approval_id"]}
        checked = tools_api.call_action("check_tool_approval", check_input)
        self.assertEqual(checked["result"]["approval_status"], "pending")
        tools_host.decide_approval(pending["approval_id"], "approve")
        checked = tools_api.call_action("check_tool_approval", check_input)
        self.assertEqual(checked["result"]["approval_status"], "executed")
        self.assertEqual(checked["result"]["execution_result"], "Wrote the note (5 chars).")

        failed = tools_api.call_action("fake_notes_write_note", {"text": "fail"})
        tools_host.decide_approval(failed["approval_id"], "approve")
        checked = tools_api.call_action("check_tool_approval", {"approval_id": failed["approval_id"]})
        self.assertEqual(checked["result"]["approval_status"], "failed")
        self.assertEqual(checked["result"]["execution_result"], "Note write failed.")
        # A right-shaped id with the wrong token, and a guessed sequential
        # number, both fail closed.
        number = pending["approval_id"].split(".", 1)[0]
        with self.assertRaisesRegex(tools_host.ToolCallError, "Unknown approval"):
            tools_api.call_action("check_tool_approval", {"approval_id": number + ".wrong-token"})
        with self.assertRaisesRegex(tools_host.ToolCallError, "Unknown approval"):
            tools_api.call_action("check_tool_approval", {"approval_id": number})
        with self.assertRaisesRegex(tools_host.ToolCallError, "requires approval_id"):
            tools_api.call_action("check_tool_approval", {})


class ToolsSocketTests(ToolsApiTestCase):
    def start_server(
        self,
        agent_uids: frozenset[int] | None = None,
        admin_uids: frozenset[int] | None = None,
    ) -> str:
        socket_dir = tempfile.TemporaryDirectory()
        self.addCleanup(socket_dir.cleanup)
        socket_path = str(Path(socket_dir.name) / "tools.sock")
        server = tools_api.ToolsServer(
            socket_path,
            agent_uids if agent_uids is not None else frozenset({os.getuid()}),
            admin_uids,
        )
        import threading

        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return socket_path

    def http(self, socket_path: str, method: str, path: str, body: dict | None = None):
        connection = UnixHTTPConnection(socket_path)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            try:
                connection.request(method, path, body=payload)
            except BrokenPipeError:
                # Early responses close the connection before the body is fully
                # sent: the pre-body 429 on the agent call cap races the client's
                # body write. The response already delivered on the AF_UNIX
                # socket stays readable, so fall through and read it.
                pass
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def raw_http(self, socket_path: str, request: bytes) -> tuple[int, dict]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(socket_path)
            connection.sendall(request)
            response = b""
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                response += chunk
        header, _, body = response.partition(b"\r\n\r\n")
        status = int(header.split(None, 2)[1])
        return status, json.loads(body)

    def raw_json_http(
        self, socket_path: str, method: str, path: str, body: dict
    ) -> tuple[int, dict]:
        payload = json.dumps(body).encode()
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: local\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n"
        ).encode() + payload
        return self.raw_http(socket_path, request)

    def test_server_sweeps_expired_assets_once_per_hour(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = tools_api.ToolsServer(
                str(root / "tools.sock"),
                frozenset({os.getuid()}),
                asset_root=root / "assets",
            )
            self.addCleanup(server.server_close)
            server._next_asset_cleanup = 999.0
            with (
                patch("host.runtime.tools_api.time.monotonic", return_value=1_000.0),
                patch.object(server.asset_store, "cleanup_expired") as cleanup,
            ):
                server.service_actions()
                server.service_actions()

            cleanup.assert_called_once_with()
            self.assertEqual(
                server._next_asset_cleanup,
                1_000.0 + tools_api.ASSET_CLEANUP_INTERVAL_SECONDS,
            )

    def test_serves_listing_and_calls_over_the_socket(self) -> None:
        socket_path = self.start_server()
        status, body = self.http(socket_path, "GET", "/tools")
        self.assertEqual(status, 200)
        self.assertEqual(body["tools"][0]["name"], "fake_notes_read_note")

        status, body = self.http(socket_path, "POST", "/call", {"name": "fake_notes_read_note", "input": {}})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "executed")

        status, body = self.http(socket_path, "POST", "/call", {"name": "nope", "input": {}})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "failed")
        self.assertIn("Unknown tool", body["error"])

        status, body = self.http(socket_path, "GET", "/nope")
        self.assertEqual(status, 404)

    def test_rejects_peers_outside_the_allowed_uids(self) -> None:
        socket_path = self.start_server(agent_uids=frozenset({0}), admin_uids=frozenset({0}))
        status, body = self.http(socket_path, "GET", "/tools")
        self.assertEqual(status, 403)
        self.assertIn("Peer not allowed", body["error"])

    def test_peers_are_scoped_strictly_by_path(self) -> None:
        # The agent peer gets exactly the MCP surface: its uid is rejected on
        # the operator delegation routes.
        agent_only = self.start_server(
            agent_uids=frozenset({os.getuid()}), admin_uids=frozenset({0})
        )
        status, _ = self.http(agent_only, "GET", "/tools")
        self.assertEqual(status, 200)
        status, body = self.raw_http(
            agent_only,
            b"POST /operator/tools/fake_notes/oauth_connect/disconnect HTTP/1.1\r\n"
            b"Host: local\r\n"
            b"Content-Length: 0\r\n\r\n",
        )
        self.assertEqual(status, 403)
        self.assertIn("admin peer", body["error"])
        # The admin peer gets exactly the operator routes: its uid is rejected
        # on the agent MCP surface.
        admin_only = self.start_server(
            agent_uids=frozenset({0}), admin_uids=frozenset({os.getuid()})
        )
        status, body = self.http(admin_only, "GET", "/tools")
        self.assertEqual(status, 403)
        self.assertIn("Peer not allowed", body["error"])
        status, body = self.raw_http(
            admin_only,
            b"POST /call HTTP/1.1\r\n"
            b"Host: local\r\n"
            b"Content-Length: 0\r\n\r\n",
        )
        self.assertEqual(status, 403)
        self.assertIn("Peer not allowed", body["error"])
        status, _ = self.http(
            admin_only, "POST", "/operator/tools/fake_notes/oauth_connect/disconnect", {}
        )
        self.assertEqual(status, 200)

    def test_concurrency_cap_returns_429(self) -> None:
        socket_path = self.start_server()
        for _ in range(tools_api.MAX_CONCURRENT_CALLS):
            self.assertTrue(tools_api._CALL_SLOTS.acquire(blocking=False))
        try:
            # Send headers and body together. The server deliberately returns
            # 429 without reading the body; a split-write HTTP client can race
            # that early close and raise BrokenPipe before reading the response.
            status, body = self.raw_json_http(
                socket_path, "POST", "/call", {"name": "fake_notes_read_note", "input": {}}
            )
        finally:
            for _ in range(tools_api.MAX_CONCURRENT_CALLS):
                tools_api._CALL_SLOTS.release()
        self.assertEqual(status, 429)

    def test_concurrency_cap_applies_before_body_read(self) -> None:
        socket_path = self.start_server()
        for _ in range(tools_api.MAX_CONCURRENT_CALLS):
            self.assertTrue(tools_api._CALL_SLOTS.acquire(blocking=False))
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(1)
                connection.connect(socket_path)
                connection.sendall(
                    b"POST /call HTTP/1.1\r\n"
                    b"Host: local\r\n"
                    b"Content-Length: 1024\r\n\r\n"
                )
                response = connection.recv(65536)
        finally:
            for _ in range(tools_api.MAX_CONCURRENT_CALLS):
                tools_api._CALL_SLOTS.release()
        self.assertIn(b" 429 ", response)

    def test_operator_routes_bypass_the_agent_call_cap(self) -> None:
        # With every agent-call slot held, an agent /call is capped but an operator
        # route still runs, so a busy agent cannot 429 the operator's approve/deny/
        # connect/disconnect.
        socket_path = self.start_server()
        for _ in range(tools_api.MAX_CONCURRENT_CALLS):
            self.assertTrue(tools_api._CALL_SLOTS.acquire(blocking=False))
        try:
            call_status, _ = self.raw_json_http(
                socket_path, "POST", "/call", {"name": "x", "input": {}}
            )
            op_status, _ = self.http(
                socket_path, "POST", "/operator/tools/nope/oauth_connect/disconnect", {}
            )
        finally:
            for _ in range(tools_api.MAX_CONCURRENT_CALLS):
                tools_api._CALL_SLOTS.release()
        self.assertEqual(call_status, 429)
        # The operator route reached the handler (unknown tool -> 404), not the cap.
        self.assertEqual(op_status, 404)

    def test_stalled_request_is_closed_by_the_read_timeout(self) -> None:
        # A peer that connects and never finishes its request must not pin a
        # handler thread: the read timeout closes the connection instead.
        socket_path = self.start_server()
        with patch.object(tools_api.ToolsRequestHandler, "timeout", 0.3):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(socket_path)
                # Send a request line but never the blank line that ends headers.
                connection.sendall(b"GET /tools HTTP/1.1\r\n")
                # The server times out reading the rest and closes the socket,
                # so recv returns EOF well within our client-side timeout.
                self.assertEqual(connection.recv(65536), b"")

    def test_rejects_malformed_or_negative_content_length(self) -> None:
        socket_path = self.start_server()
        for length in (b"not-a-number", b"-1"):
            status, body = self.raw_http(
                socket_path,
                b"POST /call HTTP/1.1\r\n"
                b"Host: local\r\n"
                b"Content-Length: " + length + b"\r\n\r\n{}",
            )
            self.assertEqual(status, 400)
            self.assertIn("Content-Length", body["error"])


class McpShimTests(ToolsApiTestCase):
    def start_shim(self, socket_path: str) -> subprocess.Popen[str]:
        env = os.environ.copy()
        env["TRUSTYCLAW_TOOLS_SOCKET"] = socket_path
        env["PYTHONPATH"] = str(REPO_ROOT)
        shim = subprocess.Popen(
            [sys.executable, "-m", "host.runtime.tools_mcp_shim"],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(shim.wait)
        self.addCleanup(shim.stdin.close)
        return shim

    def rpc(self, shim: subprocess.Popen[str], message: dict) -> dict:
        shim.stdin.write(json.dumps(message) + "\n")
        shim.stdin.flush()
        line = shim.stdout.readline()
        self.assertTrue(line, "shim closed stdout unexpectedly")
        return json.loads(line)

    def test_shim_speaks_mcp_over_the_socket(self) -> None:
        socket_dir = tempfile.TemporaryDirectory()
        self.addCleanup(socket_dir.cleanup)
        socket_path = str(Path(socket_dir.name) / "tools.sock")
        server = tools_api.ToolsServer(socket_path, frozenset({os.getuid()}))
        import threading

        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # Enable runway so its actions list (which makes both media staging
        # actions advertised) and staging into it passes the enablement gate.
        with state.mutation() as cur:
            state.set_tool_enabled(cur, "runway", True)
        shim = self.start_shim(socket_path)

        initialized = self.rpc(shim, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "trustyclaw-tools")

        # Notifications get no response; the next reply must match the next id.
        shim.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        listed = self.rpc(shim, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertIn("fake_notes_read_note", names)
        self.assertIn("list_bundled_tools", names)
        self.assertIn("check_tool_approval", names)
        self.assertIn("stage_image", names)
        self.assertIn("stage_video", names)
        self.assertIn("app_api", names)
        self.assertTrue(all("inputSchema" in tool for tool in listed["result"]["tools"]))

        video = Path(socket_dir.name) / "clip.mp4"
        video.write_bytes(b"x" * 512)
        staged = self.rpc(
            shim,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "stage_video",
                    "arguments": {"path": str(video), "for_tool": "runway"},
                },
            },
        )
        self.assertFalse(staged["result"]["isError"])
        staged_result = json.loads(staged["result"]["content"][0]["text"])
        metadata = server.asset_store.describe("runway", staged_result["video_asset_id"])
        self.assertEqual(metadata.filename, "clip.mp4")
        self.assertEqual(metadata.size_bytes, 512)
        self.assertNotIn(str(video), staged["result"]["content"][0]["text"])

        image = Path(socket_dir.name) / "frame.png"
        image.write_bytes(b"i" * 512)
        staged_image = self.rpc(
            shim,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "stage_image",
                    "arguments": {"path": str(image), "for_tool": "runway"},
                },
            },
        )
        self.assertFalse(staged_image["result"]["isError"])
        image_result = json.loads(staged_image["result"]["content"][0]["text"])
        image_metadata = server.asset_store.describe("runway", image_result["image_asset_id"])
        self.assertEqual(image_metadata.filename, "frame.png")
        self.assertEqual(image_metadata.media_type, "image/png")

        symlink = Path(socket_dir.name) / "linked.mp4"
        symlink.symlink_to(video)
        rejected = self.rpc(
            shim,
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "stage_video",
                    "arguments": {"path": str(symlink), "for_tool": "runway"},
                },
            },
        )
        self.assertTrue(rejected["result"]["isError"])

        catalog = self.rpc(shim, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "list_bundled_tools", "arguments": {}}})
        self.assertFalse(catalog["result"]["isError"])
        catalog_text = catalog["result"]["content"][0]["text"]
        self.assertIn('"fake_notes"', catalog_text)
        self.assertIn('"gmail"', catalog_text)  # disabled bundled tools appear too

        called = self.rpc(shim, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "fake_notes_read_note", "arguments": {}}})
        self.assertFalse(called["result"]["isError"])
        self.assertIn('"token-1"', called["result"]["content"][0]["text"])

        pending = self.rpc(shim, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "fake_notes_write_note", "arguments": {"text": "hi"}}})
        self.assertFalse(pending["result"]["isError"])
        pending_text = pending["result"]["content"][0]["text"]
        self.assertIn("approval_id", pending_text)
        self.assertNotIn("approval_check_token", pending_text)
        self.assertIn("check_tool_approval", pending_text)

        failed = self.rpc(shim, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "missing", "arguments": {}}})
        self.assertTrue(failed["result"]["isError"])

        unknown = self.rpc(shim, {"jsonrpc": "2.0", "id": 6, "method": "bogus/method"})
        self.assertEqual(unknown["error"]["code"], -32601)

    def test_shim_lists_only_stable_app_api_when_tools_socket_is_missing(self) -> None:
        shim = self.start_shim("/nonexistent/tools.sock")
        listed = self.rpc(shim, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]], ["app_api"])
        called = self.rpc(shim, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "x", "arguments": {}}})
        self.assertTrue(called["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
