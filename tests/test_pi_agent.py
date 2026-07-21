from __future__ import annotations

import sys
import tempfile
import unittest
from unittest.mock import patch

from host.runtime.admin_api import pi_agent, thread_scope


def _patch_region(region: str | None = "us-east-1"):  # type: ignore[no-untyped-def]
    from host.runtime.core import state

    return patch.object(state, "read_bedrock_region", return_value=region)


# A scripted fake Pi RPC process: acks every prompt, answers with one
# assistant message, and settles — the per-prompt shape the adapter counts.
RPC_SCRIPT = r"""
import json, sys

for index, line in enumerate(sys.stdin, start=1):
    command = json.loads(line)
    print(json.dumps({"id": command.get("id"), "type": "response", "command": "prompt", "success": True}), flush=True)
    print(json.dumps({"type": "agent_start"}), flush=True)
    text = "FIRST" if index == 1 else "STEERED"
    print(json.dumps({
        "type": "message_end",
        "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": text}]},
    }), flush=True)
    print(json.dumps({"type": "agent_end", "messages": [], "willRetry": False}), flush=True)
    print(json.dumps({"type": "agent_settled"}), flush=True)
"""


class PiSessionTests(unittest.TestCase):
    def run_turn(
        self,
        script: str,
        *,
        session_id: str | None = None,
        pending: list[str] | None = None,
    ) -> tuple[str, str, list[str], list[str]]:
        pending = pending if pending is not None else []
        delivered: list[str] = []
        streamed: list[str] = []
        original_cwd = pi_agent.AGENT_CWD
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pi_agent.AGENT_CWD = tmp
                server = pi_agent.PiSession([sys.executable, "-u", "-c", script])
                server.start()
                with _patch_region():
                    result_session_id, output = pi_agent.run_turn(
                        server,
                        "initial",
                        session_id,
                        "deepseek.v3.2",
                        "high",
                        lambda: [pending.pop(0)] if pending else [],
                        streamed.append,
                        delivered.append,
                    )
        finally:
            pi_agent.AGENT_CWD = original_cwd
        return result_session_id, output, delivered, streamed

    def test_run_returns_last_assistant_message_and_minted_session_id(self) -> None:
        session_id, output, _delivered, streamed = self.run_turn(RPC_SCRIPT)
        self.assertTrue(session_id)  # minted by the host on the first turn
        self.assertEqual(output, "FIRST")
        self.assertEqual(streamed, ["FIRST"])

    def test_run_reuses_the_stored_session_id(self) -> None:
        session_id, _output, _delivered, _streamed = self.run_turn(RPC_SCRIPT, session_id="session-42")
        self.assertEqual(session_id, "session-42")

    def test_run_waits_for_the_steered_runs_settle(self) -> None:
        # The initial run settles while the steer is in flight; the adapter
        # must wait for the steer's own run instead of returning FIRST.
        _sid, output, delivered, streamed = self.run_turn(RPC_SCRIPT, pending=["steer"])
        self.assertEqual(delivered, ["steer"])
        self.assertEqual(output, "STEERED")
        self.assertEqual(streamed, ["FIRST", "STEERED"])

    def test_run_command_shape_pins_the_launcher_contract(self) -> None:
        script = r"""
import json, sys
args = sys.argv[1:]
json.loads(sys.stdin.readline())
print(json.dumps({"id": "p1", "type": "response", "command": "prompt", "success": True}), flush=True)
print(json.dumps({"type": "agent_start"}), flush=True)
print(json.dumps({
    "type": "message_end",
    "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": json.dumps(args)}]},
}), flush=True)
print(json.dumps({"type": "agent_end", "messages": [], "willRetry": False}), flush=True)
print(json.dumps({"type": "agent_settled"}), flush=True)
"""
        _sid, output, _delivered, _streamed = self.run_turn(script, session_id="session-9")
        import json

        arguments = json.loads(output)
        self.assertEqual(arguments[0], "region=us-east-1")
        self.assertIn("--mode", arguments)
        self.assertEqual(arguments[arguments.index("--mode") + 1], "rpc")
        self.assertEqual(arguments[arguments.index("--model") + 1], "deepseek.v3.2")
        self.assertEqual(arguments[arguments.index("--thinking") + 1], "high")
        self.assertEqual(arguments[arguments.index("--session-id") + 1], "session-9")

    def test_run_fails_without_a_configured_region(self) -> None:
        server = pi_agent.PiSession([sys.executable, "-u", "-c", "pass"])
        from host.runtime.core import state

        with patch.object(state, "read_bedrock_region", return_value=None):
            with self.assertRaisesRegex(pi_agent.PiAgentError, "no configured region"):
                server.run("go", None, "deepseek.v3.2", "high", lambda: [], lambda _m: None, lambda _m: None)

    def test_a_close_before_the_spawn_cancels_the_turn(self) -> None:
        # A kill can land after the orchestrator's post-start status check but
        # before run() spawns the process; the closed fence must cancel the
        # turn instead of running it for a killed task.
        server = pi_agent.PiSession([sys.executable, "-u", "-c", "raise SystemExit('never spawned')"])
        server.close()
        from host.runtime.core import state

        with patch.object(state, "read_bedrock_region", return_value="us-east-1"):
            with self.assertRaisesRegex(pi_agent.PiAgentError, "closed"):
                server.run("go", None, "deepseek.v3.2", "high", lambda: [], lambda _m: None, lambda _m: None)

    def test_assistant_error_stop_reason_fails_the_turn(self) -> None:
        script = r"""
import json, sys
json.loads(sys.stdin.readline())
print(json.dumps({"id": "p1", "type": "response", "command": "prompt", "success": True}), flush=True)
print(json.dumps({
    "type": "message_end",
    "message": {"role": "assistant", "stopReason": "error", "errorMessage": "model exploded", "content": []},
}), flush=True)
"""
        with self.assertRaisesRegex(pi_agent.PiAgentError, "model exploded"):
            self.run_turn(script)

    def test_rejected_prompt_fails_the_turn(self) -> None:
        script = r"""
import json, sys
json.loads(sys.stdin.readline())
print(json.dumps({"id": "p1", "type": "response", "command": "prompt", "success": False, "error": "bad prompt"}), flush=True)
"""
        with self.assertRaisesRegex(pi_agent.PiAgentError, "bad prompt"):
            self.run_turn(script)

    def test_settle_without_assistant_message_fails_the_turn(self) -> None:
        script = r"""
import json, sys
json.loads(sys.stdin.readline())
print(json.dumps({"id": "p1", "type": "response", "command": "prompt", "success": True}), flush=True)
print(json.dumps({"type": "agent_start"}), flush=True)
print(json.dumps({"type": "agent_end", "messages": [], "willRetry": False}), flush=True)
print(json.dumps({"type": "agent_settled"}), flush=True)
"""
        with self.assertRaisesRegex(pi_agent.PiAgentError, "without an assistant message"):
            self.run_turn(script)

    def test_swallowed_delivery_settle_does_not_end_the_turn(self) -> None:
        # Pi emits a bare agent_start/agent_settled pair (no agent_end) when a
        # delivery is swallowed; only a settle after agent_end may count.
        script = r"""
import json, sys
json.loads(sys.stdin.readline())
print(json.dumps({"id": "p1", "type": "response", "command": "prompt", "success": True}), flush=True)
print(json.dumps({"type": "agent_start"}), flush=True)
print(json.dumps({"type": "agent_settled"}), flush=True)
print(json.dumps({"type": "agent_start"}), flush=True)
print(json.dumps({
    "type": "message_end",
    "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "REAL"}]},
}), flush=True)
print(json.dumps({"type": "agent_end", "messages": [], "willRetry": False}), flush=True)
print(json.dumps({"type": "agent_settled"}), flush=True)
"""
        _sid, output, _delivered, _streamed = self.run_turn(script)
        self.assertEqual(output, "REAL")

    def test_dead_process_surfaces_stderr_tail(self) -> None:
        script = r"""
import sys
sys.stdin.readline()
print("pi blew up", file=sys.stderr)
sys.exit(1)
"""
        with self.assertRaisesRegex(pi_agent.PiAgentError, "pi blew up"):
            self.run_turn(script)

    def test_thread_scope_is_separate_from_the_launcher_command(self) -> None:
        session = pi_agent.PiSession(command=["/bin/echo"], thread_id="mission_pursuit__ws-3")
        self.assertEqual(session._command, ["/bin/echo"])
        self.assertEqual(session._thread_id, "mission_pursuit__ws-3")
        self.assertIsNone(pi_agent._subprocess_cwd(pi_agent.DEFAULT_COMMAND))
        self.assertEqual(pi_agent._subprocess_cwd(session._command), pi_agent.AGENT_CWD)

    def test_close_stops_the_thread_scope_under_the_production_launcher(self) -> None:
        # A killed turn's scope keeps the thread name until its whole cgroup is
        # gone, so close() must stop the scope by name before the next task on
        # this thread recreates it.
        session = pi_agent.PiSession(
            command=pi_agent.DEFAULT_COMMAND, thread_id="stage-1-smoke-kill-pi"
        )
        with patch.object(thread_scope.subprocess, "run") as run:
            session.close()
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            [*thread_scope.STOP_COMMAND, "stage-1-smoke-kill-pi"],
        )

    def test_close_does_not_stop_a_scope_for_a_test_command_or_threadless_turn(self) -> None:
        for session in (
            pi_agent.PiSession(command=["/bin/echo"], thread_id="mission_pursuit__ws-3"),
            pi_agent.PiSession(command=pi_agent.DEFAULT_COMMAND, thread_id=None),
        ):
            with patch.object(thread_scope.subprocess, "run") as run:
                session.close()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
