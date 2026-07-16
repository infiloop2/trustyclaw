"""Small Chrome DevTools driver for the credential-bearing AWS smoke.

The fresh-host workflow runs with AWS credentials, so it must not install a
browser package from the network during the smoke step. GitHub's hosted runner
already includes Chrome. This module drives that browser over its local
DevTools WebSocket using only the Python standard library.

It deliberately implements only what the live smoke needs: evaluate JavaScript
and wait for a DOM condition. The normal mock UI suite remains Playwright-based.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import urlparse
import urllib.request


def chrome_executable() -> str:
    """Return a local Chrome/Chromium binary without downloading anything."""
    configured = os.environ.get("TRUSTYCLAW_SMOKE_CHROME")
    if configured:
        if Path(configured).is_file():
            return configured
        raise RuntimeError(f"TRUSTYCLAW_SMOKE_CHROME does not point to a file: {configured}")
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    cache = Path.home() / ".cache" / "ms-playwright"
    for pattern in ("chromium-*/chrome-linux/chrome", "chromium_headless_shell-*/chrome-linux/headless_shell"):
        for cached_candidate in sorted(cache.glob(pattern), reverse=True):
            if cached_candidate.is_file():
                return str(cached_candidate)
    raise RuntimeError(
        "fresh-host browser smoke requires the hosted runner's preinstalled Chrome; "
        "set TRUSTYCLAW_SMOKE_CHROME when running elsewhere"
    )


class _WebSocket:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise RuntimeError(f"unsupported DevTools WebSocket URL: {url}")
        port = parsed.port or 80
        self._socket = socket.create_connection((parsed.hostname, port), timeout=timeout)
        self._socket.settimeout(timeout)
        self._buffer = b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        self._socket.sendall(request)
        response = self._read_until(b"\r\n\r\n")
        status_line = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise RuntimeError(f"DevTools WebSocket upgrade failed: {status_line.decode(errors='replace')}")
        expected_accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        )
        headers = {
            name.strip().lower(): value.strip()
            for line in response.split(b"\r\n")[1:]
            if b":" in line
            for name, value in (line.split(b":", 1),)
        }
        if headers.get(b"sec-websocket-accept") != expected_accept:
            raise RuntimeError("DevTools WebSocket returned an invalid accept key")

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise RuntimeError("DevTools WebSocket closed during handshake")
            self._buffer += chunk
        end = self._buffer.index(marker) + len(marker)
        value, self._buffer = self._buffer[:end], self._buffer[end:]
        return value

    def _read_exact(self, length: int) -> bytes:
        while len(self._buffer) < length:
            chunk = self._socket.recv(max(65536, length - len(self._buffer)))
            if not chunk:
                raise RuntimeError("DevTools WebSocket closed unexpectedly")
            self._buffer += chunk
        value, self._buffer = self._buffer[:length], self._buffer[length:]
        return value

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length < 65536:
            header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def send_json(self, value: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(value, separators=(",", ":")).encode())

    def receive_json(self) -> dict[str, Any]:
        fragments = bytearray()
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if second & 0x80 else None
            payload = self._read_exact(length)
            if mask:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                raise RuntimeError("DevTools WebSocket closed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode not in (0x0, 0x1):
                continue
            fragments.extend(payload)
            if final:
                decoded = json.loads(fragments.decode())
                if not isinstance(decoded, dict):
                    raise RuntimeError(f"unexpected DevTools message: {decoded!r}")
                return decoded

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self._socket.close()


class BrowserTarget:
    """A page or sandboxed iframe DevTools target."""

    def __init__(self, websocket_url: str) -> None:
        self._websocket = _WebSocket(websocket_url)
        self._next_id = 1
        self.call("Runtime.enable")

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._websocket.send_json({"id": request_id, "method": method, "params": params or {}})
        while True:
            response = self._websocket.receive_json()
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(f"DevTools {method} failed: {response['error']}")
            result = response.get("result", {})
            return result if isinstance(result, dict) else {}

    def evaluate(self, expression: str) -> Any:
        response = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in response:
            raise RuntimeError(f"browser JavaScript failed: {response['exceptionDetails']}")
        result = response.get("result", {})
        return result.get("value") if isinstance(result, dict) else None

    def wait_for(self, expression: str, *, timeout: float = 30.0, description: str) -> Any:
        deadline = time.monotonic() + timeout
        last_value: Any = None
        while time.monotonic() < deadline:
            last_value = self.evaluate(expression)
            if last_value:
                return last_value
            time.sleep(0.1)
        raise AssertionError(f"browser timed out waiting for {description}; last value: {last_value!r}")

    def close(self) -> None:
        self._websocket.close()


class ChromeBrowser:
    """One headless page with JavaScript evaluation and condition polling."""

    def __init__(self, url: str) -> None:
        # Chrome can leave a short-lived crash/reporting child touching its
        # profile after the main browser process exits. The hosted runner is
        # ephemeral, so a late profile write must not turn a successful product
        # smoke into a teardown failure.
        self._profile = tempfile.TemporaryDirectory(
            prefix="trustyclaw-smoke-chrome-",
            ignore_cleanup_errors=True,
        )
        self._port = self._free_port()
        self._process = subprocess.Popen(
            [
                chrome_executable(),
                "--headless=new",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-size=1440,1000",
                f"--remote-debugging-port={self._port}",
                f"--user-data-dir={self._profile.name}",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._next_id = 1
        self._targets: list[BrowserTarget] = []
        try:
            self._websocket = _WebSocket(self._page_websocket_url(url))
            self.call("Runtime.enable")
            self.call("Page.enable")
            self.wait_for(
                f"location.href.replace(/\\/$/, '') === {json.dumps(url.rstrip('/'))}",
                description="the admin navigation to commit",
            )
            self.wait_for(
                "document.documentElement && (document.readyState === 'interactive' || document.readyState === 'complete')",
                description="the admin page to load",
            )
        except Exception:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self._profile.cleanup()
            raise

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            return int(candidate.getsockname()[1])

    def _page_websocket_url(self, wanted_url: str) -> str:
        deadline = time.monotonic() + 15
        endpoint = f"http://127.0.0.1:{self._port}/json/list"
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(f"Chrome exited before DevTools was ready ({self._process.returncode})")
            try:
                with urllib.request.urlopen(endpoint, timeout=1) as response:
                    targets = json.loads(response.read())
                pages = [target for target in targets if target.get("type") == "page"]
                selected = next(
                    (
                        target
                        for target in pages
                        if str(target.get("url", "")).rstrip("/") == wanted_url.rstrip("/")
                    ),
                    None,
                )
                if selected and selected.get("webSocketDebuggerUrl"):
                    return str(selected["webSocketDebuggerUrl"])
            except (OSError, json.JSONDecodeError) as exc:
                last_error = exc
            time.sleep(0.1)
        raise RuntimeError(f"Chrome DevTools did not expose a page: {last_error}")

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._websocket.send_json({"id": request_id, "method": method, "params": params or {}})
        while True:
            response = self._websocket.receive_json()
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(f"DevTools {method} failed: {response['error']}")
            result = response.get("result", {})
            return result if isinstance(result, dict) else {}

    def evaluate(self, expression: str) -> Any:
        response = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in response:
            detail = response["exceptionDetails"]
            raise RuntimeError(f"browser JavaScript failed: {detail}")
        result = response.get("result", {})
        return result.get("value") if isinstance(result, dict) else None

    def wait_for(
        self,
        expression: str,
        *,
        timeout: float = 30.0,
        description: str,
    ) -> Any:
        deadline = time.monotonic() + timeout
        last_value: Any = None
        while time.monotonic() < deadline:
            last_value = self.evaluate(expression)
            if last_value:
                return last_value
            time.sleep(0.1)
        raise AssertionError(f"browser timed out waiting for {description}; last value: {last_value!r}")

    def target(self, url_fragment: str, *, timeout: float = 30.0) -> BrowserTarget:
        """Attach to a sandboxed iframe target by URL fragment."""
        deadline = time.monotonic() + timeout
        endpoint = f"http://127.0.0.1:{self._port}/json/list"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(endpoint, timeout=1) as response:
                    targets = json.loads(response.read())
                selected = next(
                    (
                        item
                        for item in targets
                        if url_fragment in str(item.get("url", ""))
                        and item.get("webSocketDebuggerUrl")
                    ),
                    None,
                )
                if selected:
                    target = BrowserTarget(str(selected["webSocketDebuggerUrl"]))
                    self._targets.append(target)
                    return target
            except (OSError, json.JSONDecodeError):
                pass
            time.sleep(0.1)
        raise AssertionError(f"browser timed out waiting for target URL containing {url_fragment!r}")

    def close(self) -> None:
        for target in reversed(self._targets):
            target.close()
        self._websocket.close()
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self._profile.cleanup()

    def __enter__(self) -> "ChromeBrowser":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
