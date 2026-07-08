"""Stdio JSON-RPC client for the Codex app-server.

App-servers are spawned through the root-owned ``run-codex-app-server`` sudo
helper, which drops to the ``trustyclaw-agent`` user and points all traffic at
the network policy proxy. Codex persists its login and threads under the agent
home, so separate processes share state: status checks and logins use
short-lived servers, while task turns run on per-thread servers the
orchestrator keeps warm between tasks.

A device-code login only completes while the app-server that started it keeps
polling, so ``start_device_login`` parks its server in ``_login_server``. The
status poller is the sole reader of that parked server: it drives the login
forward and records each completed login in ``_completed_logins`` for the
orchestrator to capture. The parked server lives until the orchestrator captures
the completed login, a new login starts, or an operator reset closes it. Status
probes never close it: agent-side credentials can look active while an operator
flow is still pending.

The ``account/login/completed`` notification (and ``account/read``) carry no
ChatGPT account id on this app-server protocol version; the id lives only in the
login tokens the CLI just wrote, as a provider-signed ``chatgpt_account_id``
claim. So the moment the poller first observes a completed login it reads that
id through the root ``read-codex-account-id`` helper and stores it in
``_completed_logins``. That read happens once, at completion; later retries only
look the stored id up. Reading once is what keeps the trust tight: the
agent-writable auth file is consulted only in the narrow window right after the
CLI writes it, never re-trusted on a later retry (see
``read_completed_device_login_account_id``).

The Codex app-server initialize request includes a fixed TrustyClaw client
version. Keep this stable unless TrustyClaw intentionally changes the client
contract it expects Codex to see during app-server initialization.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import queue
import subprocess
import threading
import time
from typing import Any, Callable

DEFAULT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/run-codex-app-server"]
DEFAULT_ACCOUNT_ID_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/read-codex-account-id"]
AGENT_CWD = "/mnt/trustyclaw-agent/agent-home"
ACCOUNT_ID_HELPER_TIMEOUT_SECONDS = 10
CLIENT_VERSION = "v1.0"

_login_server: "CodexAppServer | None" = None
_login_id: str | None = None
_completed_logins: dict[str, str | None] = {}
_login_lock = threading.Lock()


class CodexAppServerError(RuntimeError):
    pass


class CodexTimeout(CodexAppServerError):
    pass


@dataclass(frozen=True)
class CodexLogin:
    login_id: str
    verification_url: str
    user_code: str


class CodexAppServer:
    """Not thread-safe: a single driver thread owns start()/call()/read_message()
    (``_next_id`` and ``_pending`` are unsynchronized). The one sanctioned
    cross-thread call is close() from the kill-task path; the driver then surfaces
    the dead process as an error on its next call."""

    def __init__(self, command: list[str] | None = None) -> None:
        self._command = command or DEFAULT_COMMAND
        self._next_id = 1
        self._proc: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending: deque[dict[str, Any]] = deque()
        self._stderr_tail: deque[str] = deque(maxlen=20)

    def __enter__(self) -> "CodexAppServer":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def start(self, init_timeout: float = 60.0) -> None:
        try:
            self._proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise CodexAppServerError(f"failed to start Codex app-server command: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.call("initialize", _client_info(), timeout=init_timeout)
        self.notify("initialized")

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        # Closing stdin signals EOF, the app-server's normal shutdown path. This
        # is the reliable lever: the process is spawned through sudo and may run
        # as root/agent, so a SIGTERM from an unprivileged service user can raise
        # PermissionError — terminate()/kill() are best-effort fallbacks only.
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            for stop in (proc.terminate, proc.kill):
                try:
                    stop()
                    proc.wait(timeout=5)
                    break
                except (subprocess.TimeoutExpired, PermissionError, ProcessLookupError, OSError):
                    continue
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            try:
                self._messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _read_stderr(self) -> None:
        # Drain stderr (so the child never blocks on a full pipe) and keep the
        # tail for error reporting.
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for line in proc.stderr:
            stripped = line.strip()
            if stripped:
                self._stderr_tail.append(stripped)

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    def notify(self, method: str) -> None:
        proc = self._require_proc()
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"method": method}) + "\n")
        proc.stdin.flush()

    def call(self, method: str, params: dict[str, Any], *, timeout: float = 60.0) -> Any:
        proc = self._require_proc()
        request_id = self._next_id
        self._next_id += 1
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        # Notifications that arrive before our response are kept for read_message.
        while True:
            message = self._next_message(timeout)
            if message.get("id") != request_id:
                self._pending.append(message)
                continue
            if "error" in message:
                raise CodexAppServerError(message["error"].get("message", "Codex app-server request failed"))
            return message.get("result")

    def read_message(self, *, timeout: float = 60.0) -> dict[str, Any]:
        if self._pending:
            return self._pending.popleft()
        return self._next_message(timeout)

    def collect_completed_logins(self) -> set[str]:
        """Consume successful account/login/completed notifications, returning the
        login ids that completed. The notification carries no account id, so the
        trusted id is read separately (see read_completed_device_login_account_id)."""
        completed: set[str] = set()
        pending: deque[dict[str, Any]] = deque()
        while self._pending:
            message = self._pending.popleft()
            if message.get("method") == "account/login/completed":
                params = message.get("params")
                if isinstance(params, dict) and params.get("success") is True:
                    login_id = params.get("loginId")
                    if isinstance(login_id, str) and login_id:
                        completed.add(login_id)
                continue
            pending.append(message)
        self._pending = pending
        return completed

    def _next_message(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._require_proc()
                raise CodexTimeout("timed out waiting for Codex app-server")
            try:
                return self._messages.get(timeout=min(0.25, remaining))
            except queue.Empty:
                self._require_proc()

    def _require_proc(self) -> subprocess.Popen[str]:
        proc = self._proc
        if proc is None:
            raise CodexAppServerError("Codex app-server was not started")
        returncode = proc.poll()
        if returncode is not None:
            raise CodexAppServerError(f"Codex app-server exited with status {returncode}")
        return proc


AgentServer = CodexAppServer


def _client_info() -> dict[str, dict[str, str]]:
    return {"clientInfo": {"name": "trustyclaw-host", "version": CLIENT_VERSION}}


def account_status() -> tuple[str, str | None, dict[str, Any] | None]:
    """Return (status, detail, account metadata). detail is set only for "error"."""
    # Bounded timeouts: only the background poller calls this, but a Codex
    # app-server that cannot start (e.g. its startup traffic is denied by a
    # restrictive policy) must not wedge the poller — it resolves to "error"
    # with a detail until conditions improve. The init timeout leaves room for
    # a cold Node start on a small instance.
    login_server = _current_login_server()
    if login_server is not None:
        return _login_server_status(login_server)

    server = CodexAppServer()
    try:
        server.start(init_timeout=45)
        return _account_status_from_server(server)
    except CodexAppServerError as exc:
        return _codex_status_error(exc, server)
    finally:
        server.close()


def _current_login_server() -> "CodexAppServer | None":
    with _login_lock:
        server = _login_server
    if server is None:
        return None
    if server.alive():
        return server
    dead_server = _pop_login_server_if_current(server)
    if dead_server is not None:
        dead_server.close()
    return None


def _login_server_status(server: "CodexAppServer") -> tuple[str, str | None, dict[str, Any] | None]:
    # The status poller is the only reader of the parked login server, so it also
    # drains the account/login/completed notifications that
    # read_completed_device_login_account_id later looks up. collect is
    # destructive, so record whatever completed before returning.
    status = _account_status_from_server(server)
    completed = server.collect_completed_logins()
    if completed:
        # Capture the trusted account id now, at the moment completion is first
        # observed, so an agent that later swaps the (agent-writable) auth file
        # cannot get a different account anchored under the operator-approved
        # login id on a retry. A miss is recorded as None and fails closed at
        # capture, so the operator re-logs in rather than trusting whatever
        # tokens appear on a later cycle.
        try:
            account_id = read_codex_account_id()
        except CodexAppServerError:
            account_id = None
        with _login_lock:
            if _login_server is server:
                for login_id in completed:
                    _completed_logins[login_id] = account_id
    return status


def _account_status_from_server(server: "CodexAppServer") -> tuple[str, str | None, dict[str, Any] | None]:
    try:
        result = server.call("account/read", {"refreshToken": False}, timeout=15)
        if not isinstance(result, dict):
            raise CodexAppServerError("Codex account/read returned invalid result")
        account = result.get("account")
        if account:
            account_id = read_codex_account_id()
            if account_id:
                account_metadata = _safe_account_metadata(account if isinstance(account, dict) else {})
                account_metadata["account_id"] = account_id
                try:
                    rate_limits = _safe_rate_limits_metadata(server.call("account/rateLimits/read", {}, timeout=15))
                except CodexAppServerError:
                    # Usage is optional metadata. An account without a proxy
                    # pin (agent-side credentials awaiting operator approval)
                    # cannot reach the guarded usage endpoint; that must still
                    # classify as a readable account, not a runtime error.
                    rate_limits = {}
                if rate_limits:
                    account_metadata["codex_usage"] = rate_limits
                return "active", None, account_metadata
            raise CodexAppServerError("Codex account/read returned an account without a supported account id")
        return "awaiting_login", None, None
    except CodexAppServerError as exc:
        return _codex_status_error(exc, server)


def _codex_status_error(
    exc: CodexAppServerError,
    server: "CodexAppServer",
) -> tuple[str, str | None, dict[str, Any] | None]:
    message = str(exc).lower()
    # Only treat specific "not logged in" phrasings as awaiting_login, so a
    # real failure that merely mentions auth infrastructure (e.g. "could not
    # reach auth.openai.com", "authorization server unreachable") surfaces as
    # an error with its detail instead of an impossible login prompt.
    login_markers = ("not logged in", "logged out", "login required", "must log in",
                     "no account", "unauthorized", "401")
    if any(marker in message for marker in login_markers):
        return "awaiting_login", None, None
    return "error", _error_detail(str(exc), server.stderr_tail()), None


def _error_detail(message: str, stderr: str) -> str:
    if not stderr:
        return message
    if stderr in message:
        return message
    return f"{message}; app-server stderr: {stderr}"


def _safe_account_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    metadata: dict[str, Any] = {}
    email = _string_field(value, "email")
    if email:
        metadata["email"] = email
    plan_type = _string_field(value, "planType")
    if plan_type:
        metadata["planType"] = plan_type
    return metadata


def _string_field(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if not isinstance(item, str):
        return None
    return item.strip() or None


def _rate_limit_scalar(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return value
    if isinstance(value, str):
        return value.strip() or None
    return None


def _safe_rate_limits_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    rate_limits = _safe_rate_limit_snapshot(value.get("rateLimits"))
    if rate_limits:
        result["rate_limits"] = rate_limits
    return result


def _safe_rate_limit_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("primary", "secondary"):
        window = _safe_rate_limit_window(value.get(key))
        if window:
            result[key] = window
    credits = _safe_credits_snapshot(value.get("credits"))
    if credits:
        result["credits"] = credits
    return result


def _safe_rate_limit_window(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for source_key, target_key in (
        ("usedPercent", "used_percent"),
        ("windowDurationMins", "window_duration_mins"),
        ("resetsAt", "resets_at"),
    ):
        item = _rate_limit_scalar(value.get(source_key))
        if item is not None:
            result[target_key] = item
    return result


def _safe_credits_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for source_key, target_key in (
        ("hasCredits", "has_credits"),
        ("unlimited", "unlimited"),
        ("balance", "balance"),
    ):
        item = _rate_limit_scalar(value.get(source_key))
        if item is not None:
            result[target_key] = item
    return result


def read_codex_account_id(command: list[str] | None = None) -> str | None:
    try:
        proc = subprocess.run(
            command or DEFAULT_ACCOUNT_ID_COMMAND,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=ACCOUNT_ID_HELPER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexAppServerError(f"could not read Codex account id: {exc}") from exc
    if proc.returncode != 0:
        return None
    account_id = proc.stdout.strip()
    return account_id or None


def start_device_login() -> CodexLogin:
    global _login_id, _login_server
    server = CodexAppServer()
    server.start()
    try:
        result = server.call("account/login/start", {"type": "chatgptDeviceCode"}, timeout=30)
        if result.get("type") != "chatgptDeviceCode":
            raise CodexAppServerError("Codex did not return a device-code login flow")
    except BaseException:
        server.close()
        raise
    with _login_lock:
        old_server = _login_server
        _login_server = server
        _login_id = result["loginId"]
        _completed_logins.clear()
    if old_server is not None:
        old_server.close()
    return CodexLogin(
        login_id=result["loginId"],
        verification_url=result["verificationUrl"],
        user_code=result["userCode"],
    )


def _pop_login_server() -> "CodexAppServer | None":
    global _login_id, _login_server
    with _login_lock:
        server = _login_server
        _login_server = None
        _login_id = None
        _completed_logins.clear()
    return server


def _pop_login_server_if_current(server: "CodexAppServer") -> "CodexAppServer | None":
    global _login_id, _login_server
    with _login_lock:
        if _login_server is not server:
            return None
        _login_server = None
        _login_id = None
        _completed_logins.clear()
    return server


def _pop_login_server_if_login(login_id: str) -> "CodexAppServer | None":
    global _login_id, _login_server
    with _login_lock:
        if _login_server is None or _login_id != login_id:
            return None
        server = _login_server
        _login_server = None
        _login_id = None
        _completed_logins.clear()
    return server


def current_login_server() -> "CodexAppServer | None":
    """Snapshot the parked login server so a caller (an operator reset) can later
    close that exact instance, never one a concurrent login has since parked."""
    with _login_lock:
        return _login_server


def read_completed_device_login_account_id(login_id: str) -> str | None:
    """Return the completed operator device login's account id.

    A stored OAuth row means the operator saw a device code, not that the login
    completed. First-account capture therefore requires the successful
    account/login/completed notification for that exact login id, observed by the
    status poller on the parked login server. That notification carries no
    account id (nor does account/read) on this app-server protocol version, so
    the poller reads it through the root helper (the provider-signed
    chatgpt_account_id claim) the instant it first sees the completion and stores
    it in ``_completed_logins``. This is a pure lookup of that captured id: a
    completion whose id read missed is stored as None and fails closed here, so a
    later agent swap of the auth file is never trusted. The residual swap window
    (between the CLI writing auth.json and the poller's capture) matches the
    Claude first-capture path, and the linked account is shown to the operator
    once pinned.
    """
    with _login_lock:
        if _login_server is None or _login_id != login_id:
            return None
        if login_id not in _completed_logins:
            return None
        account_id = _completed_logins[login_id]
    if not account_id:
        raise CodexAppServerError("Codex completed login did not include a supported account id")
    return account_id


def close_login_server() -> None:
    server = _pop_login_server()
    if server is not None:
        server.close()


def close_login_server_if_current(expected: "CodexAppServer | None") -> None:
    """Close the parked login server only if it is still ``expected`` (an operator
    reset that snapshotted it before clearing its OAuth record). A login that
    started during the reset has replaced the global, so it is left running."""
    if expected is None:
        return
    server = _pop_login_server_if_current(expected)
    if server is not None:
        server.close()


def close_completed_login_server(login_id: str) -> None:
    """Close the parked login server for a captured login, unless a newer login
    has replaced it under a different login id."""
    server = _pop_login_server_if_login(login_id)
    if server is not None:
        server.close()


def run_turn(
    server: CodexAppServer,
    input_message: str,
    thread_id: str | None,
    steer_messages: Callable[[], list[str]],
    on_message: Callable[[str], None],
    steer_delivered: Callable[[str], None],
) -> tuple[str, str]:
    """Run one task turn to completion. ``steer_messages`` returns the
    undelivered steers in order; each successfully delivered steer is passed
    to ``steer_delivered`` so the caller drops it from its queue. A steer that
    fails transiently stays in the queue and is retried on the next loop pass.
    Emits each completed agent message via on_message and returns
    (thread_id, final agent message)."""
    if thread_id:
        try:
            thread = server.call("thread/resume", {"threadId": thread_id, "cwd": AGENT_CWD}, timeout=30)["thread"]
        except CodexAppServerError:
            thread = _start_thread(server)
    else:
        thread = _start_thread(server)
    thread_id = str(thread["id"])
    turn = server.call(
        "turn/start",
        {"threadId": thread_id, "input": [{"type": "text", "text": input_message}]},
        timeout=30,
    )["turn"]
    turn_id = turn["id"]
    current_parts: list[str] = []
    last_message = ""
    while True:
        for steer in steer_messages():
            try:
                server.call(
                    "turn/steer",
                    {"threadId": thread_id, "expectedTurnId": turn_id, "input": [{"type": "text", "text": steer}]},
                    timeout=30,
                )
            except CodexAppServerError as exc:
                if "no active turn" in str(exc).lower():
                    # The turn is not steerable yet (turn/start returns before
                    # the turn is active server-side) or already over. The
                    # steer stays undelivered, so steer_messages() returns it
                    # again on the next loop pass (~1s); if the turn is
                    # actually over, turn/completed ends the loop and the
                    # undelivered steer is dropped — the documented behavior
                    # for a steer that races completion.
                    break
                raise
            steer_delivered(steer)
        try:
            # Short timeout so new steer messages are picked up promptly. There
            # is no overall turn deadline; a stuck turn is abandoned through
            # POST /v1/tasks/{task_id}/kill.
            message = server.read_message(timeout=1.0)
        except CodexTimeout:
            continue
        method = message.get("method")
        params = message.get("params", {})
        if method == "item/agentMessage/delta":
            current_parts.append(params.get("delta", ""))
        elif method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage":
                last_message = item.get("text") or "".join(current_parts)
                current_parts = []
                if last_message:
                    on_message(last_message)
        elif method == "turn/completed":
            turn = params.get("turn", {})
            if turn.get("status") == "completed":
                # Fall back to any deltas not yet flushed by an item/completed,
                # so a final message streamed as bare deltas is not lost.
                final = last_message or "".join(current_parts)
                return thread_id, final or "Task completed."
            error = turn.get("error") or {}
            raise CodexAppServerError(error.get("message", "Codex turn failed"))


def _start_thread(server: CodexAppServer) -> dict[str, Any]:
    return server.call(
        "thread/start",
        {
            "cwd": AGENT_CWD,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "developerInstructions": (
                "You are running inside TrustyClaw. Complete the operator task and "
                "return a concise final result."
            ),
        },
        timeout=30,
    )["thread"]
