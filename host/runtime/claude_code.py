"""Claude Code runtime adapter.

Claude Code does not expose Codex's stdio JSON-RPC app-server. The supported
automation surface is the CLI/Agent SDK: print mode, stream-json I/O, and
resumable sessions. This module wraps that process shape behind the same small
contract the orchestrator needs: account status, start/complete OAuth login,
run one turn, and close the running process for task kills.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import queue
import re
import subprocess
import threading
import time
from typing import Any, Callable

DEFAULT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/run-claude-code"]
DEFAULT_ACCOUNT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/read-claude-account"]
AGENT_CWD = "/mnt/trustyclaw-agent/agent-home"
ACCOUNT_HELPER_TIMEOUT_SECONDS = 15
STATUS_TIMEOUT_SECONDS = 45
USAGE_TIMEOUT_SECONDS = 30
LOGIN_START_TIMEOUT_SECONDS = 30
LOGIN_URL_RE = re.compile(r"If the browser didn't open, visit: (https://\S+)")
USAGE_SESSION_RE = re.compile(r"(?m)^Current session:\s*(\d+(?:\.\d+)?)%\s+used\s*$")
USAGE_WEEK_RE = re.compile(r"(?m)^Current week \(all models\):\s*(\d+(?:\.\d+)?)%\s+used\s+·\s+resets\s+(.+?)\s*$")

_login_process: "ClaudeLoginProcess | None" = None
_login_lock = threading.Lock()


class ClaudeCodeError(RuntimeError):
    pass


class ClaudeTimeout(ClaudeCodeError):
    pass


@dataclass(frozen=True)
class ClaudeLogin:
    login_url: str


class ClaudeCodeSession:
    """Owns at most one running Claude CLI process.

    start()/alive() exist to satisfy the orchestrator's pooled-server contract;
    the actual Claude process is spawned per turn because Claude's resumable
    CLI sessions are persisted on disk.
    """

    def __init__(self, command: list[str] | None = None) -> None:
        self._command = command or DEFAULT_COMMAND
        self._proc: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._closed = False

    def start(self, init_timeout: float = 60.0) -> None:
        self._closed = False

    def alive(self) -> bool:
        return not self._closed and (self._proc is None or self._proc.poll() is None)

    def close(self) -> None:
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
        self._proc = None

    def run(
        self,
        input_message: str,
        session_id: str | None,
        steer_messages: Callable[[], list[str]],
        on_message: Callable[[str], None],
        steer_delivered: Callable[[str], None],
    ) -> tuple[str, str]:
        argv = [
            *self._command,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "bypassPermissions",
            "--safe-mode",
            "--strict-mcp-config",
        ]
        if session_id:
            argv.extend(["--resume", session_id])
        self._messages = queue.Queue()
        self._stderr_tail.clear()
        self._proc = subprocess.Popen(
            argv,
            cwd=_subprocess_cwd(self._command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._send_user_message(input_message)
        outstanding_user_messages = 1
        last_message = ""
        result_session_id = session_id
        while True:
            for steer in steer_messages():
                self._send_user_message(steer)
                outstanding_user_messages += 1
                steer_delivered(steer)
            try:
                message = self._messages.get(timeout=1.0)
            except queue.Empty:
                self._require_proc()
                continue
            if isinstance(message.get("session_id"), str):
                result_session_id = message["session_id"]
            if message.get("type") == "assistant":
                text = _assistant_text(message)
                if text:
                    last_message = text
                    on_message(text)
            if message.get("type") == "result":
                if message.get("subtype") != "success" or message.get("is_error"):
                    raise ClaudeCodeError(str(message.get("result") or message.get("subtype") or "Claude turn failed"))
                outstanding_user_messages = max(0, outstanding_user_messages - 1)
                if outstanding_user_messages:
                    continue
                final = str(message.get("result") or last_message or "Task completed.")
                self.close()
                if not result_session_id:
                    raise ClaudeCodeError("Claude result did not include a session_id")
                return result_session_id, final

    def _send_user_message(self, text: str) -> None:
        proc = self._require_proc()
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
        }) + "\n")
        proc.stdin.flush()

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            try:
                self._messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for line in proc.stderr:
            stripped = line.strip()
            if stripped:
                self._stderr_tail.append(stripped)

    def _require_proc(self) -> subprocess.Popen[str]:
        if self._proc is None or self._proc.poll() is not None:
            detail = "; ".join(self._stderr_tail)
            raise ClaudeCodeError(f"Claude Code process is not running{': ' + detail if detail else ''}")
        return self._proc


AgentServer = ClaudeCodeSession


class ClaudeLoginProcess:
    def __init__(self, command: list[str] | None = None, start_timeout: float = LOGIN_START_TIMEOUT_SECONDS) -> None:
        self._command = command or DEFAULT_COMMAND
        self._start_timeout = start_timeout
        self._proc: subprocess.Popen[str] | None = None

    def start(self) -> ClaudeLogin:
        self._proc = subprocess.Popen(
            [*self._command, "auth", "login", "--claudeai"],
            cwd=_subprocess_cwd(self._command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert self._proc.stdout is not None
        lines: queue.Queue[str | None] = queue.Queue()

        def read_stdout() -> None:
            assert self._proc is not None and self._proc.stdout is not None
            for line in self._proc.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=read_stdout, daemon=True).start()
        output = ""
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                chunk = lines.get(timeout=min(0.5, remaining))
            except queue.Empty:
                if self._proc.poll() is not None:
                    raise ClaudeCodeError("Claude OAuth login exited before returning a login URL")
                continue
            if chunk is None:
                raise ClaudeCodeError("Claude OAuth login exited before returning a login URL")
            output += chunk
            match = LOGIN_URL_RE.search(output)
            if match:
                return ClaudeLogin(login_url=match.group(1))
        self.close()
        raise ClaudeTimeout("Claude OAuth login did not return a login URL in time")

    def complete(self, code: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise ClaudeCodeError("Claude OAuth login has not been started")
        proc.stdin.write(code.strip() + "\n")
        proc.stdin.flush()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCodeError("Claude OAuth login did not complete after code submission") from exc
        if proc.returncode != 0:
            raise ClaudeCodeError("Claude OAuth login failed")

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()


def account_status() -> tuple[str, str | None, dict[str, Any] | None]:
    try:
        proc = subprocess.run(
            [*DEFAULT_COMMAND, "auth", "status", "--json"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=STATUS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "error", f"could not check Claude auth status: {exc!r}", None
    if proc.returncode != 0:
        return "awaiting_login", None, None
    try:
        status = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return "error", f"Claude auth status returned invalid JSON: {exc}", None
    if not isinstance(status, dict) or status.get("loggedIn") is not True:
        return "awaiting_login", None, None
    if status.get("authMethod") != "claude.ai":
        return "error", "Claude Code must be logged in with Claude.ai OAuth", None
    try:
        account = read_claude_account()
    except Exception as exc:
        return "error", f"could not read Claude account: {exc!r}", None
    if not account:
        return "error", "Claude auth is logged in but OAuth token metadata is unavailable", None
    _fill_claude_account_metadata(account, status)
    usage = read_claude_usage()
    if usage:
        account["claude_usage"] = usage
    return "active", None, account


def read_claude_usage(command: list[str] | None = None) -> dict[str, Any]:
    usage_command = command or DEFAULT_COMMAND
    try:
        proc = subprocess.run(
            [*usage_command, "-p", "/usage", "--output-format", "json"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=USAGE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    result = value.get("result")
    if not isinstance(result, str):
        return {}
    return _parse_claude_usage_result(result)


def _parse_claude_usage_result(result: str) -> dict[str, Any]:
    session_match = USAGE_SESSION_RE.search(result)
    week_match = USAGE_WEEK_RE.search(result)
    if not session_match or not week_match:
        return {}
    resets_at_text = week_match.group(2).strip()
    if not resets_at_text or "\n" in resets_at_text or len(resets_at_text) > 100:
        return {}
    return {
        "current_session_used_percent": _percent_value(session_match.group(1)),
        "weekly_used_percent": _percent_value(week_match.group(1)),
        "weekly_resets_at_text": resets_at_text,
    }


def _percent_value(value: str) -> int | float:
    parsed = float(value)
    return int(parsed) if parsed.is_integer() else parsed


def read_claude_account(command: list[str] | None = None) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            command or DEFAULT_ACCOUNT_COMMAND,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=ACCOUNT_HELPER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeCodeError(f"could not read Claude account: {exc}") from exc
    if proc.returncode != 0:
        return None
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCodeError("Claude account helper returned invalid JSON") from exc
    return value if isinstance(value, dict) and value.get("access_token_sha256") else None


def start_oauth_login() -> ClaudeLogin:
    global _login_process
    process = ClaudeLoginProcess()
    login = process.start()
    with _login_lock:
        if _login_process is not None:
            _login_process.close()
        _login_process = process
    return login


def complete_oauth_login(code: str) -> None:
    global _login_process
    with _login_lock:
        process = _login_process
    if process is None:
        raise ClaudeCodeError("Claude OAuth login has not been started")
    try:
        process.complete(code)
    finally:
        process.close()
        with _login_lock:
            if _login_process is process:
                _login_process = None


def close_login_process() -> None:
    global _login_process
    with _login_lock:
        if _login_process is not None:
            _login_process.close()
            _login_process = None


def run_turn(
    server: ClaudeCodeSession,
    input_message: str,
    session_id: str | None,
    steer_messages: Callable[[], list[str]],
    on_message: Callable[[str], None],
    steer_delivered: Callable[[str], None],
) -> tuple[str, str]:
    return server.run(input_message, session_id, steer_messages, on_message, steer_delivered)


def _subprocess_cwd(command: list[str]) -> str | None:
    # In production, the admin API cannot traverse the agent user's private
    # 0700 home. The sudo helper starts as root, cds there, and then drops to
    # trustyclaw-agent. Custom test commands still run from AGENT_CWD.
    return None if command == DEFAULT_COMMAND else AGENT_CWD


def _fill_claude_account_metadata(account: dict[str, Any], status: dict[str, Any]) -> None:
    if not account.get("email") and isinstance(status.get("email"), str) and status["email"].strip():
        account["email"] = status["email"].strip()
    if not account.get("organization_id") and isinstance(status.get("orgId"), str) and status["orgId"].strip():
        account["organization_id"] = status["orgId"].strip()
    if not account.get("account_id"):
        for key in ("accountId", "account_id", "userId", "userID", "user_id"):
            value = status.get(key)
            if isinstance(value, str) and value.strip():
                account["account_id"] = value.strip()
                break
    if not account.get("account_id") and isinstance(account.get("email"), str):
        # Claude's status output exposes email/org but not always the account
        # UUID. This field is user-facing metadata only; the proxy guard pins
        # on access_token_sha256.
        account["account_id"] = account["email"]
    plan_type = _extract_claude_plan_type(status)
    if plan_type:
        account["plan_type"] = plan_type


def _extract_claude_plan_type(status: dict[str, Any]) -> str | None:
    value = status.get("subscriptionType")
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _assistant_text(message: dict[str, Any]) -> str:
    payload = message.get("message")
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    return "".join(parts)
