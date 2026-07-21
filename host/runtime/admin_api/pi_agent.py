"""Pi runtime adapter (AWS Bedrock inference).

Pi is the host's open-source harness: a minimal coding agent whose only
provider here is Amazon Bedrock in the operator's own AWS account. The
supported automation surface is Pi's RPC mode — newline-delimited JSON
commands and events over stdio — plus host-minted session ids for resumable
threads. This module wraps that process shape behind the same small contract
the orchestrator needs: account status, connect credentials, run one turn,
and close the running process for task kills. The provider's credential
surface (operator paste, STS attestation) is shared with every Bedrock-backed
harness in ``host.runtime.admin_api.bedrock_credentials``.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import uuid
from collections import deque
from typing import Any, Callable

from host.runtime.admin_api import bedrock_credentials, thread_scope

DEFAULT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/run-pi"]
AGENT_CWD = "/mnt/trustyclaw-agent/agent-home"
# The orchestrator talks to every provider module through one contract; the
# shared Bedrock connection satisfies the account side of it.
account_status = bedrock_credentials.account_status


class PiAgentError(RuntimeError):
    pass


class PiSession:
    """Owns at most one running Pi RPC process.

    start() exists to satisfy the orchestrator's server contract; the actual
    Pi process is spawned in run() because Pi's resumable sessions are
    persisted on disk under the host-minted session id.
    """

    def __init__(self, command: list[str] | None = None, thread_id: str | None = None) -> None:
        self._command = command or DEFAULT_COMMAND
        self._thread_id = thread_id
        # The orchestrator sets this only for an app-created task. Pi's
        # append-system-prompt keeps it distinct from the app's current user
        # message and alongside the host's immutable AGENTS.md instructions.
        self.app_instructions: str | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._proc: subprocess.Popen[str] | None = None
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=20)

    def start(self, init_timeout: float = 60.0) -> None:
        return

    def close(self) -> None:
        # The closed flag fences the kill-before-spawn race: run() spawns the
        # process only in run(), so a kill that lands after the orchestrator's
        # post-start status check but before the spawn must still cancel the
        # turn instead of letting a killed task run.
        with self._lock:
            self._closed = True
            proc = self._proc
        if proc is not None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Best-effort signal only: the production launcher runs as root,
                # so this unprivileged kill fails with EPERM and the root scope
                # teardown below is the real kill; a same-user command (tests)
                # just dies here. A signal failure must never escape close() —
                # the orchestrator keeps a thread fenced when close() raises.
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
            self._proc = None
        # Last resort after the runtime's own shutdown above: a killed turn
        # leaves its descendants (a shell still in a long command) in this
        # thread's systemd scope, keeping the scope's cgroup — and its name —
        # alive so the next task on this thread cannot recreate it. Free the
        # scope so close() returns only once the whole cgroup is gone; a clean
        # exit already emptied it, so this is then a no-op.
        thread_scope.stop_thread_scope(self._thread_id, self._command, DEFAULT_COMMAND)

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
        # State the operator's region decision to the launcher as its required
        # first argument; the launcher translates it into AWS_REGION (see
        # host/bootstrap/helpers/run-pi.sh). The orchestrator is the only side
        # with a database role, so it reads the policy here; the proxy
        # enforces the same region independently.
        from host.runtime.core import state

        region = state.read_bedrock_region()
        if not region:
            raise PiAgentError("the AWS Bedrock integration has no configured region")
        result_session_id = session_id or str(uuid.uuid4())
        argv = [
            *self._command,
            f"region={region}",
        ]
        if self._thread_id is not None:
            argv.extend(["--thread-scope", self._thread_id])
        argv.extend([
            "--mode",
            "rpc",
            "--model",
            model,
            "--thinking",
            effort,
            # The host mints the session id and passes it on every turn:
            # --session-id creates the session when missing and resumes it
            # otherwise, so the first and every later turn share one shape.
            "--session-id",
            result_session_id,
        ])
        if self.app_instructions:
            argv.extend(["--append-system-prompt", self.app_instructions])
        self._events = queue.Queue()
        self._stderr_tail.clear()
        # No operator credential crosses this boundary: the launcher injects
        # the shared Bedrock routing identity and the network proxy re-signs
        # each allowed request with the operator's real key.
        with self._lock:
            if self._closed:
                raise PiAgentError("Pi turn was closed")
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
        # One agent_settled per run Pi starts for us, and agent_settled fires
        # only once no queued continuation remains — so a prompt whose accept
        # ack precedes a counted settle on the ordered stdout stream is
        # covered by that settle, while an ack that arrives after the
        # outstanding settles were consumed hit an idle agent and starts one
        # more run. Two empirically established rules make the counting
        # exact: a "steer" prompt is delivered only into a live run (Pi
        # swallows a steer sent while idle) and a plain prompt only to a
        # fully idle agent (Pi rejects it mid-stream), so steers wait for
        # agent_start or the settle; and a settle only counts when an
        # agent_end arrived since the last counted one (Pi emits a bare
        # agent_start/agent_settled pair for a swallowed delivery, which
        # must not consume the real run's expectation).
        prompt_sequence = 0
        pending_acks: set[str] = set()
        expected_settles = 0
        run_active = False
        run_ended = False
        last_message = ""

        def send(text: str, steering: bool) -> None:
            nonlocal prompt_sequence
            prompt_sequence += 1
            pending_acks.add(f"p{prompt_sequence}")
            self._send_prompt(f"p{prompt_sequence}", text, steering)

        def idle() -> bool:
            return expected_settles == 0 and not pending_acks

        send(input_message, steering=False)
        while True:
            if run_active or idle():
                for steer in steer_messages():
                    if run_active:
                        send(steer, steering=True)
                    elif idle():
                        send(steer, steering=False)
                    else:
                        break  # the rest wait for this new run's agent_start
                    steer_delivered(steer)
            try:
                event = self._events.get(timeout=1.0)
            except queue.Empty:
                self._require_proc()
                continue
            event_type = event.get("type")
            if event_type == "response":
                if event.get("success") is False:
                    raise PiAgentError(str(event.get("error") or "Pi rejected the prompt"))
                identifier = event.get("id")
                if isinstance(identifier, str) and identifier in pending_acks:
                    pending_acks.discard(identifier)
                    if expected_settles == 0:
                        expected_settles = 1
            if event_type == "agent_start":
                run_active = True
            if event_type == "agent_end":
                run_ended = True
            if event_type == "message_end":
                text, error = _assistant_message(event)
                if error is not None:
                    raise PiAgentError(error)
                if text:
                    last_message = text
                    on_message(text)
            if event_type == "agent_settled":
                run_active = False
                if not run_ended:
                    continue  # a swallowed-delivery settle; the real run is still due
                run_ended = False
                expected_settles = max(0, expected_settles - 1)
                if expected_settles or pending_acks or steer_messages():
                    continue
                if not last_message:
                    raise PiAgentError("Pi settled without an assistant message")
                self.close()
                return result_session_id, last_message

    def _send_prompt(self, identifier: str, text: str, steering: bool = False) -> None:
        proc = self._require_proc()
        assert proc.stdin is not None
        command: dict[str, Any] = {"id": identifier, "type": "prompt", "message": text}
        if steering:
            # Required while the agent is streaming; ignored when it is idle,
            # where the prompt starts a new run.
            command["streamingBehavior"] = "steer"
        proc.stdin.write(json.dumps(command) + "\n")
        proc.stdin.flush()

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            try:
                self._events.put(json.loads(line))
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
            raise PiAgentError(f"Pi process is not running{': ' + detail if detail else ''}")
        return self._proc


def run_turn(
    server: PiSession,
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


def _assistant_message(event: dict[str, Any]) -> tuple[str, str | None]:
    """(assistant text, error) from a message_end event; non-assistant
    messages yield ("", None)."""
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return "", None
    stop_reason = message.get("stopReason")
    if stop_reason in ("error", "aborted"):
        detail = message.get("errorMessage")
        return "", str(detail) if isinstance(detail, str) and detail else f"Pi turn {stop_reason}"
    content = message.get("content")
    if not isinstance(content, list):
        return "", None
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    return "".join(parts), None
