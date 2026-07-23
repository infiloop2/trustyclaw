from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager, ExitStack
import json
import hashlib
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import pg_harness

from host.network_integrations.bedrock import guard as bedrock_guard
from host.network_integrations.bedrock import manifest as bedrock_manifest
from host.network_integrations.bedrock.manifest import BedrockIntegration
from host.runtime.core import aws_sigv4
from host.network_integrations.claude import guard as claude_guard
from host.network_integrations.claude.manifest import ClaudeIntegration
from host.config import parse_network_controls
from host.network_integrations.openai import guard as openai_guard
from host.runtime.core import network_policy, state
from host.runtime.core.network_policy import load_policy
from host.runtime.core.state import save_network_policy as save_policy
from host.runtime.core.state import save_proxy_claude_account, save_proxy_openai_account_id
from state_seed import read_network_events
from host.runtime.network_proxy import service as network_proxy


class UpstreamHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.headers.get("Upgrade", "").lower() == "websocket":
            # A real WebSocket server rejects handshakes whose Connection
            # header was rewritten by an intermediary.
            if "upgrade" not in self.headers.get("Connection", "").lower():
                self.send_error(400, "missing Connection: Upgrade")
                return
            self.send_response(101)
            self.send_header("Connection", "Upgrade")
            self.send_header("Upgrade", "websocket")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"{self.path}".encode())

    def do_POST(self) -> None:
        # Echo the body back; requires Content-Length, like most upstreams that
        # reject chunked bodies the proxy is expected to decode.
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(b"echo:" + body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class NetworkProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_PROXY_STATE_DIR": self.temp_dir.name})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def save_policy(self, custom_domains: dict) -> None:
        integrations: dict = {"openai": {"enabled": True}}
        if custom_domains:
            integrations["custom"] = {"domains": custom_domains}
        save_policy({"network_integrations": integrations}, "2026-06-08T00:00:00Z")

    def start_http_server(self, tls: bool = False) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        if tls:
            cert, key = self.self_signed_cert("upstream")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def start_proxy(self) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), network_proxy.ProxyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_proxy_loads_typed_openai_config_without_generated_domain_rules(self) -> None:
        self.save_policy({})

        stored = load_policy()
        enforcement = network_proxy._load_enforcement_policy()

        self.assertNotIn("custom", stored["network_integrations"])
        self.assertTrue(enforcement.integrations["openai"].enabled)
        self.assertEqual(enforcement.to_json(), stored)

    def test_proxy_loads_valid_policy_without_status_file(self) -> None:
        self.save_policy({})

        policy, denial = network_proxy._policy_load_denial()

        self.assertIsNone(denial)
        self.assertIsNotNone(policy)
        self.assertTrue(policy.integrations["openai"].enabled)

    def test_proxy_denies_on_invalid_stored_policy(self) -> None:
        # The typed schema cannot hold a malformed policy document, so the
        # residual invalid case is a structurally valid but semantically bad
        # policy (here: an uncompilable path-guard regex). Still a policy
        # problem, not an availability blip: it must deny (never fall back to
        # a stale cached policy).
        from host.runtime.core import db

        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO network_policy (singleton, updated_at) VALUES (TRUE, %s)"
                " ON CONFLICT (singleton) DO UPDATE SET updated_at = EXCLUDED.updated_at",
                ("2026-06-08T00:00:00Z",),
            )
            cur.execute("INSERT INTO allowed_domains (domain) VALUES ('api.example.com')")
            cur.execute(
                "INSERT INTO domain_methods (domain, position, method) VALUES ('api.example.com', 0, 'GET')"
            )
            cur.execute(
                "INSERT INTO domain_path_guards (domain, position, pattern)"
                " VALUES ('api.example.com', 0, '(')"
            )

        policy, denial = network_proxy._policy_load_denial()

        self.assertIsNone(policy)
        self.assertIsNotNone(denial)
        self.assertEqual(denial, "network_policy_unavailable")

    def test_proxy_denies_everything_through_database_outages(self) -> None:
        # No fallback cache by design: the pins and the decision log live in
        # the same database, so nothing could keep flowing anyway — an outage
        # denies every request until the database returns. Simple, fail safe.
        save_policy({"network_integrations": {"custom": {"domains": {
            "api.example.com": {"allow_http_methods": ["GET"]}
        }}}}, "2026-06-08T00:00:00Z")
        loaded = network_proxy._load_enforcement_policy()
        self.assertIn("api.example.com", loaded.to_json()["network_integrations"]["custom"]["domains"])

        with patch("host.runtime.network_proxy.service.load_policy", side_effect=OSError("db down")):
            empty, denial = network_proxy._policy_load_denial()
        self.assertIsNone(empty)
        self.assertIsNotNone(denial)
        self.assertEqual(denial, "network_policy_unavailable")

        # The database coming back restores enforcement immediately.
        restored, denial = network_proxy._policy_load_denial()
        self.assertIsNone(denial)
        self.assertIsNotNone(restored)
        self.assertIn("api.example.com", restored.to_json()["network_integrations"]["custom"]["domains"])

    def self_signed_cert(self, name: str) -> tuple[str, str]:
        cert = Path(self.temp_dir.name) / f"{name}.crt"
        key = Path(self.temp_dir.name) / f"{name}.key"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return str(cert), str(key)

    def proxy_ca(self) -> None:
        ca_cert = Path(self.temp_dir.name) / "network_proxy_ca.crt"
        ca_key = Path(self.temp_dir.name) / "network_proxy_ca.key"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(ca_key), "-out", str(ca_cert), "-days", "1",
                "-subj", "/CN=TrustyClaw Test Proxy",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def send_raw(self, host: str, port: int, data: bytes) -> bytes:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)

    def test_plain_http_is_denied_even_for_allowed_domains(self) -> None:
        # The proxy is HTTPS-only: an http:// request is denied before any
        # body read, DNS resolution, or upstream dial, whatever the policy
        # says, and the denial is logged like any other decision.
        proxy = self.start_proxy()
        proxy_port = proxy.server_address[1]
        self.save_policy({"127.0.0.1": {"allow_http_methods": ["GET"]}})

        allowed_domain = self.send_raw(
            "127.0.0.1",
            proxy_port,
            b"GET http://127.0.0.1/allowed?x=1 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
        )
        other_port = self.send_raw(
            "127.0.0.1",
            proxy_port,
            b"GET http://127.0.0.1:8080/allowed HTTP/1.1\r\nHost: 127.0.0.1:8080\r\n\r\n",
        )

        self.assertIn(b"403", allowed_domain)
        self.assertIn(b"plain_http_denied", allowed_domain)
        self.assertIn(b"403", other_port)
        events = read_network_events()
        self.assertEqual([event["decision"] for event in events], ["denied", "denied"])
        self.assertEqual(events[0]["protocol"], "http")
        self.assertEqual(events[0]["query"], "x=1")
        self.assertEqual(events[0]["reason_code"], "plain_http_denied")
        self.assertEqual(events[1]["port"], 8080)
        self.assertEqual(events[1]["seq"], events[0]["seq"] + 1)

    def test_chunked_request_body_is_decoded_and_forwarded_with_content_length(self) -> None:
        self.proxy_ca()
        upstream = self.start_http_server(tls=True)
        proxy = self.start_proxy()
        self.save_policy({"127.0.0.1": {"allow_http_methods": ["POST"]}})
        request = (
            b"POST /upload HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n"
            b"6\r\n world\r\n"
            b"0\r\n\r\n"
        )

        with self.map_upstream(443, upstream.server_address[1], tls=True):
            response = self.https_via_proxy(proxy.server_address[1], request)

        self.assertIn(b"200 OK", response)
        self.assertIn(b"echo:hello world", response)

    def test_invalid_content_length_is_denied_without_reading_body(self) -> None:
        class Reader:
            def read(self, _amount: int) -> bytes:
                raise AssertionError("malformed Content-Length should not read")

        self.assertEqual(
            network_proxy.read_body(Reader(), [("Content-Length", "not-a-number")]),
            (b"", "request_body_malformed"),
        )
        self.assertEqual(
            network_proxy.read_body(Reader(), [("Content-Length", "-1")]),
            (b"", "request_body_malformed"),
        )

    def test_tunnel_body_parse_denial_is_logged(self) -> None:
        self.proxy_ca()
        proxy = self.start_proxy()
        self.save_policy({"127.0.0.1": {"allow_http_methods": ["GET"]}})

        response = self.https_via_proxy(
            proxy.server_address[1],
            b"GET / HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Length: nope\r\n\r\n",
        )

        self.assertIn(b"request_body_malformed", response)
        event = read_network_events()[-1]
        self.assertEqual(event["decision"], "denied")
        self.assertEqual(event["reason_code"], "request_body_malformed")
        self.assertEqual(event["host"], "127.0.0.1")

    def test_malformed_request_line_raises_controlled_parse_error(self) -> None:
        class Reader:
            def __init__(self, lines: list[bytes]) -> None:
                self._lines = iter(lines)

            def readline(self, _limit: int) -> bytes:
                return next(self._lines, b"")

        for request_line in (b"GET\r\n", b"GET / missing-version\r\n", b" / HTTP/1.1\r\n"):
            with self.subTest(request_line=request_line), self.assertRaisesRegex(OSError, "malformed request line"):
                network_proxy.read_request_head(Reader([request_line, b"\r\n"]))

    def test_malformed_ports_get_an_error_instead_of_killing_the_handler(self) -> None:
        # A non-numeric port in the CONNECT target must produce an HTTP error,
        # not an unhandled ValueError that kills the handler thread with no
        # response; a malformed plain-HTTP target still gets the 403 denial.
        proxy = self.start_proxy()
        self.save_policy({"127.0.0.1": {"allow_http_methods": ["GET"]}})

        connect = self.send_raw(
            "127.0.0.1",
            proxy.server_address[1],
            b"CONNECT 127.0.0.1:4a3 HTTP/1.1\r\nHost: 127.0.0.1:4a3\r\n\r\n",
        )
        absolute = self.send_raw(
            "127.0.0.1",
            proxy.server_address[1],
            b"GET http://127.0.0.1:8x0/allowed HTTP/1.1\r\nHost: 127.0.0.1:8x0\r\n\r\n",
        )

        self.assertIn(b"400", connect)
        self.assertIn(b"403", absolute)

    def test_connect_to_unlisted_host_is_denied_before_any_dial(self) -> None:
        proxy = self.start_proxy()
        self.save_policy({"allowed.example.com": {"allow_http_methods": ["GET"]}})

        real_create_connection = socket.create_connection

        def must_not_dial(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("proxy dialed upstream for a denied host")

        with patch("host.runtime.network_proxy.service.socket.create_connection", must_not_dial):
            with real_create_connection(("127.0.0.1", proxy.server_address[1]), timeout=5) as sock:
                sock.sendall(b"CONNECT evil.example.org:443 HTTP/1.1\r\nHost: evil.example.org:443\r\n\r\n")
                response = sock.recv(4096)

        self.assertIn(b"403", response)
        event = read_network_events()[-1]
        self.assertEqual(event["decision"], "denied")
        self.assertEqual(event["host"], "evil.example.org")
        self.assertEqual(event["method"], "CONNECT")
        # Denied events carry a reason code so a failed request is diagnosable.
        self.assertEqual(event["reason_code"], "host_not_allowed")

    def test_https_request_is_inspected_and_logged(self) -> None:
        self.proxy_ca()
        upstream = self.start_http_server(tls=True)
        proxy = self.start_proxy()
        self.save_policy(
            {
                "127.0.0.1": {
                    "allow_http_methods": ["GET"],
                    "path_guards": ["^/secure$"],
                }
            }
        )

        with self.map_upstream(443, upstream.server_address[1], tls=True):
            response = self.https_via_proxy(
                proxy.server_address[1], b"GET /secure HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
            )

        self.assertIn(b"200 OK", response)
        events = read_network_events()
        self.assertEqual(events[-1]["protocol"], "https")
        self.assertEqual(events[-1]["path"], "/secure")
        self.assertEqual(events[-1]["decision"], "allowed")

    def test_bedrock_response_is_metered_for_the_signing_runtime(self) -> None:
        # End to end through the real proxy handler: an allowed dummy-signed
        # Converse request is re-signed with the operator key, and the relayed
        # response's usage still lands on the signing runtime's counter row.
        # Regression: the meter must be selected from the as-received routing
        # identity — selecting it after rewrite_request_headers has replaced
        # the Authorization metered nothing.
        self.proxy_ca()
        bedrock_host = "bedrock-runtime.us-east-1.amazonaws.com"
        response_body = json.dumps(
            {"output": {}, "usage": {"inputTokens": 321, "outputTokens": 45}}
        ).encode()

        class ConverseHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

            def log_message(self, fmt: str, *args: object) -> None:
                return

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ConverseHandler)
        cert, key = self.self_signed_cert("bedrock-upstream")
        upstream_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        upstream_context.load_cert_chain(cert, key)
        upstream.socket = upstream_context.wrap_socket(upstream.socket, server_side=True)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        proxy = self.start_proxy()
        save_policy(
            {"network_integrations": {"bedrock": {"enabled": True}}}, "2026-06-08T00:00:00Z"
        )
        state.save_bedrock_credential("AKIAOPERATORKEY00001", "S" * 40, "us-east-1")

        path = "/model/deepseek.v3.2/converse"
        body = b'{"messages":[]}'
        amz_date = "20260717T120000Z"
        headers = [
            ("content-type", "application/json"),
            ("host", bedrock_host),
            ("x-amz-date", amz_date),
        ]
        authorization, _sig = aws_sigv4.header_signature(
            method="POST", path=path, query="", headers=headers,
            signed_headers=("content-type", "host", "x-amz-date"),
            payload_hash=hashlib.sha256(body).hexdigest(),
            amz_date=amz_date, date_stamp=amz_date[:8],
            region="us-east-1", service="bedrock",
            access_key_id=bedrock_manifest.ROUTING_ACCESS_KEY_ID,
            secret_access_key=bedrock_manifest.ROUTING_SECRET_ACCESS_KEY,
        )
        request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {bedrock_host}\r\n"
            "Content-Type: application/json\r\n"
            f"X-Amz-Date: {amz_date}\r\n"
            f"Authorization: {authorization}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode() + body

        # The Bedrock host is never resolved in the no-network test sandbox;
        # dial the local TLS upstream instead. connect_public's own SSRF
        # refusal is covered by its dedicated tests.
        upstream_port = upstream.server_address[1]
        with (
            patch.object(
                network_proxy,
                "connect_public",
                lambda host, port, timeout: socket.create_connection(
                    ("127.0.0.1", upstream_port), timeout=timeout
                ),
            ),
            patch(
                "host.runtime.network_proxy.service.ssl.create_default_context",
                ssl._create_unverified_context,
            ),
        ):
            response = self.https_via_proxy(
                proxy.server_address[1], request, host=bedrock_host
            )

        self.assertIn(b"200", response.split(b"\r\n", 1)[0])
        self.assertIn(response_body, response)
        (row,) = state.read_bedrock_usage("1970-01-01")
        self.assertEqual(row["model_id"], "deepseek.v3.2")
        self.assertEqual((row["requests"], row["metered_requests"]), (1, 1))
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (321, 45))

    def test_https_host_header_must_match_connect_host(self) -> None:
        self.proxy_ca()
        proxy = self.start_proxy()
        self.save_policy({"127.0.0.1": {"allow_http_methods": ["GET"]}})

        response = self.https_via_proxy(
            proxy.server_address[1], b"GET /secure HTTP/1.1\r\nHost: evil.example.org\r\n\r\n"
        )

        self.assertIn(b"403 Forbidden", response)
        event = read_network_events()[-1]
        self.assertEqual(event["decision"], "denied")
        self.assertEqual(event["reason_code"], "host_header_invalid")

    def test_https_non_origin_form_targets_are_denied(self) -> None:
        # Only origin-form targets are served inside the TLS tunnel: an
        # absolute-form target (even one naming another host) and a malformed
        # authority are the same crisp, logged denial.
        self.proxy_ca()
        proxy = self.start_proxy()
        self.save_policy({"127.0.0.1": {"allow_http_methods": ["GET"]}})

        for target in (b"https://evil.example.org/secure", b"https://127.0.0.1:4a3/secure"):
            with self.subTest(target=target):
                response = self.https_via_proxy(
                    proxy.server_address[1],
                    b"GET " + target + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
                )
                self.assertIn(b"403 Forbidden", response)
                event = read_network_events()[-1]
                self.assertEqual(event["decision"], "denied")
                self.assertEqual(event["reason_code"], "request_target_invalid")

    def test_connect_client_tls_abort_gets_no_http_response_bytes(self) -> None:
        # After "200 Connection Established" the client speaks TLS or nothing.
        # A client that aborts the MITM handshake must get a plain close — an
        # HTTP 502 written into that stream would be garbage bytes to a TLS
        # client and previously raised again inside the handler on a reset
        # socket.
        self.proxy_ca()
        proxy = self.start_proxy()
        self.save_policy({"127.0.0.1": {"allow_http_methods": ["GET"]}})

        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(5)
        raw.connect(("127.0.0.1", proxy.server_address[1]))
        self.addCleanup(raw.close)
        raw.sendall(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n")
        self.assertIn(b"200 Connection Established", raw.recv(4096))

        raw.sendall(b"not a TLS ClientHello")
        trailing = b""
        while True:
            try:
                chunk = raw.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            trailing += chunk
        # A TLS alert from the failed handshake is fine; an HTTP response is
        # the bug.
        self.assertNotIn(b"HTTP/", trailing)
        self.assertNotIn(b"502", trailing)

    def test_is_public_ip_rejects_internal_ranges(self) -> None:
        for good in ("93.184.216.34", "8.8.8.8", "1.1.1.1"):
            self.assertTrue(network_proxy._is_public_ip(good), good)
        for bad in (
            "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254",
            "0.0.0.0", "::1", "fc00::1", "fe80::1", "not-an-ip",
        ):
            self.assertFalse(network_proxy._is_public_ip(bad), bad)

    def test_connect_public_refuses_non_public_resolution_without_dialing(self) -> None:
        # An allowed domain pointed at the metadata IP must not be dialed.
        resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]
        with (
            patch("host.runtime.network_proxy.service.socket.getaddrinfo", return_value=resolved),
            patch("host.runtime.network_proxy.service.socket.create_connection") as create_connection,
        ):
            with self.assertRaises(OSError):
                network_proxy.connect_public("metadata.evil.test", 443, 5)
            create_connection.assert_not_called()

    def test_live_web_search_payload_is_denied_with_specific_reason(self) -> None:
        self.proxy_ca()
        proxy = self.start_proxy()
        save_policy(
            {"network_integrations": {"openai": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        save_proxy_openai_account_id("acct-test")
        body = b'{"tools":[{"type":"web_search","external_web_access":true}]}'
        request = (
            b"POST /v1/responses HTTP/1.1\r\nHost: chatgpt.com\r\n"
            b"ChatGPT-Account-ID: acct-test\r\n"
            b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )

        # No upstream is needed: the guard denies before any upstream connection.
        response = self.https_via_proxy(proxy.server_address[1], request, host="chatgpt.com")

        self.assertIn(b"403", response)
        self.assertIn(b"openai_web_tool_denied", response)
        self.assertEqual(read_network_events()[-1]["decision"], "denied")

    def test_wss_handshake_is_classified_as_wss_and_keeps_upgrade_headers(self) -> None:
        self.proxy_ca()
        upstream = self.start_http_server(tls=True)
        proxy = self.start_proxy()
        self.save_policy(
            {
                "127.0.0.1": {
                    "allow_http_methods": ["GET"],
                    "path_guards": ["^/socket$"],
                }
            }
        )
        request = (
            b"GET /socket HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n\r\n"
        )

        with self.map_upstream(443, upstream.server_address[1], tls=True):
            response = self.https_via_proxy(proxy.server_address[1], request)

        self.assertIn(b"101 Switching Protocols", response)
        self.assertEqual(read_network_events()[-1]["protocol"], "wss")

    def test_connection_slot_is_released_when_the_handler_thread_cannot_start(self) -> None:
        server = network_proxy.BoundedThreadingHTTPServer(("127.0.0.1", 0), network_proxy.ProxyHandler)
        self.addCleanup(server.server_close)
        client, request = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(request.close)
        slots = threading.BoundedSemaphore(1)

        with (
            patch.object(network_proxy, "CONNECTION_SLOTS", slots),
            patch.object(ThreadingHTTPServer, "process_request", side_effect=RuntimeError("cannot start thread")),
            self.assertRaises(RuntimeError),
        ):
            server.process_request(request, ("127.0.0.1", 65000))

        # The slot must come back even though process_request_thread (the
        # normal release point) never ran; a leak here eventually makes the
        # proxy silently drop every connection.
        self.assertTrue(slots.acquire(blocking=False))

    @contextmanager
    def map_upstream(self, port_from: int, upstream_port: int, tls: bool = False):
        """Redirect the proxy's upstream dials for 127.0.0.1:<port_from> to the
        local test server, so requests can target the default ports."""
        real_create_connection = socket.create_connection

        def mapped_create_connection(address, timeout=None, source_address=None, *args, **kwargs):  # type: ignore[no-untyped-def]
            if address == ("127.0.0.1", port_from):
                return real_create_connection(("127.0.0.1", upstream_port), timeout=timeout, source_address=source_address)
            return real_create_connection(address, timeout=timeout, source_address=source_address)

        # The tests use loopback as a stand-in upstream, which the SSRF guard
        # would otherwise refuse; allow it here. connect_public's own refusal is
        # covered by dedicated tests below.
        with ExitStack() as stack:
            stack.enter_context(patch("host.runtime.network_proxy.service.socket.create_connection", mapped_create_connection))
            stack.enter_context(patch("host.runtime.network_proxy.service._is_public_ip", lambda ip: True))
            if tls:
                stack.enter_context(patch("host.runtime.network_proxy.service.ssl.create_default_context", ssl._create_unverified_context))
            yield

    def https_via_proxy(self, proxy_port: int, request: bytes, *, host: str = "127.0.0.1") -> bytes:
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(5)
        raw.connect(("127.0.0.1", proxy_port))
        try:
            raw.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
            connect_response = raw.recv(4096)
            self.assertIn(b"200 Connection Established", connect_response)
            context = ssl._create_unverified_context()
            tls = context.wrap_socket(raw, server_hostname=host)
            try:
                tls.sendall(request)
                chunks: list[bytes] = []
                while True:
                    chunk = tls.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                tls.close()
        finally:
            raw.close()


LIVE_SEARCH = json.dumps({"tools": [{"type": "web_search", "external_web_access": True}]}).encode()
CACHED_SEARCH = json.dumps({"tools": [{"type": "web_search", "external_web_access": False}]}).encode()


def masked_frame(payload: bytes, opcode: int = 0x1, fin: bool = True, rsv: int = 0, masked: bool = True) -> bytes:
    """Build a (client-side) WebSocket frame; payloads under 126 bytes only."""
    assert len(payload) < 126
    mask = b"\x21\x07\x42\x9a"
    head = bytes([(0x80 if fin else 0) | (rsv << 4) | opcode])
    if not masked:
        return head + bytes([len(payload)]) + payload
    return head + bytes([0x80 | len(payload)]) + mask + bytes(
        byte ^ mask[index % 4] for index, byte in enumerate(payload)
    )


class WebSocketHandshakeTests(unittest.TestCase):
    def test_extension_offer_is_not_forwarded_upstream(self) -> None:
        # If the upstream accepted permessage-deflate, frames would carry RSV
        # bits the message guard must deny mid-stream. Dropping the client's
        # offer keeps both sides on plain, inspectable frames.
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        network_proxy.send_http_request(
            ours,
            "GET",
            "/socket",
            [
                ("Host", "chatgpt.com"),
                ("Upgrade", "websocket"),
                ("Connection", "Upgrade"),
                ("Sec-WebSocket-Key", "c2VjcmV0LWtleQ=="),
                ("Sec-WebSocket-Version", "13"),
                ("Sec-WebSocket-Extensions", "permessage-deflate; client_max_window_bits"),
            ],
            b"",
            websocket=True,
        )
        sent = b""
        while b"\r\n\r\n" not in sent:
            sent += theirs.recv(65536)
        self.assertNotIn(b"sec-websocket-extensions", sent.lower())
        self.assertIn(b"Upgrade: websocket", sent)
        self.assertIn(b"Sec-WebSocket-Key", sent)


class WebSocketGuardTests(unittest.TestCase):
    POLICY = parse_network_controls(
        {
            "network_integrations": {
                "openai": {"enabled": True},
                "custom": {"domains": {"example.com": {"allow_http_methods": ["GET"]}}},
            },
        }
    )

    def setUp(self) -> None:
        pg_harness.reset_database()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_PROXY_STATE_DIR": self.temp_dir.name})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def guard(self) -> network_proxy.WebSocketClientGuard:
        return network_proxy.WebSocketClientGuard(openai_guard.ws_message_denied)

    def test_cached_web_search_message_is_forwarded_unchanged(self) -> None:
        frame = masked_frame(CACHED_SEARCH)
        self.assertEqual(self.guard().feed(frame), frame)

    def test_live_web_search_message_is_denied(self) -> None:
        with self.assertRaises(network_proxy.WebSocketDenied) as denied:
            self.guard().feed(masked_frame(LIVE_SEARCH))
        self.assertEqual(denied.exception.code, "openai_web_tool_denied")

    def test_non_json_message_mentioning_web_search_is_forwarded(self) -> None:
        # The upstream parses messages as JSON; a frame it cannot parse cannot
        # declare tools, and text mentioning a tool name carries no capability.
        frame = masked_frame(b"plain text mentioning web_search_preview")
        self.assertEqual(self.guard().feed(frame), frame)

    def test_web_search_mention_in_prompt_text_is_forwarded(self) -> None:
        frame = masked_frame(
            b'{"type":"response.create","instructions":"never use web_search_preview",'
            b'"tools":[{"type":"function","name":"exec"}]}'
        )
        self.assertEqual(self.guard().feed(frame), frame)

    def test_dated_web_search_preview_variant_is_denied(self) -> None:
        with self.assertRaises(network_proxy.WebSocketDenied) as denied:
            self.guard().feed(
                masked_frame(b'{"tools":[{"type":"web_search_preview_2025_03_11"}]}')
            )
        self.assertEqual(denied.exception.code, "openai_web_tool_denied")

    def test_remote_mcp_tool_is_denied(self) -> None:
        with self.assertRaises(network_proxy.WebSocketDenied) as denied:
            self.guard().feed(
                masked_frame(
                    b'{"tools":[{"type":"mcp","server_label":"evil",'
                    b'"server_url":"https://evil.example/mcp"}]}'
                )
            )
        self.assertEqual(denied.exception.code, "openai_remote_mcp_denied")

    def test_indexed_web_search_message_is_denied(self) -> None:
        # external_web_access is false, but indexed_web_access authorizes the
        # upstream to fetch server-approved external URLs, so it must be denied.
        with self.assertRaises(network_proxy.WebSocketDenied) as denied:
            self.guard().feed(
                masked_frame(
                    b'{"tools":[{"type":"web_search","external_web_access":false,'
                    b'"indexed_web_access":true}]}'
                )
            )
        self.assertEqual(denied.exception.code, "openai_web_tool_denied")

    def test_bare_web_and_browse_tools_are_denied(self) -> None:
        for body in (
            b'{"tools":[{"type":"web"}]}',
            b'{"tools":[{"type":"web_fetch","url":"https://evil.example"}]}',
            b'{"tools":[{"type":"browser"}]}',
            b'{"tools":[{"type":"computer_use"}]}',
            b'{"tools":[{"type":"code_interpreter"}]}',
        ):
            with self.assertRaises(network_proxy.WebSocketDenied):
                self.guard().feed(masked_frame(body))

    def test_renamed_web_tool_carrying_access_flag_is_denied(self) -> None:
        # A tool under an unknown type that still carries a *_web_access flag
        # must fail closed rather than be forwarded.
        with self.assertRaises(network_proxy.WebSocketDenied):
            self.guard().feed(
                masked_frame(b'{"tools":[{"type":"surf","external_web_access":true}]}')
            )

    def test_cached_web_search_with_explicit_indexed_false_is_forwarded(self) -> None:
        body = (
            b'{"tools":[{"type":"web_search","external_web_access":false,'
            b'"indexed_web_access":false}]}'
        )
        frame = masked_frame(body)
        self.assertEqual(self.guard().feed(frame), frame)

    def test_non_web_hosted_tools_are_forwarded(self) -> None:
        # image_generation and local_shell reach no external URL with request
        # data; they must not be swept up by the web-tool guard.
        for body in (
            b'{"tools":[{"type":"image_generation"}]}',
            b'{"tools":[{"type":"local_shell"}]}',
        ):
            frame = masked_frame(body)
            self.assertEqual(self.guard().feed(frame), frame)

    def test_fragmented_message_is_reassembled_before_the_check(self) -> None:
        guard = self.guard()
        held = guard.feed(masked_frame(LIVE_SEARCH[:10], opcode=0x1, fin=False))
        self.assertEqual(held, b"")  # nothing forwarded until the message completes
        with self.assertRaises(network_proxy.WebSocketDenied):
            guard.feed(masked_frame(LIVE_SEARCH[10:], opcode=0x0, fin=True))

    def test_control_frames_pass_through_mid_message(self) -> None:
        guard = self.guard()
        ping = masked_frame(b"", opcode=0x9)
        cleared = guard.feed(masked_frame(b"{", opcode=0x1, fin=False) + ping)
        self.assertEqual(cleared, ping)

    def test_unmasked_and_extension_frames_are_denied(self) -> None:
        with self.assertRaises(network_proxy.WebSocketDenied):
            self.guard().feed(masked_frame(b"{}", masked=False))
        with self.assertRaises(network_proxy.WebSocketDenied):
            self.guard().feed(masked_frame(b"{}", rsv=0x4))  # e.g. permessage-deflate

    def run_tunnel(self, host: str) -> tuple[socket.socket, socket.socket, threading.Thread]:
        client_side, proxy_client = socket.socketpair()
        proxy_upstream, upstream_side = socket.socketpair()
        client_side.settimeout(5)
        upstream_side.settimeout(5)
        thread = threading.Thread(
            target=network_proxy.tunnel_websocket,
            args=(proxy_client, proxy_upstream, self.POLICY, "wss", host, 443, "/ws"),
            daemon=True,
        )
        thread.start()
        self.addCleanup(client_side.close)
        self.addCleanup(upstream_side.close)
        return client_side, upstream_side, thread

    def test_tunnel_denies_live_search_and_logs_the_decision(self) -> None:
        client, upstream, thread = self.run_tunnel("chatgpt.com")
        client.sendall(masked_frame(LIVE_SEARCH))

        close_frame = client.recv(1024)
        self.assertEqual(close_frame[0], 0x88)
        self.assertEqual(int.from_bytes(close_frame[2:4], "big"), 1008)
        self.assertEqual(upstream.recv(1024), b"")  # nothing reached the upstream side
        thread.join(timeout=5)

        event = read_network_events()[-1]
        self.assertEqual(event["decision"], "denied")
        self.assertEqual(event["method"], "MESSAGE")
        self.assertEqual(event["reason_code"], "openai_web_tool_denied")

    def test_tunnel_forwards_allowed_messages_both_ways(self) -> None:
        client, upstream, thread = self.run_tunnel("chatgpt.com")
        frame = masked_frame(CACHED_SEARCH)
        client.sendall(frame)
        self.assertEqual(upstream.recv(1024), frame)
        upstream.sendall(b"\x81\x03abc")  # unmasked server frame passes through untouched
        self.assertEqual(client.recv(1024), b"\x81\x03abc")
        client.close()
        thread.join(timeout=5)

    def test_tunnel_stays_opaque_for_unguarded_domains(self) -> None:
        client, upstream, thread = self.run_tunnel("example.com")
        client.sendall(b"\x00not a websocket frame at all")
        self.assertEqual(upstream.recv(1024), b"\x00not a websocket frame at all")
        client.close()
        thread.join(timeout=5)


class ExternalUrlRequestGuardTests(unittest.TestCase):
    """Direct coverage of the structural guard, including the standalone Codex
    search endpoints whose external_web_access/indexed_web_access live under
    ``settings`` rather than a tool object."""

    JSON = [("content-type", "application/json")]

    def deny(self, payload: dict, path: str | None = None) -> str | None:
        return openai_guard._external_url_request_denial(self.JSON, json.dumps(payload).encode(), path)

    def test_standalone_search_cached_is_allowed(self) -> None:
        for path in ("/backend-api/codex/alpha/search", "/v1/alpha/search"):
            self.assertIsNone(self.deny({"settings": {"external_web_access": False}}, path))

    def test_standalone_search_live_is_denied(self) -> None:
        reason = self.deny({"settings": {"external_web_access": True}}, "/v1/alpha/search")
        self.assertEqual(reason, "openai_web_tool_denied")

    def test_standalone_search_indexed_is_denied(self) -> None:
        reason = self.deny(
            {"settings": {"external_web_access": False, "indexed_web_access": True}},
            "/backend-api/codex/alpha/search",
        )
        self.assertEqual(reason, "openai_web_tool_denied")

    def test_standalone_search_missing_settings_is_denied(self) -> None:
        self.assertIsNotNone(self.deny({}, "/v1/alpha/search"))

    def test_indexed_flag_on_tool_is_denied(self) -> None:
        reason = self.deny(
            {"tools": [{"type": "web_search", "external_web_access": False, "indexed_web_access": True}]}
        )
        self.assertEqual(reason, "openai_web_tool_denied")

    def test_unknown_web_tool_fails_closed(self) -> None:
        for tool_type in ("web", "web_fetch", "browser", "computer_use", "computer_use_preview", "code_interpreter"):
            reason = self.deny({"tools": [{"type": tool_type}]})
            self.assertIsNotNone(reason, f"{tool_type} should be denied")

    def test_cached_web_search_still_allowed(self) -> None:
        self.assertIsNone(self.deny({"tools": [{"type": "web_search", "external_web_access": False}]}))

    def test_function_tool_still_allowed(self) -> None:
        self.assertIsNone(self.deny({"tools": [{"type": "function", "name": "exec"}]}))

    def test_non_web_tool_with_false_web_access_flag_is_allowed(self) -> None:
        # A false/default *_web_access flag grants no access; a safe tool that
        # carries one must not be swept into the guard and denied for its type.
        self.assertIsNone(self.deny({"tools": [{"type": "function", "external_web_access": False}]}))
        self.assertIsNone(self.deny({"tools": [{"type": "custom", "indexed_web_access": False}]}))

    def test_non_web_tool_with_truthy_web_access_flag_is_denied(self) -> None:
        # But a truthy flag under a non-web type is a renamed web tool: deny.
        self.assertIsNotNone(self.deny({"tools": [{"type": "surf", "external_web_access": True}]}))
        self.assertIsNotNone(self.deny({"tools": [{"type": "surf", "indexed_web_access": True}]}))


class BedrockGuardTests(unittest.TestCase):
    """The Bedrock integration admits Hermes model routes and re-signs them
    with the published operator key."""

    KEY = bedrock_manifest.ROUTING_ACCESS_KEY_ID
    ROUTING_SECRET = bedrock_manifest.ROUTING_SECRET_ACCESS_KEY
    REAL = ("AKIABEDROCKOPERATOR1", "operator-secret-000000000000000000000000", "us-east-2")
    CONFIG = BedrockIntegration(enabled=True)
    HOST = "bedrock-runtime.us-east-2.amazonaws.com"
    PATH = "/model/deepseek.v3.2/converse-stream"
    AMZ_DATE = "20260717T120000Z"
    BODY = b'{"messages":[]}'

    def signed_request(
        self,
        *,
        access_key_id: str | None = None,
        host: str | None = None,
        path: str | None = None,
        region: str = "us-east-2",
        service: str = "bedrock",
        body: bytes | None = None,
        secret: str | None = None,
        content_sha_header: bool = False,
    ) -> list[tuple[str, str]]:
        """Headers for a request signed the way a harness SDK signs it."""
        body = body if body is not None else self.BODY
        headers = [
            ("content-type", "application/json"),
            ("host", host or self.HOST),
            ("x-amz-date", self.AMZ_DATE),
        ]
        signed = ["content-type", "host", "x-amz-date"]
        if content_sha_header:
            headers.append(("x-amz-content-sha256", hashlib.sha256(body).hexdigest()))
            signed.insert(2, "x-amz-content-sha256")
        if secret is None:
            secret = self.ROUTING_SECRET
        authorization, _sig = aws_sigv4.header_signature(
            method="POST",
            path=path or self.PATH,
            query="",
            headers=headers,
            signed_headers=tuple(signed),
            payload_hash=hashlib.sha256(body).hexdigest(),
            amz_date=self.AMZ_DATE,
            date_stamp=self.AMZ_DATE[:8],
            region=region,
            service=service,
            access_key_id=access_key_id or self.KEY,
            secret_access_key=secret,
        )
        return [*headers, ("authorization", authorization)]

    def deny(
        self,
        *,
        host: str | None = None,
        path: str | None = None,
        query: str = "",
        headers: list[tuple[str, str]] | None = None,
        credential: tuple[str, str, str] | None | object = ...,
        config: BedrockIntegration | None = None,
        method: str = "POST",
        body: bytes | None = None,
    ) -> str | None:
        headers = headers if headers is not None else self.signed_request(host=host, path=path)
        selected = self.REAL if credential is ... else credential
        with patch.object(bedrock_guard, "read_bedrock_proxy_credential", return_value=selected):
            return bedrock_guard.request_denied(
                config or self.CONFIG, method, host or self.HOST, path or self.PATH, query, headers,
                body if body is not None else self.BODY,
            )

    def test_host_allowed_only_for_the_enabled_integration_region(self) -> None:
        with patch.object(bedrock_guard, "read_bedrock_proxy_credential", return_value=self.REAL):
            self.assertTrue(bedrock_guard.host_allowed(self.CONFIG, self.HOST.upper()))
            self.assertFalse(bedrock_guard.host_allowed(self.CONFIG, "bedrock-runtime.us-east-1.amazonaws.com"))
            self.assertFalse(
                bedrock_guard.host_allowed(BedrockIntegration(enabled=False), self.HOST)
            )

    def test_missing_credential_reaches_request_guard_on_supported_hosts(self) -> None:
        with patch.object(bedrock_guard, "read_bedrock_proxy_credential", return_value=None):
            self.assertTrue(
                bedrock_guard.host_allowed(
                    self.CONFIG, "bedrock-runtime.us-east-1.amazonaws.com"
                )
            )
            self.assertFalse(
                bedrock_guard.host_allowed(
                    self.CONFIG, "bedrock-runtime.eu-west-1.amazonaws.com"
                )
            )

    def test_enabled_bedrock_allows_converse_route_shapes(self) -> None:
        for path in (
            "/model/deepseek.v3.2/converse",
            "/model/qwen.qwen3-coder-next/converse-stream",
        ):
            with self.subTest(path=path):
                self.assertIsNone(self.deny(path=path, headers=self.signed_request(path=path)))

    def test_the_smithy_content_sha_header_shape_verifies_too(self) -> None:
        headers = self.signed_request(content_sha_header=True)
        self.assertIsNone(self.deny(headers=headers))

    def test_hermes_routing_key_is_admitted(self) -> None:
        self.assertIsNone(self.deny(headers=self.signed_request()))

    def test_response_meter_records_the_invoked_model(self) -> None:
        meter = bedrock_guard.response_meter(
            self.CONFIG, "POST", self.HOST, self.PATH, "",
            self.signed_request(), self.BODY,
        )
        assert meter is not None
        self.assertEqual(meter._model_id, "deepseek.v3.2")

    def test_no_meter_without_a_model_route(self) -> None:
        # response_meter runs only after request admission, so only the route
        # must be parsed again to identify the model.
        self.assertIsNone(
            bedrock_guard.response_meter(
                self.CONFIG, "POST", self.HOST, "/foundation-models", "",
                self.signed_request(), self.BODY,
            )
        )

    def test_a_foreign_scope_is_never_resigned(self) -> None:
        # The proxy must not mint real-key signatures valid for another
        # service or region, even over an otherwise allowed request.
        self.assertEqual(
            self.deny(headers=self.signed_request(service="s3")), "bedrock_signature_invalid"
        )
        self.assertEqual(
            self.deny(headers=self.signed_request(region="us-east-1")), "bedrock_signature_invalid"
        )

    def test_disabled_integration_fails_closed(self) -> None:
        config = BedrockIntegration(enabled=False)
        self.assertEqual(
            self.deny(config=config),
            "bedrock_credentials_unavailable",
        )

    def test_non_model_paths_and_methods_are_denied(self) -> None:
        self.assertEqual(self.deny(path="/model/deepseek.v3.2/invoke-with-response-stream-x"), "network_policy_denied")
        self.assertEqual(self.deny(path="/model/deepseek.v3.2/invoke"), "network_policy_denied")
        self.assertEqual(self.deny(path="/foundation-models"), "network_policy_denied")
        self.assertEqual(self.deny(method="GET"), "network_policy_denied")
        # An encoded slash in the model segment normalizes into extra path
        # segments and fails closed.
        self.assertEqual(
            self.deny(path="/model/arn%3Aaws%3Abedrock%2Fprofile/converse"), "network_policy_denied"
        )

    def test_missing_proxy_credentials_fail_closed(self) -> None:
        self.assertEqual(
            self.deny(credential=None), "bedrock_credentials_unavailable"
        )

    def test_unsigned_request_is_denied(self) -> None:
        self.assertEqual(self.deny(headers=[]), "bedrock_signature_required")
        self.assertEqual(
            self.deny(headers=[("authorization", "Bearer some-token")]), "bedrock_signature_required"
        )

    def test_foreign_access_key_is_denied(self) -> None:
        # An agent-smuggled real AWS credential is not a routing identity: the
        # proxy neither forwards nor re-signs for it.
        self.assertEqual(
            self.deny(headers=self.signed_request(access_key_id="AKIAATTACKERKEY00001", secret="attacker")),
            "bedrock_access_key_mismatch",
        )

    def test_dummy_signature_is_not_a_security_boundary(self) -> None:
        # The dummy secret is public and carries no AWS capability. The proxy
        # signs the exact body it receives only after the structural guard.
        self.assertIsNone(self.deny(headers=self.signed_request(secret="any-dummy-value")))
        self.assertIsNone(self.deny(body=b'{"messages":["changed"]}'))

    def test_query_string_auth_is_denied(self) -> None:
        self.assertEqual(
            self.deny(query="X-Amz-Signature=abc&X-Amz-Credential=AKIAX%2F20260717"),
            "bedrock_query_auth_denied",
        )
        self.assertEqual(self.deny(query="x-amz-signature=abc"), "bedrock_query_auth_denied")

    def test_session_credentials_are_denied(self) -> None:
        self.assertEqual(
            self.deny(headers=[*self.signed_request(), ("X-Amz-Security-Token", "tok")]),
            "bedrock_session_credentials_denied",
        )

    def test_rewrite_resigns_with_the_operator_credential(self) -> None:
        headers = self.signed_request()
        credential = self.REAL
        with patch.object(bedrock_guard, "read_bedrock_proxy_credential", return_value=credential):
            self.assertIsNone(
                bedrock_guard.request_denied(self.CONFIG, "POST", self.HOST, self.PATH, "", headers, self.BODY)
            )
            rewritten = bedrock_guard.rewrite_request_headers(
                self.CONFIG, "POST", self.HOST, self.PATH, "", headers, self.BODY
            )
        authorization = dict((k.lower(), v) for k, v in rewritten)["authorization"]
        real_key, real_secret, _region = self.REAL
        expected, _sig = aws_sigv4.header_signature(
            method="POST",
            path=self.PATH,
            query="",
            headers=headers,
            signed_headers=("content-type", "host", "x-amz-date"),
            payload_hash=hashlib.sha256(self.BODY).hexdigest(),
            amz_date=self.AMZ_DATE,
            date_stamp=self.AMZ_DATE[:8],
            region="us-east-2",
            service="bedrock",
            access_key_id=real_key,
            secret_access_key=real_secret,
        )
        self.assertEqual(authorization, expected)
        self.assertIn(real_key, authorization)
        self.assertNotIn(self.KEY, authorization)
        # Every other header (including the signed set) is untouched, so the
        # re-signed request stays byte-faithful to what was verified.
        self.assertEqual(
            [(k, v) for k, v in rewritten if k.lower() != "authorization"],
            [(k, v) for k, v in headers if k.lower() != "authorization"],
        )

    def test_rewrite_without_a_credential_leaves_the_request_worthless(self) -> None:
        # A reset racing the request: the published credential vanished
        # between the deny decision and the rewrite. The dummy signature goes
        # upstream and AWS rejects it — failure stays closed without secrets.
        headers = self.signed_request()
        with patch.object(bedrock_guard, "read_bedrock_proxy_credential", return_value=None):
            rewritten = bedrock_guard.rewrite_request_headers(
                self.CONFIG, "POST", self.HOST, self.PATH, "", headers, self.BODY
            )
        self.assertEqual(rewritten, headers)

    def test_rewrite_does_not_cross_a_replaced_credential_region(self) -> None:
        headers = self.signed_request()
        replacement = ("AKIAREPLACED", "replacement-secret", "us-west-2")
        with patch.object(
            bedrock_guard,
            "read_bedrock_proxy_credential",
            side_effect=[self.REAL, replacement],
        ):
            self.assertIsNone(
                bedrock_guard.request_denied(
                    self.CONFIG, "POST", self.HOST, self.PATH, "", headers, self.BODY
                )
            )
            rewritten = bedrock_guard.rewrite_request_headers(
                self.CONFIG, "POST", self.HOST, self.PATH, "", headers, self.BODY
            )
        self.assertEqual(rewritten, headers)


class AnthropicServerToolGuardTests(unittest.TestCase):
    """The api.anthropic.com body guard denies Anthropic server-side tools that
    run off-box (web search/fetch, code execution, remote MCP). Client-executed
    built-ins and user-defined tools pass."""

    JSON = [("content-type", "application/json"), ("authorization", "Bearer test-token")]
    ACCOUNT = {"access_token_sha256": hashlib.sha256(b"test-token").hexdigest()}

    def deny(self, payload: dict, allow_web_search: bool = False) -> str | None:
        with patch.object(claude_guard, "read_proxy_claude_account", return_value=self.ACCOUNT):
            return claude_guard.request_denied(
                ClaudeIntegration(True, allow_web_search),
                "POST", "api.anthropic.com", "/v1/messages", "", self.JSON,
                json.dumps(payload).encode(),
            )

    def test_server_side_web_tools_are_denied(self) -> None:
        for tool_type in ("web_search_20250305", "web_search_20260209", "web_fetch_20250910", "code_execution_20260120"):
            self.assertIsNotNone(self.deny({"tools": [{"type": tool_type}]}), f"{tool_type} should be denied")

    def test_web_search_allowed_when_operator_enabled(self) -> None:
        # With the toggle on, web search passes...
        self.assertIsNone(self.deny({"tools": [{"type": "web_search_20250305"}]}, allow_web_search=True))
        self.assertIsNone(self.deny({"tools": [{"type": "web_search_20260209"}]}, allow_web_search=True))
        # ...but the other off-box server tools stay denied regardless.
        self.assertIsNotNone(self.deny({"tools": [{"type": "web_fetch_20250910"}]}, allow_web_search=True))
        self.assertIsNotNone(self.deny({"tools": [{"type": "code_execution_20260120"}]}, allow_web_search=True))
        self.assertIsNotNone(self.deny({"mcp_servers": [{"type": "url", "name": "x", "url": "https://x/mcp"}]}, allow_web_search=True))

    def test_remote_mcp_servers_are_denied(self) -> None:
        self.assertIsNotNone(self.deny({"mcp_servers": [{"type": "url", "name": "x", "url": "https://x/mcp"}]}))

    def test_client_side_and_user_tools_are_allowed(self) -> None:
        self.assertIsNone(self.deny({"tools": [{"type": "bash_20250124", "name": "bash"}]}))
        self.assertIsNone(self.deny({"tools": [{"type": "text_editor_20250728"}, {"type": "memory_20250818"}]}))
        self.assertIsNone(self.deny({"tools": [{"name": "do_thing", "input_schema": {"type": "object"}}]}))
        self.assertIsNone(self.deny({"model": "x", "messages": [{"role": "user", "content": "hi"}]}))

    def test_empty_mcp_servers_is_allowed(self) -> None:
        self.assertIsNone(self.deny({"mcp_servers": []}))

    def test_undecodable_body_fails_closed(self) -> None:
        headers = self.JSON + [("content-encoding", "br")]
        reason = claude_guard.request_denied(
            ClaudeIntegration(True), "POST", "api.anthropic.com", "/v1/messages", "",
            headers, b"\x00\x01\x02",
        )
        self.assertIsNotNone(reason)

    def test_non_json_body_is_not_inspected(self) -> None:
        with patch.object(claude_guard, "read_proxy_claude_account", return_value=self.ACCOUNT):
            self.assertIsNone(
                claude_guard.request_denied(
                    ClaudeIntegration(True), "POST", "api.anthropic.com", "/v1/messages", "",
                    [("content-type", "text/plain"), ("authorization", "Bearer test-token")],
                    b"plain text mentioning web_search",
                )
            )


if __name__ == "__main__":
    unittest.main()
