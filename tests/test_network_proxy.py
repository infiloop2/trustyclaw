from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import pg_harness

from host.runtime.network_policy import load_policy
from host.runtime.state import save_network_policy as save_policy
from host.runtime.state import save_proxy_claude_account, save_proxy_openai_account_id
from state_seed import read_network_events
from host.runtime import network_proxy


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

    def save_policy(self, allowed_network_access: dict) -> None:
        save_policy(
            {
                "managed_network_integrations": {"openai": {"enabled": True}},
                "allowed_network_access": allowed_network_access,
            },
            "2026-06-08T00:00:00Z",
        )

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

    def test_proxy_expands_managed_openai_rules_in_memory_only(self) -> None:
        self.save_policy({})

        stored = load_policy()
        enforcement = network_proxy._load_enforcement_policy()

        for host in ("api.openai.com", "auth.openai.com", "chatgpt.com"):
            self.assertNotIn(host, stored["allowed_network_access"])
            self.assertIn(host, enforcement["allowed_network_access"])

    def test_proxy_loads_valid_policy_without_status_file(self) -> None:
        self.save_policy({})

        policy, denial = network_proxy._policy_load_denial()

        self.assertIsNone(denial)
        self.assertIn("api.openai.com", policy["allowed_network_access"])

    def test_proxy_denies_on_invalid_stored_policy(self) -> None:
        # The typed schema cannot hold a malformed policy document, so the
        # residual invalid case is a structurally valid but semantically bad
        # policy (here: an uncompilable path-guard regex). Still a policy
        # problem, not an availability blip: it must deny (never fall back to
        # a stale cached policy).
        from host.runtime import db

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

        self.assertEqual(policy, {})
        self.assertIsNotNone(denial)
        self.assertIn("network policy unavailable", denial)

    def test_proxy_denies_everything_through_database_outages(self) -> None:
        # No fallback cache by design: the pins and the decision log live in
        # the same database, so nothing could keep flowing anyway — an outage
        # denies every request until the database returns. Simple, fail safe.
        save_policy({"managed_network_integrations": {}, "allowed_network_access": {
            "api.example.com": {"allow_http_methods": ["GET"]}
        }}, "2026-06-08T00:00:00Z")
        loaded = network_proxy._load_enforcement_policy()
        self.assertIn("api.example.com", loaded["allowed_network_access"])

        with patch("host.runtime.network_proxy.load_policy", side_effect=OSError("db down")):
            empty, denial = network_proxy._policy_load_denial()
        self.assertEqual(empty, {})
        self.assertIsNotNone(denial)
        self.assertIn("network policy unavailable", denial)

        # The database coming back restores enforcement immediately.
        restored, denial = network_proxy._policy_load_denial()
        self.assertIsNone(denial)
        self.assertIn("api.example.com", restored["allowed_network_access"])

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
        self.assertIn(b"plain HTTP is not supported", allowed_domain)
        self.assertIn(b"403", other_port)
        events = read_network_events()
        self.assertEqual([event["decision"] for event in events], ["denied", "denied"])
        self.assertEqual(events[0]["protocol"], "http")
        self.assertEqual(events[0]["query"], "x=1")
        self.assertEqual(events[0]["reason"], "plain HTTP is not supported; use HTTPS")
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
            (b"", "malformed Content-Length"),
        )
        self.assertEqual(
            network_proxy.read_body(Reader(), [("Content-Length", "-1")]),
            (b"", "malformed Content-Length"),
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

        self.assertIn(b"malformed Content-Length", response)
        event = read_network_events()[-1]
        self.assertEqual(event["decision"], "denied")
        self.assertEqual(event["reason"], "malformed Content-Length")
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

        with patch("host.runtime.network_proxy.socket.create_connection", must_not_dial):
            with real_create_connection(("127.0.0.1", proxy.server_address[1]), timeout=5) as sock:
                sock.sendall(b"CONNECT evil.example.org:443 HTTP/1.1\r\nHost: evil.example.org:443\r\n\r\n")
                response = sock.recv(4096)

        self.assertIn(b"403", response)
        event = read_network_events()[-1]
        self.assertEqual(event["decision"], "denied")
        self.assertEqual(event["host"], "evil.example.org")
        self.assertEqual(event["method"], "CONNECT")
        # Denied events carry a reason so a failed request is diagnosable.
        self.assertIn("policy", event["reason"])

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
        self.assertIn("Host header", event["reason"])

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
                self.assertIn("request target must be origin-form", event["reason"])

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
            patch("host.runtime.network_proxy.socket.getaddrinfo", return_value=resolved),
            patch("host.runtime.network_proxy.socket.create_connection") as create_connection,
        ):
            with self.assertRaises(OSError):
                network_proxy.connect_public("metadata.evil.test", 443, 5)
            create_connection.assert_not_called()

    def test_live_web_search_payload_is_denied_with_specific_reason(self) -> None:
        self.proxy_ca()
        proxy = self.start_proxy()
        save_policy(
            {"managed_network_integrations": {"openai": {"enabled": True}}, "allowed_network_access": {}},
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
        self.assertIn(b"live web search is disabled", response)
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
            stack.enter_context(patch("host.runtime.network_proxy.socket.create_connection", mapped_create_connection))
            stack.enter_context(patch("host.runtime.network_proxy._is_public_ip", lambda ip: True))
            if tls:
                stack.enter_context(patch("host.runtime.network_proxy.ssl.create_default_context", ssl._create_unverified_context))
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
    POLICY = {
        "allowed_network_access": {
            "chatgpt.com": {"allow_http_methods": ["GET"], "openai_external_url_request_guard": True},
            "example.com": {"allow_http_methods": ["GET"]},
        },
    }

    def setUp(self) -> None:
        pg_harness.reset_database()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_PROXY_STATE_DIR": self.temp_dir.name})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def guard(self) -> network_proxy.WebSocketClientGuard:
        return network_proxy.WebSocketClientGuard()

    def test_cached_web_search_message_is_forwarded_unchanged(self) -> None:
        frame = masked_frame(CACHED_SEARCH)
        self.assertEqual(self.guard().feed(frame), frame)

    def test_live_web_search_message_is_denied(self) -> None:
        with self.assertRaises(network_proxy.WebSocketDenied) as denied:
            self.guard().feed(masked_frame(LIVE_SEARCH))
        self.assertIn("live web search is disabled", denied.exception.reason)

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
        self.assertIn("web_search_preview is disabled", denied.exception.reason)

    def test_remote_mcp_tool_is_denied(self) -> None:
        with self.assertRaises(network_proxy.WebSocketDenied) as denied:
            self.guard().feed(
                masked_frame(
                    b'{"tools":[{"type":"mcp","server_label":"evil",'
                    b'"server_url":"https://evil.example/mcp"}]}'
                )
            )
        self.assertIn("remote MCP tools are disabled", denied.exception.reason)

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
        self.assertIn("live web search is disabled", event["reason"])

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


if __name__ == "__main__":
    unittest.main()
