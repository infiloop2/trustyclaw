"""Hermes runtime adapter (AWS Bedrock inference).

Hermes (NousResearch's hermes-agent) runs on the same Bedrock provider and
credential as Pi. Its supported automation surface here is the headless
one-query API behind a stdin adapter: one process per prompt, quiet output,
approvals disabled (the OS/proxy boundary is the enforcement), fixed
terminal/file/bundled-tools toolsets with the host MCP shim connected, and
reported/resumed session ids. The launcher pins the provider and environment;
this adapter supplies only the prompt, model, and session selection.

Hermes has no mid-turn steering channel in this mode. Each API task maps to
exactly one Hermes process and model turn; later input starts a new task on
the same thread and resumes its stored Hermes session. The provider's
credential surface (operator paste, STS attestation) is shared with every
Bedrock-backed harness in ``host.runtime.admin_api.bedrock_credentials``.
"""

from __future__ import annotations

import re
import subprocess
import threading
from typing import Any, Callable

from host.runtime.admin_api import bedrock_credentials

DEFAULT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/run-hermes"]
AGENT_CWD = "/mnt/trustyclaw-agent/agent-home"
# Bounded by Hermes's own agent.max_turns; the wait is generous because one
# prompt can run many tool-using turns.
TURN_TIMEOUT_SECONDS = 45 * 60
# The captured id re-enters the CLI as the --resume value, so it must never
# look like a flag: require a leading alphanumeric.
SESSION_ID_RE = re.compile(r"^session_id:\s*([A-Za-z0-9][\S]*)\s*$", re.MULTILINE)
# The orchestrator talks to every provider module through one contract; the
# shared Bedrock connection satisfies the account side of it.
account_status = bedrock_credentials.account_status


class HermesAgentError(RuntimeError):
    pass


class HermesSession:
    """Owns at most one running Hermes chat process.

    start() exists to satisfy the orchestrator's server contract; each prompt
    spawns its own process in run() because the headless chat CLI is
    single-shot and sessions are persisted on disk under the agent home.
    """

    def __init__(self, command: list[str] | None = None, thread_id: str | None = None) -> None:
        self._command = command or DEFAULT_COMMAND
        self._thread_id = thread_id
        # The orchestrator sets this only for an app-created task; delivered
        # as an ephemeral system-prompt addition so it stays distinct from the
        # user message.
        self.app_instructions: str | None = None
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._closed = False

    def start(self, init_timeout: float = 60.0) -> None:
        return

    def close(self) -> None:
        with self._lock:
            self._closed = True
            proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

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
        # Hermes exposes no mid-turn steering channel. The API rejects steers
        # before they reach this shared runtime contract, so one task always
        # remains one process and one model turn.
        del effort, steer_messages, steer_delivered
        from host.runtime.core import state

        region = state.read_bedrock_region()
        if not region:
            raise HermesAgentError("the AWS Bedrock integration has no configured region")
        result_session_id, last_message = self._run_prompt(region, input_message, session_id, model)
        on_message(last_message)
        return result_session_id, last_message

    def _run_prompt(
        self, region: str, prompt: str, session_id: str | None, model: str
    ) -> tuple[str, str]:
        argv = [*self._command, f"region={region}"]
        if self._thread_id is not None:
            argv.extend(["--thread-scope", self._thread_id])
        if self.app_instructions and not session_id:
            # The headless chat CLI has no system-prompt flag that survives
            # Hermes's env handling, so host-validated app instructions are
            # prepended once, when the session starts; the session history
            # carries them on resume.
            prompt = f"[Host app instructions]\n{self.app_instructions}\n\n[User message]\n{prompt}"
        argv.extend(["--model", model])
        if session_id:
            argv.extend(["--resume", session_id])
        with self._lock:
            if self._closed:
                raise HermesAgentError("Hermes turn was closed")
            # No operator credential crosses this boundary: the launcher
            # injects the shared Bedrock routing identity and the network
            # proxy re-signs each allowed request with the operator's real key.
            self._proc = subprocess.Popen(
                argv,
                cwd=_subprocess_cwd(self._command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            proc = self._proc
        try:
            stdout, stderr = proc.communicate(prompt, timeout=TURN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            self.close()
            raise HermesAgentError("Hermes turn timed out") from exc
        finally:
            with self._lock:
                self._proc = None
        if self._closed:
            raise HermesAgentError("Hermes turn was closed")
        if proc.returncode != 0:
            detail = (stderr or stdout or "").strip()[:500]
            raise HermesAgentError(detail or f"Hermes exited with status {proc.returncode}")
        # --pass-session-id prints the session line to stderr; the answer text
        # is stdout.
        match = SESSION_ID_RE.search(stderr or "") or SESSION_ID_RE.search(stdout or "")
        new_session_id = match.group(1) if match else session_id
        if not new_session_id:
            raise HermesAgentError("Hermes did not report a session id")
        answer = _answer_text(stdout)
        if not answer:
            raise HermesAgentError("Hermes returned no answer text")
        return new_session_id, answer


def run_turn(
    server: HermesSession,
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


def _answer_text(stdout: str | None) -> str:
    """The final answer: stdout minus the session line and blank edges."""
    lines = [
        line for line in (stdout or "").splitlines()
        if not SESSION_ID_RE.fullmatch(line.strip())
    ]
    return "\n".join(lines).strip()


def _subprocess_cwd(command: list[str]) -> str | None:
    # In production, the admin API cannot traverse the agent user's private
    # 0700 home. The sudo helper starts as root, cds there, and then drops to
    # trustyclaw-agent. Custom test commands still run from AGENT_CWD.
    return None if command == DEFAULT_COMMAND else AGENT_CWD
