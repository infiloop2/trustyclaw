from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

import pg_harness

from host.runtime.agent_network import api as network_introspection_api
from host.runtime.core import state
from host.runtime.agent_shim.mcp_shim import UnixHTTPConnection

REPO_ROOT = Path(__file__).resolve().parents[1]


class NetworkIntrospectionTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()

    def start_server(self, agent_uids: frozenset[int] | None = None) -> str:
        socket_dir = tempfile.TemporaryDirectory()
        self.addCleanup(socket_dir.cleanup)
        socket_path = str(Path(socket_dir.name) / "agent-network.sock")
        server = network_introspection_api.NetworkIntrospectionServer(
            socket_path,
            agent_uids if agent_uids is not None else frozenset({os.getuid()}),
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return socket_path

    def http(self, socket_path: str, method: str, path: str, body: dict | None = None):
        connection = UnixHTTPConnection(socket_path)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            connection.request(
                method,
                path,
                body=payload,
                headers={"Content-Type": "application/json"} if payload is not None else {},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_lists_every_integration_including_custom_domains(self) -> None:
        state.save_network_policy(
            {
                "network_integrations": {
                    "github": {
                        "enabled": True,
                        "write_repositories": [{"owner": "infiloop2", "repo": "trustyclaw"}],
                        "require_dot_github_approval": True,
                    },
                    "custom": {"domains": {"example.com": {"allow_http_methods": ["GET"]}}},
                },
            },
            "2026-07-16T00:00:00Z",
        )

        result = network_introspection_api.call_action("list_network_integrations", {})

        by_id = {
            entry["integration_id"]: entry
            for entry in result["result"]["network_integrations"]
        }
        self.assertEqual(
            sorted(by_id),
            ["claude", "custom", "github", "npm_packages", "openai", "python_packages"],
        )
        self.assertTrue(by_id["github"]["enabled"])
        self.assertEqual(
            by_id["github"]["options"],
            {
                "write_repositories": [{"owner": "infiloop2", "repo": "trustyclaw"}],
                "require_dot_github_approval": True,
            },
        )
        self.assertTrue(by_id["custom"]["enabled"])
        self.assertEqual(
            by_id["custom"]["options"]["domains"],
            {"example.com": {"allow_http_methods": ["GET"]}},
        )
        self.assertFalse(by_id["openai"]["enabled"])

    def test_recent_denials_are_joined_with_catalog_guidance(self) -> None:
        state.append_network_event("https", "GET", "allowed.example.com", 443, "/", "", True)
        state.append_network_event(
            "https", "POST", "github.com", 443,
            "/acme/website.git/git-receive-pack", "", False,
            "github_write_repo_required",
        )
        state.append_network_event(
            "https", "CONNECT", "evil.example.com", 443, "", "", False,
            "host_not_allowed",
        )

        result = network_introspection_api.call_action("recent_network_denials", {})

        denials = result["result"]["denials"]
        self.assertEqual([denial["host"] for denial in denials], ["evil.example.com", "github.com"])
        self.assertIn("write repositories", denials[1]["guidance"])
        self.assertIn("custom-domain rule", denials[0]["guidance"])

    def test_limit_validation_and_socket_peer_gate(self) -> None:
        for bad in (0, 101, "5", True):
            with self.subTest(limit=bad), self.assertRaisesRegex(
                network_introspection_api.NetworkToolCallError, "limit"
            ):
                network_introspection_api.call_action("recent_network_denials", {"limit": bad})

        rejected_socket = self.start_server(frozenset())
        status, _body = self.http(rejected_socket, "GET", "/tools")
        self.assertEqual(status, 403)

    def test_mcp_shim_aggregates_the_dedicated_socket(self) -> None:
        network_socket = self.start_server()
        env = os.environ.copy()
        env["TRUSTYCLAW_TOOLS_SOCKET"] = "/nonexistent/tools.sock"
        env["TRUSTYCLAW_AGENT_NETWORK_SOCKET"] = network_socket
        env["PYTHONPATH"] = str(REPO_ROOT)
        shim = subprocess.Popen(
            [sys.executable, "-m", "host.runtime.agent_shim.mcp_shim"],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(shim.wait)
        self.addCleanup(shim.stdin.close)

        shim.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
        shim.stdin.flush()
        listed = json.loads(shim.stdout.readline())
        self.assertEqual(
            [tool["name"] for tool in listed["result"]["tools"]],
            ["list_network_integrations", "recent_network_denials", "app_api"],
        )

        shim.stdin.write(json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_network_integrations", "arguments": {}},
        }) + "\n")
        shim.stdin.flush()
        called = json.loads(shim.stdout.readline())
        self.assertFalse(called["result"]["isError"])
        self.assertIn('"custom"', called["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
