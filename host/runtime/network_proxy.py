"""Localhost network policy proxy (127.0.0.1:7445), runs as trustyclaw-proxy.

All agent traffic is forced here: nftables drops direct outbound traffic for
non-root users, and the agent runs with HTTP(S)_PROXY pointing at this proxy.

The proxy is HTTPS/WSS-only: traffic arrives as CONNECT and is inspected by
terminating client TLS with a certificate signed by the TrustyClaw proxy CA,
then opening a separate TLS connection upstream. Plain HTTP is denied with a
logged 403 — no allowed destination speaks it, and the GitHub credential must
never travel an unencrypted socket.

Policy checks happen before any upstream DNS resolution or connection, so a
denied host name is never resolved (host names are otherwise a data
exfiltration channel). Every decision is recorded in the network_events table.
Requests are denied whenever the persisted policy cannot be parsed.

On domains whose rule requires message inspection (the external URL request
guard), WebSocket connections are not opaque tunnels: each client→upstream
message is parsed out of its frames and policy-checked before forwarding, and
a violation closes the connection with a 1008 close frame.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import select
import socket
import ssl
import subprocess
import threading
from typing import Any
import urllib.parse

from host.config import expand_network_controls, parse_network_controls
from host.constants import LOOPBACK, PROXY_PORT
from host.runtime.network_policy import (
    decide_http_request,
    anthropic_request_denied,
    github_credential_headers,
    github_push_gate_response,
    github_request_denied,
    host_allowed,
    load_policy,
    openai_request_denied,
    openai_ws_message_denied,
    websocket_inspection_required,
)
from host.runtime.state import (
    append_network_event,
    network_proxy_cert_files,
)


HOST = LOOPBACK
PORT = PROXY_PORT
BUFFER_SIZE = 65536
MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 128 * 1024 * 1024  # bodies are buffered in memory for inspection
MAX_CONNECTIONS = 64  # cap concurrent handlers so buffered bodies cannot OOM the proxy
IDLE_TIMEOUT = 310.0


def _load_enforcement_policy() -> dict[str, Any]:
    """Load, validate, and expand the stored policy for this request.

    The stored policy is the operator-facing config; managed provider domains
    are expanded only inside the proxy process so the stored config never
    contains generated OpenAI rules. There is deliberately no fallback cache:
    any failure — an unavailable database exactly like an invalid policy —
    propagates and the request is denied. The other enforcement inputs
    (account pins) and the decision log live in the same database, so a
    cached policy could not keep requests flowing through an outage anyway;
    denying everything until the database returns is the simple, fail-safe
    behavior.
    """
    return expand_network_controls(parse_network_controls(load_policy()))


def _policy_load_denial() -> tuple[dict[str, Any], str | None]:
    try:
        return _load_enforcement_policy(), None
    except Exception as exc:
        return {}, f"network policy unavailable: {exc}"


CERT_LOCK = threading.Lock()
CONNECTION_SLOTS = threading.BoundedSemaphore(MAX_CONNECTIONS)


def _is_public_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def connect_public(host: str, port: int, timeout: float) -> socket.socket:
    """Resolve ``host`` and connect only if every resolved address is publicly
    routable. An allowed domain (especially a wildcard) that resolves to a
    loopback, link-local, or private address — by misconfiguration or DNS
    rebinding — must not let the proxy reach internal services (SSRF).
    Connects to the vetted address rather than re-resolving."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError(f"could not resolve {host}") from exc
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if not isinstance(ip, str):
            raise OSError(f"resolved non-string address {ip!r} for {host}")
        if not _is_public_ip(ip):
            raise OSError(f"refusing to connect to non-public address {ip} for {host}")
        ips.append(ip)
    return socket.create_connection((ips[0], port), timeout=timeout)


def request_denial_reason(
    policy: dict,
    protocol: str,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> str | None:
    """Why this request is denied, or None if it is allowed. The specific reason
    (e.g. a live web search denial) is returned to the client and logged."""
    if not decide_http_request(policy, protocol, method, host, path, query):
        return "network policy denied request"
    return (
        openai_request_denied(policy, host, headers, body, path=path)
        or anthropic_request_denied(policy, method, host, path, headers)
        or github_request_denied(policy, method, host, path, query, body)
    )


def host_header_denial(headers: list[tuple[str, str]], expected_host: str, expected_port: int) -> str | None:
    presented = [value for key, value in headers if key.lower() == "host"]
    if not presented:
        return "Host header is required"
    if len(presented) != 1:
        return "exactly one Host header is required"
    try:
        host, port = _split_host_port(presented[0], expected_port)
    except ValueError:
        return "Host header port is invalid"
    if host.lower() != expected_host.lower() or port != expected_port:
        return "Host header does not match the vetted upstream host"
    return None


class ProxyHandler(BaseHTTPRequestHandler):
    timeout = IDLE_TIMEOUT

    def do_CONNECT(self) -> None:
        self.close_connection = True
        try:
            host, port = _split_host_port(self.path, 443)
        except ValueError:
            self.send_error(400, "CONNECT target is invalid")
            return
        # Deny before any DNS or upstream connection happens for this host.
        policy, policy_error = _policy_load_denial()
        if port != 443:
            reason = "only port 443 is allowed for CONNECT"
        elif policy_error is not None:
            reason = policy_error
        elif not host_allowed(policy, host):
            reason = "host is not in the allowed network policy"
        else:
            reason = None
        if reason is not None:
            append_network_event("https", "CONNECT", host, port, "", "", False, reason)
            self.send_error(403, reason)
            return
        try:
            cert_path, key_path = ensure_host_cert(host)
        except (OSError, subprocess.CalledProcessError) as exc:
            self.send_error(502, str(exc))
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        client_context.load_cert_chain(cert_path, key_path)
        try:
            client_tls = client_context.wrap_socket(self.connection, server_side=True)
        except OSError:
            # CONNECT already succeeded. If the client closes during the MITM
            # handshake there is no valid HTTP response channel left.
            return
        self._serve_tls_request(host, port, client_tls)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _deny_plain_http(self) -> None:
        """Plain HTTP (and WS) is not supported: every allowed destination
        speaks HTTPS/WSS via CONNECT, so an http:// request can only be a
        misconfiguration or a downgrade. Denied before any body read, DNS
        resolution, or upstream connection, and logged like any other
        denial."""
        self.close_connection = True
        host, port, path, query = "", 80, "/", ""
        try:
            parsed = urllib.parse.urlsplit(self.path)
            host = parsed.hostname or ""
            port = parsed.port or 80
            path = parsed.path or "/"
            query = parsed.query
        except ValueError:
            pass  # malformed authority: log the denial with the defaults
        reason = "plain HTTP is not supported; use HTTPS"
        append_network_event("http", self.command, host, port, path, query, False, reason)
        self.send_error(403, reason)

    # Every plain (non-CONNECT) method is denied the same way.
    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _deny_plain_http

    def _serve_tls_request(self, host: str, port: int, client_tls: ssl.SSLSocket) -> None:
        """Read one decrypted request from the client, decide, then connect
        upstream and forward. The forced ``Connection: close`` keeps the
        connection to a single policy-checked request."""
        upstream_tls = None
        try:
            client_tls.settimeout(IDLE_TIMEOUT)
            reader = SocketReader(client_tls)
            method, target, headers = read_request_head(reader)
            # Origin-form only: every real client sends origin-form inside a
            # CONNECT tunnel, and a leading "/" cannot carry a scheme or
            # authority, so nothing needs re-vetting against the tunnel host.
            # Anything else (absolute-form, authority-form, garbage) is denied
            # outright — fail closed, one reason.
            path, query = "/", ""
            target_denial = None
            if target.startswith("/"):
                parsed = urllib.parse.urlsplit(target)
                path = parsed.path or "/"
                query = parsed.query
            else:
                target_denial = "request target must be origin-form"
            is_websocket = any(key.lower() == "upgrade" and value.lower() == "websocket" for key, value in headers)
            protocol = "wss" if is_websocket else "https"
            body, body_deny = read_body(reader, headers)
            policy, policy_error = _policy_load_denial()
            reason = (
                body_deny
                or target_denial
                or host_header_denial(headers, host, port)
                or policy_error
                or request_denial_reason(policy, protocol, method, host, path, query, headers, body)
            )
            # The .github approval gate runs after the write guard passes: a
            # gated push that changes .github/ is answered with a git
            # report-status ("queued for approval") instead of being forwarded.
            gate_response = None
            if reason is None:
                gate_response, gate_reason = github_push_gate_response(policy, method, host, path, body)
                if gate_reason is not None:
                    reason = gate_reason
            append_network_event(protocol, method, host, port, path, query, reason is None, reason)
            if gate_response is not None:
                client_tls.sendall(gate_response)
                return
            if reason is not None:
                message = reason.encode()
                client_tls.sendall(
                    b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: "
                    + str(len(message)).encode()
                    + b"\r\n\r\n"
                    + message
                )
                return
            # After the allow decision: on GitHub domains the proxy
            # authenticates the request itself (agent Authorization stripped,
            # the working token injected) — the agent never holds the token.
            headers = github_credential_headers(host, headers)
            upstream_raw = connect_public(host, port, timeout=15)
            upstream_tls = ssl.create_default_context().wrap_socket(upstream_raw, server_hostname=host)
            upstream_tls.settimeout(IDLE_TIMEOUT)
            send_http_request(upstream_tls, method, target, headers, body, websocket=is_websocket)
            if is_websocket:
                # reader.drain(): frames the client pipelined behind the handshake.
                tunnel_websocket(
                    client_tls, upstream_tls, policy, protocol, host, port, path,
                    initial_client_bytes=reader.drain(),
                )
            else:
                forward_until_close(upstream_tls, client_tls)
        except OSError:
            pass
        finally:
            if upstream_tls is not None:
                upstream_tls.close()
            client_tls.close()


def _split_host_port(authority: str, default_port: int) -> tuple[str, int]:
    if ":" not in authority:
        return authority, default_port
    host, port = authority.rsplit(":", 1)
    return host, int(port)


def ensure_host_cert(host: str) -> tuple[str, str]:
    certs = network_proxy_cert_files(host)
    with CERT_LOCK:
        if certs.cert.exists() and certs.key.exists():
            return str(certs.cert), str(certs.key)
        certs.directory.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["/usr/bin/openssl", "req", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(certs.key), "-out", str(certs.csr), "-subj", f"/CN={host}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        certs.ext.write_text(f"subjectAltName=DNS:{host}\n")
        subprocess.run(
            ["/usr/bin/openssl", "x509", "-req", "-in", str(certs.csr),
             "-CA", str(certs.ca_cert), "-CAkey", str(certs.ca_key), "-CAcreateserial",
             "-out", str(certs.cert), "-days", "365", "-sha256", "-extfile", str(certs.ext)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        certs.key.chmod(0o600)
        return str(certs.cert), str(certs.key)


class SocketReader:
    """Minimal buffered reader over a socket. Unlike ``makefile`` it can hand
    back any unconsumed bytes, which matters when the connection turns into an
    opaque tunnel after a WebSocket handshake."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = b""

    def readline(self, limit: int = MAX_HEADER_BYTES) -> bytes:
        while b"\n" not in self._buffer:
            if len(self._buffer) > limit:
                raise OSError("header line too long")
            chunk = self._sock.recv(BUFFER_SIZE)
            if not chunk:
                line, self._buffer = self._buffer, b""
                return line
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line + b"\n"

    def read(self, amount: int) -> bytes:
        while len(self._buffer) < amount:
            chunk = self._sock.recv(BUFFER_SIZE)
            if not chunk:
                break
            self._buffer += chunk
        data, self._buffer = self._buffer[:amount], self._buffer[amount:]
        return data

    def drain(self) -> bytes:
        data, self._buffer = self._buffer, b""
        return data


def read_request_head(reader: Any) -> tuple[str, str, list[tuple[str, str]]]:
    """Parse the request line and headers from anything with ``readline``."""
    request_line = reader.readline(MAX_HEADER_BYTES)
    if not request_line.strip():
        raise OSError("connection closed before request line")
    try:
        method, target, version = request_line.decode("iso-8859-1").strip().split(" ", 2)
    except ValueError as exc:
        raise OSError("malformed request line") from exc
    if not method or not target or not version.startswith("HTTP/"):
        raise OSError("malformed request line")
    headers: list[tuple[str, str]] = []
    total = len(request_line)
    while True:
        line = reader.readline(MAX_HEADER_BYTES)
        total += len(line)
        if total > MAX_HEADER_BYTES:
            raise OSError("request headers too large")
        if line in (b"\r\n", b"\n", b""):
            break
        if b":" in line:
            key, value = line.decode("iso-8859-1").split(":", 1)
            headers.append((key.strip(), value.strip()))
    return method.upper(), target, headers


def read_body(reader: Any, headers: list[tuple[str, str]]) -> tuple[bytes, str | None]:
    """Read the full request body (Content-Length or chunked) from anything
    with ``read``/``readline``. Returns (body, deny reason). The body is
    buffered and capped so the policy check always sees it completely."""
    header_map = {key.lower(): value for key, value in headers}
    if "chunked" in header_map.get("transfer-encoding", "").lower():
        return read_chunked_body(reader)
    try:
        length = int(header_map.get("content-length", "0") or "0")
    except ValueError:
        return b"", "malformed Content-Length"
    if length < 0:
        return b"", "malformed Content-Length"
    if length > MAX_BODY_BYTES:
        return b"", "request body too large"
    return reader.read(length), None


def read_chunked_body(reader: Any) -> tuple[bytes, str | None]:
    body = b""
    while True:
        size_line = reader.readline(MAX_HEADER_BYTES)
        try:
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            return b"", "malformed chunked request body"
        if size == 0:
            while True:  # consume optional trailers up to the blank line
                line = reader.readline(MAX_HEADER_BYTES)
                if line in (b"\r\n", b"\n", b""):
                    break
            return body, None
        if len(body) + size > MAX_BODY_BYTES:
            return b"", "request body too large"
        body += reader.read(size)
        reader.read(2)  # CRLF after each chunk


def send_http_request(
    upstream: socket.socket,
    method: str,
    target: str,
    headers: list[tuple[str, str]],
    body: bytes,
    *,
    websocket: bool,
) -> None:
    """Forward the request. The body was fully read (and de-chunked) for
    inspection, so it is re-sent with an explicit Content-Length. WebSocket
    handshakes keep their Connection/Upgrade headers; everything else is
    pinned to Connection: close so the upstream socket carries exactly one
    policy-checked request."""
    upstream.sendall(f"{method} {target} HTTP/1.1\r\n".encode("ascii"))
    had_body_header = False
    for key, value in headers:
        lower = key.lower()
        if lower in {"content-length", "transfer-encoding"}:
            had_body_header = True
            continue
        if lower in {"proxy-connection", "proxy-authorization"}:
            continue
        if lower == "sec-websocket-extensions":
            # Never forward the client's extension offer (e.g.
            # permessage-deflate): an accepted extension compresses frames and
            # sets RSV bits, which the message guard cannot inspect and would
            # deny mid-stream. With the offer dropped, neither side negotiates
            # an extension and the frames stay plain.
            continue
        if lower == "connection" and not websocket:
            continue
        upstream.sendall(f"{key}: {value}\r\n".encode("iso-8859-1"))
    if body or had_body_header:
        upstream.sendall(f"Content-Length: {len(body)}\r\n".encode("ascii"))
    if not websocket:
        upstream.sendall(b"Connection: close\r\n")
    upstream.sendall(b"\r\n")
    if body:
        upstream.sendall(body)


def forward_until_close(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            data = source.recv(BUFFER_SIZE)
            if not data:
                return
            target.sendall(data)
    finally:
        source.close()


class WebSocketDenied(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WebSocketClientGuard:
    """Parses client→upstream WebSocket frames so each complete text/binary
    message can be policy-checked before it is forwarded. RFC 6455 requires
    client frames to be masked and extensions are not negotiated by our
    clients, so anything malformed, extension-compressed (RSV bits), or
    oversized is denied rather than blindly forwarded — the same fail-closed
    posture as the HTTP body guard."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._message = bytearray()
        self._frames: list[bytes] = []  # raw frames of the in-progress message
        self._fragmented = False

    def feed(self, data: bytes) -> bytes:
        """Consume client bytes; return the frames cleared for forwarding.
        Raises WebSocketDenied when a message violates policy or the stream
        cannot be safely inspected."""
        self._buffer.extend(data)
        cleared = bytearray()
        while (frame := self._next_frame()) is not None:
            raw, fin, opcode, payload = frame
            if opcode in (0x8, 0x9, 0xA):  # close/ping/pong pass through
                cleared += raw
                continue
            if opcode == 0x0:  # continuation
                if not self._fragmented:
                    raise WebSocketDenied("unexpected websocket continuation frame")
            elif opcode in (0x1, 0x2):  # text/binary
                if self._fragmented:
                    raise WebSocketDenied("interleaved websocket data frames")
            else:
                raise WebSocketDenied(f"unsupported websocket opcode {opcode}")
            self._message.extend(payload)
            if len(self._message) > MAX_BODY_BYTES:
                raise WebSocketDenied("websocket message too large to inspect")
            self._frames.append(raw)
            self._fragmented = not fin
            if fin:
                reason = openai_ws_message_denied(bytes(self._message))
                if reason is not None:
                    raise WebSocketDenied(reason)
                cleared += b"".join(self._frames)
                self._frames.clear()
                self._message.clear()
        return bytes(cleared)

    def _next_frame(self) -> tuple[bytes, bool, int, bytes] | None:
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        if buffer[0] & 0x70:
            raise WebSocketDenied("websocket frame uses RSV bits; extensions are not supported")
        fin = bool(buffer[0] & 0x80)
        opcode = buffer[0] & 0x0F
        if not buffer[1] & 0x80:
            raise WebSocketDenied("client websocket frame is not masked")
        length = buffer[1] & 0x7F
        offset = 2
        if length == 126:
            if len(buffer) < 4:
                return None
            length, offset = int.from_bytes(buffer[2:4], "big"), 4
        elif length == 127:
            if len(buffer) < 10:
                return None
            length, offset = int.from_bytes(buffer[2:10], "big"), 10
        if length > MAX_BODY_BYTES:
            raise WebSocketDenied("websocket frame too large to inspect")
        total = offset + 4 + length
        if len(buffer) < total:
            return None
        raw = bytes(buffer[:total])
        mask = raw[offset : offset + 4]
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw[offset + 4 :]))
        del buffer[:total]
        return raw, fin, opcode, payload


def _websocket_close_frame(status_code: int, reason: str) -> bytes:
    payload = status_code.to_bytes(2, "big") + reason.encode()[:120]
    return bytes([0x88, len(payload)]) + payload


def tunnel_websocket(
    client: socket.socket,
    upstream: socket.socket,
    policy: dict[str, Any],
    protocol: str,
    host: str,
    port: int,
    path: str,
    initial_client_bytes: bytes = b"",
) -> None:
    """Relay a WebSocket connection. When the domain rule requires message
    inspection (external URL request guard), each client→upstream message is
    policy-checked before forwarding; a violation is logged, answered with a
    1008 close frame, and ends the connection. Upstream→client frames pass
    through untouched. Domains with no message-dependent rule keep the plain
    opaque tunnel."""
    # One relay loop for both modes: with no message-dependent rule the guard
    # is None and the tunnel stays byte-opaque (feed() can then never raise).
    guard = WebSocketClientGuard() if websocket_inspection_required(policy, host) else None
    try:
        if initial_client_bytes:
            cleared = guard.feed(initial_client_bytes) if guard else initial_client_bytes
            if cleared:
                upstream.sendall(cleared)
        sockets = [client, upstream]
        while True:
            readable, _, errors = select.select(sockets, [], sockets, IDLE_TIMEOUT)
            if errors or not readable:
                return
            for source in readable:
                data = source.recv(BUFFER_SIZE)
                if not data:
                    return
                if source is client:
                    cleared = guard.feed(data) if guard else data
                    if cleared:
                        upstream.sendall(cleared)
                else:
                    client.sendall(data)
    except WebSocketDenied as denied:
        append_network_event(protocol, "MESSAGE", host, port, path, "", False, denied.reason)
        try:
            client.sendall(_websocket_close_frame(1008, denied.reason))
        except OSError:
            pass
    finally:
        upstream.close()
        client.close()


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Cap concurrent connections. Each handler may buffer up to MAX_BODY_BYTES
    for inspection, so without a cap the untrusted agent could open many large
    POSTs at once and OOM the proxy — its only sanctioned network path."""

    def process_request(self, request, client_address):
        if not CONNECTION_SLOTS.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # The slot is normally released by process_request_thread; if the
            # handler thread could not even start (e.g. resource exhaustion),
            # release here or the slot leaks and the proxy eventually drops
            # every connection — the agent's only network path.
            CONNECTION_SLOTS.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            CONNECTION_SLOTS.release()


def main() -> int:
    BoundedThreadingHTTPServer((HOST, PORT), ProxyHandler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
