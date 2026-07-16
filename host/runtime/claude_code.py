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
from datetime import datetime, timedelta, timezone
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
# The bundled tools surface: Claude Code spawns the MCP shim as the agent
# user; the shim forwards to the host tools socket (see
# docs/architecture/tools/host-integration.md). --strict-mcp-config (below)
# makes this the only MCP server. With no tools enabled the shim lists
# nothing, so passing it unconditionally is harmless.
TOOLS_MCP_CONFIG = json.dumps(
    {
        "mcpServers": {
            "trustyclaw": {
                "command": "/usr/bin/python3",
                "args": ["-m", "host.runtime.tools_mcp_shim"],
                "env": {"PYTHONPATH": "/opt/trustyclaw-host"},
            }
        }
    }
)
ACCOUNT_HELPER_TIMEOUT_SECONDS = 15
# The attest helper makes one HTTPS round trip (10s inside the helper) on top
# of the file read, so it gets a larger budget than the plain account read.
ATTEST_HELPER_TIMEOUT_SECONDS = 20
STATUS_TIMEOUT_SECONDS = 45
USAGE_TIMEOUT_SECONDS = 30
LOGIN_START_TIMEOUT_SECONDS = 30
LOGIN_URL_RE = re.compile(r"If the browser didn't open, visit: (https://\S+)")
# Usage lines are parsed one window per line: a window header, a percent, and
# an optional reset time. Each piece is matched independently so one odd line
# (or a missing reset) degrades to a partial snapshot instead of no snapshot.
# All captured values are bounded because the text comes from an agent-run CLI.
USAGE_WINDOW_RE = re.compile(
    r"^\s*Current\s+(session|week\s*\(([^()]{1,40})\))\s*:\s*(.+?)\s*$", re.IGNORECASE
)
USAGE_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d{1,2})?)\s*%\s+used\b", re.IGNORECASE)
USAGE_RESETS_RE = re.compile(r"\bresets\s+(.{1,100}?)\s*$", re.IGNORECASE)
USAGE_RESET_RE = re.compile(r"^([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{1,2})(?::(\d{2}))?(am|pm)\s+\(UTC\)$", re.IGNORECASE)
USAGE_RESET_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_login_process: "ClaudeLoginProcess | None" = None
_login_lock = threading.Lock()


class ClaudeCodeError(RuntimeError):
    pass


class ClaudeAuthenticationError(ClaudeCodeError):
    pass


class ClaudeTimeout(ClaudeCodeError):
    pass


@dataclass(frozen=True)
class ClaudeLogin:
    login_url: str


class ClaudeCodeSession:
    """Owns at most one running Claude CLI process.

    start() exists to satisfy the orchestrator's server contract; the actual
    Claude process is spawned in run() because Claude's resumable CLI sessions
    are persisted on disk.
    """

    def __init__(self, command: list[str] | None = None, thread_id: str | None = None) -> None:
        self._command = command or DEFAULT_COMMAND
        self._thread_id = thread_id
        # The orchestrator sets this only for an app-created task. Claude's
        # append-system-prompt keeps it distinct from the app's current user
        # message and alongside the host's immutable CLAUDE.md instructions.
        self.app_instructions: str | None = None
        # Task turns run inside a systemd scope named after the host thread.
        # Keep the id separate from the command because the launcher's required
        # web-search decision must remain its first argument.
        self._proc: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=20)

    def start(self, init_timeout: float = 60.0) -> None:
        return

    def close(self) -> None:
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
        model: str,
        effort: str,
        steer_messages: Callable[[], list[str]],
        on_message: Callable[[str], None],
        steer_delivered: Callable[[str], None],
    ) -> tuple[str, str]:
        # State the operator's web-search decision to the launcher as its
        # required first argument; the launcher translates it into the WebSearch
        # deny (see host/bootstrap/helpers/run-claude-code.sh). The orchestrator
        # is the only side with a database role, so it reads the toggle here; the
        # proxy enforces the same toggle independently.
        from host.runtime import state

        enabled = state.read_claude_web_search()
        argv = [
            *self._command,
            f"web-search={'on' if enabled else 'off'}",
        ]
        if self._thread_id is not None:
            argv.extend(["--thread-scope", self._thread_id])
        argv.extend([
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--effort",
            effort,
            "--setting-sources",
            "user",
            # Deliberately no --safe-mode: the pinned CLI drops every
            # non-SDK MCP server in safe mode (verified empirically), which
            # would disable the bundled tools. --strict-mcp-config keeps the
            # MCP surface pinned to exactly the shim below, and the agent's
            # isolation comes from the OS boundaries and the installed
            # bypassPermissions user settings, not harness flags (see
            # docs/architecture/privilege-boundaries.md).
            "--strict-mcp-config",
            "--mcp-config",
            TOOLS_MCP_CONFIG,
        ])
        if self.app_instructions:
            argv.extend(["--append-system-prompt", self.app_instructions])
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


class ClaudeLoginProcess:
    def __init__(self, command: list[str] | None = None, start_timeout: float = LOGIN_START_TIMEOUT_SECONDS) -> None:
        self._command = command or DEFAULT_COMMAND
        self._start_timeout = start_timeout
        self._proc: subprocess.Popen[str] | None = None

    def start(self) -> ClaudeLogin:
        # Login runs no model turn; pass the launcher's required decision as
        # off (immaterial here, keeps the deny-by-default posture).
        self._proc = subprocess.Popen(
            [*self._command, "web-search=off", "auth", "login", "--claudeai"],
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
            # No model turn here; the launcher requires the decision, and off
            # keeps the deny-by-default posture (immaterial for a status check).
            [*DEFAULT_COMMAND, "web-search=off", "auth", "status", "--json"],
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
    return "active", None, account


def read_claude_usage(command: list[str] | None = None) -> dict[str, Any]:
    usage_command = command or DEFAULT_COMMAND
    try:
        proc = subprocess.run(
            # /usage runs no agent turn; pass the launcher's required decision
            # as off (immaterial here, keeps the deny-by-default posture).
            [*usage_command, "web-search=off", "-p", "/usage", "--output-format", "json"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=USAGE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeCodeError(str(exc)) from exc
    if proc.returncode != 0:
        _raise_usage_probe_error("\n".join(part for part in (proc.stdout, proc.stderr) if part))
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCodeError(f"Claude usage probe returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ClaudeCodeError("Claude usage probe returned an invalid response")
    result = value.get("result")
    if value.get("is_error") is True or value.get("subtype") == "error":
        _raise_usage_probe_error(result if isinstance(result, str) else proc.stderr)
    if not isinstance(result, str):
        return {}
    return _parse_claude_usage_result(result)


def _raise_usage_probe_error(detail: Any) -> None:
    message = str(detail or "Claude usage probe failed").strip()[:500]
    normalized = message.lower()
    authentication_markers = (
        "failed to authenticate",
        "invalid authentication credentials",
        "invalid bearer",
        "oauth token has expired",
        "authentication_error",
        "api error: 401",
    )
    if any(marker in normalized for marker in authentication_markers):
        raise ClaudeAuthenticationError("Claude OAuth credentials are no longer valid")
    raise ClaudeCodeError(message)


def _parse_claude_usage_result(result: str, now: datetime | None = None) -> dict[str, Any]:
    """Extract usage windows from the CLI's human-readable /usage text.

    Recognizes ``Current session`` (``current_session_*``), ``Current week
    (all models)`` (``weekly_*``), and ``Current week (Fable)``
    (``fable_weekly_*``); other model-specific week lines are ignored. Windows
    parse independently and the reset time is optional per window, so a partial
    or drifted response yields whatever parsed instead of an empty snapshot;
    the first line per window wins."""
    captured_at = now or datetime.now(timezone.utc)
    usage: dict[str, Any] = {}
    for line in result.splitlines():
        window_match = USAGE_WINDOW_RE.match(line)
        if not window_match:
            continue
        week_label = window_match.group(2)
        if week_label is None:
            prefix = "current_session"
        elif week_label.strip().lower() == "all models":
            prefix = "weekly"
        elif week_label.strip().lower() == "fable":
            prefix = "fable_weekly"
        else:
            continue  # only the all-models and Fable weekly windows are tracked
        if f"{prefix}_used_percent" in usage:
            continue
        rest = window_match.group(3)
        percent_match = USAGE_PERCENT_RE.search(rest)
        if not percent_match:
            continue
        usage[f"{prefix}_used_percent"] = _percent_value(percent_match.group(1))
        resets_match = USAGE_RESETS_RE.search(rest)
        resets_at = _parse_usage_reset_at(resets_match.group(1), captured_at) if resets_match else None
        if resets_at is not None:
            usage[f"{prefix}_resets_at"] = resets_at
    return usage


def _parse_usage_reset_at(value: str, now: datetime) -> int | None:
    match = USAGE_RESET_RE.fullmatch(value)
    if not match:
        return None
    month = USAGE_RESET_MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    hour12 = int(match.group(3))
    minute = int(match.group(4) or 0)
    if hour12 < 1 or hour12 > 12 or minute > 59:
        return None
    hour = hour12 % 12 + (12 if match.group(5).lower() == "pm" else 0)
    try:
        reset_at = datetime(now.year, month, int(match.group(2)), hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None
    # The provider omits the year. A December capture can legitimately report
    # an early-January reset, while a recent stale snapshot remains overdue.
    if reset_at < now - timedelta(days=183):
        try:
            reset_at = reset_at.replace(year=now.year + 1)
        except ValueError:
            return None
    return int(reset_at.timestamp())


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


def read_attested_identity(
    command: list[str] | None = None, expected_token_sha256: str | None = None
) -> dict[str, Any]:
    """Server-attested identity of the agent's current Claude OAuth token.

    The root helper reads the agent credential file and asks
    api.anthropic.com/api/oauth/profile who the token belongs to, so the
    returned identity (account_uuid, email, organization_uuid, plus the
    token's access_token_sha256) is bound to the token by the provider, not
    by agent-writable metadata. Raises ClaudeCodeError when the token cannot
    be attested: missing credentials, unreachable endpoint, or a rejected
    token."""
    try:
        argv = list(command or [*DEFAULT_ACCOUNT_COMMAND, "--attest"])
        if expected_token_sha256:
            argv.extend(["--expected-token-sha256", expected_token_sha256])
        proc = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=ATTEST_HELPER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeCodeError(f"could not attest Claude account: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ClaudeCodeError(detail or "Claude account attestation failed")
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCodeError("Claude account attestation returned invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("account_uuid"), str)
        or not value["account_uuid"]
        or not isinstance(value.get("access_token_sha256"), str)
        or not value["access_token_sha256"]
    ):
        raise ClaudeCodeError("Claude account attestation response is incomplete")
    return value


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
        process = _login_process
        _login_process = None
    if process is not None:
        process.close()


def run_turn(
    server: ClaudeCodeSession,
    input_message: str,
    session_id: str | None,
    model: str,
    effort: str,
    steer_messages: Callable[[], list[str]],
    on_message: Callable[[str], None],
    steer_delivered: Callable[[str], None],
) -> tuple[str, str]:
    return server.run(
        input_message,
        session_id,
        model,
        effort,
        steer_messages,
        on_message,
        steer_delivered,
    )


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
    # No account_id here on purpose: the trusted id always comes from the
    # stored anchor or a fresh server attestation (orchestrator._with_identity
    # replaces it on every active account), never from CLI output.
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
