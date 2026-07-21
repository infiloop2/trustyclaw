from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest.mock import patch

from host.runtime.admin_api import hermes_agent

# A scripted fake of the Hermes stdin adapter: one process per prompt, session
# id on stderr, answer text on stdout, and resume keeps the session id.
CHAT_SCRIPT = r"""
import json, sys
args = sys.argv[1:]
def value_after(flag):
    return args[args.index(flag) + 1] if flag in args else None
session_id = value_after("--resume") or "hermes-session-1"
prompt = sys.stdin.read()
print(f"session_id: {session_id}", file=sys.stderr)
print(json.dumps({"prompt": prompt, "args": args}))
"""


class HermesSessionTests(unittest.TestCase):
    def run_turn(
        self,
        script: str,
        *,
        session_id: str | None = None,
        pending: list[str] | None = None,
        app_instructions: str | None = None,
        input_message: str = "initial",
    ) -> tuple[str, str, list[str], list[str]]:
        pending = pending if pending is not None else []
        delivered: list[str] = []
        streamed: list[str] = []
        original_cwd = hermes_agent.AGENT_CWD
        from host.runtime.core import state

        try:
            with tempfile.TemporaryDirectory() as tmp:
                hermes_agent.AGENT_CWD = tmp
                server = hermes_agent.HermesSession([sys.executable, "-u", "-c", script])
                server.app_instructions = app_instructions
                server.start()
                with patch.object(state, "read_bedrock_region", return_value="us-east-1"):
                    result_session_id, output = hermes_agent.run_turn(
                        server,
                        input_message,
                        session_id,
                        "deepseek.v3.2",
                        "high",
                        lambda: [pending.pop(0)] if pending else [],
                        streamed.append,
                        delivered.append,
                    )
        finally:
            hermes_agent.AGENT_CWD = original_cwd
        return result_session_id, output, delivered, streamed

    def test_run_returns_the_reported_session_id_and_answer(self) -> None:
        session_id, output, _delivered, streamed = self.run_turn(CHAT_SCRIPT)
        self.assertEqual(session_id, "hermes-session-1")
        payload = json.loads(output)
        self.assertEqual(payload["prompt"], "initial")
        self.assertEqual(payload["args"][0], "region=us-east-1")
        self.assertIn("--model", payload["args"])
        self.assertNotIn("initial", payload["args"])
        self.assertNotIn("--resume", payload["args"])
        self.assertEqual(len(streamed), 1)

    def test_run_resumes_the_stored_session(self) -> None:
        session_id, output, _delivered, _streamed = self.run_turn(CHAT_SCRIPT, session_id="hermes-session-7")
        self.assertEqual(session_id, "hermes-session-7")
        payload = json.loads(output)
        self.assertEqual(payload["args"][payload["args"].index("--resume") + 1], "hermes-session-7")

    def test_run_does_not_poll_or_deliver_steers(self) -> None:
        _sid, output, delivered, streamed = self.run_turn(CHAT_SCRIPT, pending=["steer one"])
        self.assertEqual(delivered, [])
        self.assertEqual(len(streamed), 1)
        payload = json.loads(output)
        self.assertEqual(payload["prompt"], "initial")

    def test_app_instructions_prefix_only_the_new_session_prompt(self) -> None:
        _sid, output, _delivered, _streamed = self.run_turn(CHAT_SCRIPT, app_instructions="Be the app.")
        payload = json.loads(output)
        self.assertIn("[Host app instructions]", payload["prompt"])
        self.assertIn("Be the app.", payload["prompt"])
        self.assertIn("[User message]\ninitial", payload["prompt"])
        _sid, output, _delivered, _streamed = self.run_turn(
            CHAT_SCRIPT, session_id="hermes-session-1", app_instructions="Be the app."
        )
        self.assertEqual(json.loads(output)["prompt"], "initial")

    def test_a_leading_dash_prompt_is_delivered_verbatim_over_stdin(self) -> None:
        _sid, output, _delivered, _streamed = self.run_turn(
            CHAT_SCRIPT, input_message="--help me with this"
        )
        payload = json.loads(output)
        self.assertEqual(payload["prompt"], "--help me with this")
        self.assertNotIn("--help me with this", payload["args"])

    def test_a_flag_shaped_session_id_line_is_ignored(self) -> None:
        # A session id re-enters argv as --resume's value, so a reported id
        # that looks like a flag is never adopted; with no prior session the
        # turn fails instead.
        script = CHAT_SCRIPT.replace('or "hermes-session-1"', 'or "--toolsets=all"')
        with self.assertRaises(hermes_agent.HermesAgentError):
            self.run_turn(script)

    def test_run_fails_without_a_configured_region(self) -> None:
        server = hermes_agent.HermesSession([sys.executable, "-u", "-c", "pass"])
        from host.runtime.core import state

        with patch.object(state, "read_bedrock_region", return_value=None):
            with self.assertRaisesRegex(hermes_agent.HermesAgentError, "no configured region"):
                server.run(
                    "go", None, "deepseek.v3.2", "high",
                    lambda: [], lambda _m: None, lambda _m: None,
                )

    def test_nonzero_exit_surfaces_stderr_detail(self) -> None:
        script = r"""
import sys
print("API call failed after 3 retries: Connection error.", file=sys.stderr)
sys.exit(1)
"""
        with self.assertRaisesRegex(hermes_agent.HermesAgentError, "Connection error"):
            self.run_turn(script)

    def test_missing_session_id_fails_the_turn(self) -> None:
        script = r"""
print("an answer with no session line")
"""
        with self.assertRaisesRegex(hermes_agent.HermesAgentError, "session id"):
            self.run_turn(script)

    def test_empty_answer_fails_the_turn(self) -> None:
        script = r"""
import sys
print("session_id: hermes-session-1", file=sys.stderr)
"""
        with self.assertRaisesRegex(hermes_agent.HermesAgentError, "no answer text"):
            self.run_turn(script)

    def test_thread_scope_is_separate_from_the_launcher_command(self) -> None:
        session = hermes_agent.HermesSession(command=["/bin/echo"], thread_id="mission_pursuit__ws-3")
        self.assertEqual(session._command, ["/bin/echo"])
        self.assertEqual(session._thread_id, "mission_pursuit__ws-3")
        self.assertIsNone(hermes_agent._subprocess_cwd(hermes_agent.DEFAULT_COMMAND))
        self.assertEqual(hermes_agent._subprocess_cwd(session._command), hermes_agent.AGENT_CWD)


if __name__ == "__main__":
    unittest.main()
