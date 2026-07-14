from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest

from host.runtime import claude_code


class ClaudeCodeTests(unittest.TestCase):
    def test_read_claude_account_reads_helper_json(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'account_id':'acct','organization_id':'org','access_token_sha256':'hash'}))",
        ]
        self.assertEqual(
            claude_code.read_claude_account(command),
            {"account_id": "acct", "organization_id": "org", "access_token_sha256": "hash"},
        )
        self.assertIsNone(claude_code.read_claude_account([sys.executable, "-c", "import sys; sys.exit(1)"]))

    def test_read_attested_identity_parses_helper_json(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'access_token_sha256':'hash','account_uuid':'acct','email':'op@example.com'}))",
        ]
        self.assertEqual(
            claude_code.read_attested_identity(command),
            {"access_token_sha256": "hash", "account_uuid": "acct", "email": "op@example.com"},
        )

    def test_read_attested_identity_passes_expected_token_hash(self) -> None:
        command = [
            sys.executable,
            "-c",
            (
                "import json, sys; "
                "assert sys.argv[-2:] == ['--expected-token-sha256', 'hash']; "
                "print(json.dumps({'access_token_sha256':'hash','account_uuid':'acct'}))"
            ),
        ]
        self.assertEqual(
            claude_code.read_attested_identity(command, expected_token_sha256="hash"),
            {"access_token_sha256": "hash", "account_uuid": "acct"},
        )

    def test_read_attested_identity_raises_with_helper_stderr_detail(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import sys; print('could not reach the Claude profile endpoint', file=sys.stderr); sys.exit(1)",
        ]
        with self.assertRaises(claude_code.ClaudeCodeError) as error:
            claude_code.read_attested_identity(command)
        self.assertIn("could not reach the Claude profile endpoint", str(error.exception))

    def test_read_attested_identity_rejects_incomplete_response(self) -> None:
        command = [sys.executable, "-c", "import json; print(json.dumps({'account_uuid': 'acct'}))"]
        with self.assertRaises(claude_code.ClaudeCodeError):
            claude_code.read_attested_identity(command)

    def test_account_status_maps_missing_helper_to_awaiting_login(self) -> None:
        original_command = claude_code.DEFAULT_COMMAND
        original_account = claude_code.DEFAULT_ACCOUNT_COMMAND
        claude_code.DEFAULT_COMMAND = [sys.executable, "-c", "import sys; sys.exit(1)", "--"]
        claude_code.DEFAULT_ACCOUNT_COMMAND = [sys.executable, "-c", "import sys; sys.exit(2)"]
        try:
            self.assertEqual(claude_code.account_status(), ("awaiting_login", None, None))
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.DEFAULT_ACCOUNT_COMMAND = original_account

    def test_account_status_requires_claude_ai_oauth_and_account_pin(self) -> None:
        original_command = claude_code.DEFAULT_COMMAND
        original_account = claude_code.DEFAULT_ACCOUNT_COMMAND
        claude_code.DEFAULT_COMMAND = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'}))",
            "--",
        ]
        claude_code.DEFAULT_ACCOUNT_COMMAND = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'account_id':'acct','organization_id':'org','access_token_sha256':'hash'}))",
        ]
        try:
            self.assertEqual(
                claude_code.account_status(),
                ("active", None, {"account_id": "acct", "organization_id": "org", "access_token_sha256": "hash"}),
            )
            claude_code.DEFAULT_COMMAND = [
                sys.executable,
                "-c",
                "import json; print(json.dumps({'loggedIn': True, 'authMethod': 'console'}))",
                "--",
            ]
            status, detail, account = claude_code.account_status()
            self.assertEqual(status, "error")
            self.assertIn("Claude.ai OAuth", detail or "")
            self.assertIsNone(account)
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.DEFAULT_ACCOUNT_COMMAND = original_account

    def test_account_status_fills_metadata_from_status_when_helper_has_only_token_hash(self) -> None:
        original_command = claude_code.DEFAULT_COMMAND
        original_account = claude_code.DEFAULT_ACCOUNT_COMMAND
        claude_code.DEFAULT_COMMAND = [
            sys.executable,
            "-c",
            (
                "import json; print(json.dumps({"
                "'loggedIn': True, 'authMethod': 'claude.ai', "
                "'email': 'user@example.com', 'orgId': 'org_123', "
                "'subscriptionType': 'max'}))"
            ),
            "--",
        ]
        claude_code.DEFAULT_ACCOUNT_COMMAND = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'access_token_sha256':'hash'}))",
        ]
        try:
            self.assertEqual(
                claude_code.account_status(),
                (
                    "active",
                    None,
                    {
                        # No account_id from CLI output: the trusted id is
                        # always set by the orchestrator's anchor/attestation.
                        "access_token_sha256": "hash",
                        "email": "user@example.com",
                        "organization_id": "org_123",
                        "plan_type": "max",
                    },
                ),
            )
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.DEFAULT_ACCOUNT_COMMAND = original_account

    def test_read_claude_usage_parses_usage_result_text(self) -> None:
        command = [
            sys.executable,
            "-c",
            (
                "import json; print(json.dumps({"
                "'type': 'result', "
                "'result': 'You are currently using your subscription to power your Claude Code usage\\n\\n"
                "Current session: 0% used · resets Jul 2, 1am (UTC)\\n"
                "Current week (all models): 12.5% used · resets Jul 2, 3:59pm (UTC)\\n"
                "Current week (Fable): 78% used · resets Jul 2, 3:59pm (UTC)'"
                "}))"
            ),
        ]

        year = datetime.now(timezone.utc).year
        self.assertEqual(claude_code.read_claude_usage(command), {
            "current_session_used_percent": 0,
            "current_session_resets_at": int(datetime(year, 7, 2, 1, tzinfo=timezone.utc).timestamp()),
            "weekly_used_percent": 12.5,
            "weekly_resets_at": int(datetime(year, 7, 2, 15, 59, tzinfo=timezone.utc).timestamp()),
            "fable_weekly_used_percent": 78,
            "fable_weekly_resets_at": int(datetime(year, 7, 2, 15, 59, tzinfo=timezone.utc).timestamp()),
        })

    def test_parse_claude_usage_assigns_early_january_reset_to_next_year(self) -> None:
        result = (
            "Current session: 1% used · resets Jan 1, 1am (UTC)\n"
            "Current week (all models): 2% used · resets Jan 2, 3:59pm (UTC)"
        )

        self.assertEqual(
            claude_code._parse_claude_usage_result(
                result,
                now=datetime(2026, 12, 31, 12, tzinfo=timezone.utc),
            ),
            {
                "current_session_used_percent": 1,
                "current_session_resets_at": int(datetime(2027, 1, 1, 1, tzinfo=timezone.utc).timestamp()),
                "weekly_used_percent": 2,
                "weekly_resets_at": int(datetime(2027, 1, 2, 15, 59, tzinfo=timezone.utc).timestamp()),
            },
        )

    def test_parse_claude_usage_drops_only_the_invalid_reset_date(self) -> None:
        result = (
            "Current session: 1% used · resets Feb 30, 1am (UTC)\n"
            "Current week (all models): 2% used · resets Mar 1, 3:59pm (UTC)"
        )

        self.assertEqual(
            claude_code._parse_claude_usage_result(
                result,
                now=datetime(2026, 2, 1, tzinfo=timezone.utc),
            ),
            {
                "current_session_used_percent": 1,
                "weekly_used_percent": 2,
                "weekly_resets_at": int(datetime(2026, 3, 1, 15, 59, tzinfo=timezone.utc).timestamp()),
            },
        )

    def test_parse_claude_usage_keeps_percent_when_reset_is_missing_or_foreign(self) -> None:
        # A window with no reset clause, or one in an unrecognized timezone,
        # still contributes its percent; only its resets_at is omitted.
        result = (
            "Current session: 3% used\n"
            "Current week (all models): 4% used · resets Jul 14, 4:30am (Asia/Calcutta)"
        )

        self.assertEqual(
            claude_code._parse_claude_usage_result(result, now=datetime(2026, 7, 13, tzinfo=timezone.utc)),
            {"current_session_used_percent": 3, "weekly_used_percent": 4},
        )

    def test_parse_claude_usage_windows_parse_independently(self) -> None:
        # A drifted session line must not blank the windows that still parse.
        result = (
            "Current session: no usage recorded yet\n"
            "Current week (all models): 5% used · resets Jul 14, 1am (UTC)\n"
            "Current week (Fable): 6% used · resets Jul 14, 1am (UTC)"
        )

        self.assertEqual(
            claude_code._parse_claude_usage_result(result, now=datetime(2026, 7, 13, tzinfo=timezone.utc)),
            {
                "weekly_used_percent": 5,
                "weekly_resets_at": int(datetime(2026, 7, 14, 1, tzinfo=timezone.utc).timestamp()),
                "fable_weekly_used_percent": 6,
                "fable_weekly_resets_at": int(datetime(2026, 7, 14, 1, tzinfo=timezone.utc).timestamp()),
            },
        )

    def test_parse_claude_usage_tracks_only_the_fable_model_week(self) -> None:
        # The Fable week is captured under fixed keys; other model weeks are
        # ignored, and the first Fable line wins.
        result = (
            "Current week (Fable): 6% used\n"
            "Current week (Fable): 99% used\n"
            "Current week (Opus): 42% used"
        )

        self.assertEqual(
            claude_code._parse_claude_usage_result(result, now=datetime(2026, 7, 13, tzinfo=timezone.utc)),
            {"fable_weekly_used_percent": 6},
        )

    def test_read_claude_usage_ignores_unknown_result_text(self) -> None:
        command = [sys.executable, "-c", "import json; print(json.dumps({'result': 'not usage'}))"]

        self.assertEqual(claude_code.read_claude_usage(command), {})

    def test_read_claude_usage_rejects_invalid_oauth_even_when_cli_exits_zero(self) -> None:
        command = [
            sys.executable,
            "-c",
            (
                "import json; print(json.dumps({"
                "'type': 'result', 'subtype': 'error', 'is_error': True, "
                "'result': 'Failed to authenticate. API Error: 401 Invalid authentication credentials'"
                "}))"
            ),
        ]

        with self.assertRaises(claude_code.ClaudeAuthenticationError):
            claude_code.read_claude_usage(command)

    def test_account_status_reads_helper_identity_metadata(self) -> None:
        original_command = claude_code.DEFAULT_COMMAND
        original_account = claude_code.DEFAULT_ACCOUNT_COMMAND
        claude_code.DEFAULT_COMMAND = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'}))",
            "--",
        ]
        claude_code.DEFAULT_ACCOUNT_COMMAND = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'access_token_sha256':'hash','account_id':'acct'}))",
        ]
        try:
            self.assertEqual(
                claude_code.account_status(),
                (
                    "active",
                    None,
                    {
                        "access_token_sha256": "hash",
                        "account_id": "acct",
                    },
                ),
            )
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.DEFAULT_ACCOUNT_COMMAND = original_account

    def test_account_status_errors_when_logged_in_but_token_hash_is_missing(self) -> None:
        original_command = claude_code.DEFAULT_COMMAND
        original_account = claude_code.DEFAULT_ACCOUNT_COMMAND
        claude_code.DEFAULT_COMMAND = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'}))",
            "--",
        ]
        claude_code.DEFAULT_ACCOUNT_COMMAND = [sys.executable, "-c", "import sys; sys.exit(1)"]
        try:
            status, detail, account = claude_code.account_status()
            self.assertEqual(status, "error")
            self.assertIn("OAuth token metadata", detail or "")
            self.assertIsNone(account)
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.DEFAULT_ACCOUNT_COMMAND = original_account

    def test_login_process_extracts_login_url(self) -> None:
        script = (
            "import sys, time; "
            "print('Opening browser to sign in...'); "
            "print('If the browser didn\\'t open, visit: https://claude.com/cai/oauth/authorize?code=true'); "
            "sys.stdout.flush(); "
            "time.sleep(1)"
        )
        process = claude_code.ClaudeLoginProcess([sys.executable, "-u", "-c", script, "--"])
        original_cwd = claude_code.AGENT_CWD
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                login = process.start()
                self.assertEqual(login.login_url, "https://claude.com/cai/oauth/authorize?code=true")
        finally:
            claude_code.AGENT_CWD = original_cwd
            process.close()

    def test_default_login_helper_does_not_require_admin_access_to_agent_home(self) -> None:
        script = (
            "import sys, time; "
            "print('If the browser didn\\'t open, visit: https://claude.com/cai/oauth/authorize?code=true'); "
            "sys.stdout.flush(); "
            "time.sleep(1)"
        )
        original_command = claude_code.DEFAULT_COMMAND
        original_cwd = claude_code.AGENT_CWD
        process = None
        try:
            claude_code.DEFAULT_COMMAND = [sys.executable, "-u", "-c", script, "--"]
            claude_code.AGENT_CWD = "/definitely/not-readable-by-admin"
            process = claude_code.ClaudeLoginProcess()
            login = process.start()
            self.assertEqual(login.login_url, "https://claude.com/cai/oauth/authorize?code=true")
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.AGENT_CWD = original_cwd
            if process is not None:
                process.close()

    def test_login_process_times_out_without_login_url(self) -> None:
        process = claude_code.ClaudeLoginProcess(
            [sys.executable, "-u", "-c", "import time; time.sleep(5)", "--"],
            start_timeout=0.1,
        )
        original_cwd = claude_code.AGENT_CWD
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                with self.assertRaises(claude_code.ClaudeTimeout):
                    process.start()
        finally:
            claude_code.AGENT_CWD = original_cwd
            process.close()

    def test_complete_oauth_login_always_closes_process(self) -> None:
        class FakeLoginProcess:
            completed_code: str | None = None
            closed = False

            def complete(self, code: str) -> None:
                self.completed_code = code

            def close(self) -> None:
                self.closed = True

        process = FakeLoginProcess()
        with claude_code._login_lock:
            original = claude_code._login_process
            claude_code._login_process = process  # type: ignore[assignment]
        try:
            claude_code.complete_oauth_login("CODE-123")
            self.assertEqual(process.completed_code, "CODE-123")
            self.assertTrue(process.closed)
            self.assertIsNone(claude_code._login_process)
        finally:
            with claude_code._login_lock:
                claude_code._login_process = original

    def test_close_login_process_clears_handle_when_close_fails(self) -> None:
        class FakeLoginProcess:
            def close(self) -> None:
                raise PermissionError("cannot signal helper")

        process = FakeLoginProcess()
        with claude_code._login_lock:
            original = claude_code._login_process
            claude_code._login_process = process  # type: ignore[assignment]
        try:
            with self.assertRaises(PermissionError):
                claude_code.close_login_process()
            self.assertIsNone(claude_code._login_process)
        finally:
            with claude_code._login_lock:
                claude_code._login_process = original

    def test_run_turn_waits_for_result_after_delivered_steer(self) -> None:
        script = r"""
import json, sys

session_id = "session-1"
for index, line in enumerate(sys.stdin, start=1):
    json.loads(line)
    text = "FIRST" if index == 1 else "STEERED"
    print(json.dumps({
        "type": "assistant",
        "session_id": session_id,
        "message": {"content": [{"type": "text", "text": text}]},
    }), flush=True)
    print(json.dumps({
        "type": "result",
        "subtype": "success",
        "session_id": session_id,
        "result": text,
    }), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        pending = ["steer"]
        delivered: list[str] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession([sys.executable, "-u", "-c", script])
                server.start()
                session_id, output = claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "opus",
                    "high",
                    lambda: [pending.pop(0)] if pending else [],
                    lambda _message: None,
                    delivered.append,
                )
        finally:
            claude_code.AGENT_CWD = original_cwd
        self.assertEqual(session_id, "session-1")
        self.assertEqual(delivered, ["steer"])
        self.assertEqual(output, "STEERED")

    def test_run_turn_discards_stale_messages_from_previous_process(self) -> None:
        script = r"""
import json, sys

json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "fresh-session",
    "result": "FRESH",
}), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession([sys.executable, "-u", "-c", script])
                server._messages.put({
                    "type": "result",
                    "subtype": "success",
                    "session_id": "stale-session",
                    "result": "STALE",
                })
                session_id, output = claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "opus",
                    "high",
                    lambda: [],
                    lambda _message: None,
                    lambda _message: None,
                )
        finally:
            claude_code.AGENT_CWD = original_cwd
        self.assertEqual(session_id, "fresh-session")
        self.assertEqual(output, "FRESH")

    def test_default_turn_helper_does_not_require_admin_access_to_agent_home(self) -> None:
        script = r"""
import json, sys

json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "default-helper-session",
    "result": "OK",
}), flush=True)
"""
        original_command = claude_code.DEFAULT_COMMAND
        original_cwd = claude_code.AGENT_CWD
        try:
            claude_code.DEFAULT_COMMAND = [sys.executable, "-u", "-c", script]
            claude_code.AGENT_CWD = "/definitely/not-readable-by-admin"
            server = claude_code.ClaudeCodeSession()
            session_id, output = claude_code.run_turn(
                server,
                "initial",
                None,
                "opus",
                "high",
                lambda: [],
                lambda _message: None,
                lambda _message: None,
            )
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.AGENT_CWD = original_cwd
        self.assertEqual(session_id, "default-helper-session")
        self.assertEqual(output, "OK")

    def test_task_launch_uses_managed_user_settings_without_safe_mode(self) -> None:
        script = r"""
import json, pathlib, sys

pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "argv-session",
    "result": "OK",
}), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                argv_path = Path(tmp) / "argv.json"
                server = claude_code.ClaudeCodeSession([sys.executable, "-u", "-c", script, str(argv_path)])
                claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "fable",
                    "ultracode",
                    lambda: [],
                    lambda _message: None,
                    lambda _message: None,
                )
                argv = json.loads(argv_path.read_text())
        finally:
            claude_code.AGENT_CWD = original_cwd

        self.assertIn("--setting-sources", argv)
        self.assertIn("user", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "fable")
        self.assertEqual(argv[argv.index("--effort") + 1], "ultracode")
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--safe-mode", argv)
        self.assertNotIn("--permission-mode", argv)


class ToolsMcpConfigTests(unittest.TestCase):
    def test_run_passes_the_bundled_tools_mcp_config(self) -> None:
        import json

        config = json.loads(claude_code.TOOLS_MCP_CONFIG)
        shim = config["mcpServers"]["trustyclaw"]
        self.assertEqual(shim["command"], "/usr/bin/python3")
        self.assertEqual(shim["args"], ["-m", "host.runtime.tools_mcp_shim"])
        self.assertEqual(shim["env"], {"PYTHONPATH": "/opt/trustyclaw-host"})

        # Echo the CLI argv back through the turn result to pin the flags the
        # runtime actually passes.
        script = r"""
import json, sys
sys.stdin.readline()
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "argv-session",
    "result": json.dumps(sys.argv[1:]),
}), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession([sys.executable, "-u", "-c", script])
                server.start()
                _session_id, output = claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "opus",
                    "high",
                    lambda: [],
                    lambda _message: None,
                    lambda _steer: None,
                )
        finally:
            claude_code.AGENT_CWD = original_cwd
        argv = json.loads(output)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--mcp-config", argv)
        self.assertEqual(argv[argv.index("--mcp-config") + 1], claude_code.TOOLS_MCP_CONFIG)
        # Safe mode would drop every non-SDK MCP server (verified against the
        # pinned CLI), silently disabling the bundled tools.
        self.assertNotIn("--safe-mode", argv)


if __name__ == "__main__":
    unittest.main()
