from __future__ import annotations

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
                "'email': 'user@example.com', 'orgId': 'org_123'}))"
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
                        "access_token_sha256": "hash",
                        "account_id": "user@example.com",
                        "email": "user@example.com",
                        "organization_id": "org_123",
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
                lambda: [],
                lambda _message: None,
                lambda _message: None,
            )
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.AGENT_CWD = original_cwd
        self.assertEqual(session_id, "default-helper-session")
        self.assertEqual(output, "OK")


if __name__ == "__main__":
    unittest.main()
