from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch

from host.runtime import codex_app_server as codex_app_server_module
from host.runtime.codex_app_server import (
    CodexAppServer,
    CodexAppServerError,
    read_codex_account_id,
    run_turn,
)


# A scripted stand-in for the Codex app-server speaking the stdio JSON-RPC
# protocol. It interleaves notifications with responses: the first message
# delta is emitted BEFORE the turn/start response, which regression-tests that
# call() keeps notifications instead of dropping them.
FAKE_APP_SERVER = r"""
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({"id": msg["id"], "result": {}})
    elif method == "thread/start":
        send({"id": msg["id"], "result": {"thread": {"id": "thread_1"}}})
    elif method == "turn/start":
        send({"method": "item/agentMessage/delta", "params": {"delta": "Hel"}})
        send({"id": msg["id"], "result": {"turn": {"id": "turn_1"}}})
        send({"method": "item/agentMessage/delta", "params": {"delta": "lo"}})
        send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "Hello"}}})
        send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "Final answer"}}})
        send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
"""


# A turn whose final answer streams only as deltas and ends with turn/completed,
# with no terminating item/completed — run_turn must still return the deltas
# rather than the "Task completed." placeholder.
FAKE_DELTA_ONLY_SERVER = r"""
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({"id": msg["id"], "result": {}})
    elif method == "thread/start":
        send({"id": msg["id"], "result": {"thread": {"id": "thread_1"}}})
    elif method == "turn/start":
        send({"id": msg["id"], "result": {"turn": {"id": "turn_1"}}})
        send({"method": "item/agentMessage/delta", "params": {"delta": "partial "}})
        send({"method": "item/agentMessage/delta", "params": {"delta": "answer"}})
        send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
"""


# Rejects the first turn/steer with the transient "no active turn" error the
# real app-server returns when a steer races turn startup; the retried steer
# is accepted and acknowledged in the final message.
FAKE_STEER_RETRY_SERVER = r"""
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

steers = 0
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({"id": msg["id"], "result": {}})
    elif method == "thread/start":
        send({"id": msg["id"], "result": {"thread": {"id": "thread_1"}}})
    elif method == "turn/start":
        send({"id": msg["id"], "result": {"turn": {"id": "turn_1"}}})
    elif method == "turn/steer":
        steers += 1
        if steers == 1:
            send({"id": msg["id"], "error": {"message": "no active turn to steer"}})
        else:
            send({"id": msg["id"], "result": {}})
            send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "STEERED"}}})
            send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
"""


# Rejects every steer with a non-transient error: the turn must fail loudly.
FAKE_STEER_REJECT_SERVER = r"""
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({"id": msg["id"], "result": {}})
    elif method == "thread/start":
        send({"id": msg["id"], "result": {"thread": {"id": "thread_1"}}})
    elif method == "turn/start":
        send({"id": msg["id"], "result": {"turn": {"id": "turn_1"}}})
    elif method == "turn/steer":
        send({"id": msg["id"], "error": {"message": "steer rejected: malformed input"}})
"""


def fake_account_server(result: dict[str, object], rate_limits: dict[str, object] | None = None) -> str:
    result_json = json.dumps(result)
    rate_limits_json = json.dumps(rate_limits or {})
    return f"""
import json, sys

result = json.loads({result_json!r})
rate_limits = json.loads({rate_limits_json!r})

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({{"id": msg["id"], "result": {{}}}})
    elif method == "account/read":
        send({{"id": msg["id"], "result": result}})
    elif method == "account/rateLimits/read":
        send({{"id": msg["id"], "result": rate_limits}})
        break
"""


class CodexAppServerTests(unittest.TestCase):
    def account_status_with_result(
        self, result: dict[str, object], rate_limits: dict[str, object] | None = None
    ) -> tuple[str, str | None, dict[str, object] | None]:
        previous = codex_app_server_module.DEFAULT_COMMAND
        codex_app_server_module.DEFAULT_COMMAND = [sys.executable, "-u", "-c", fake_account_server(result, rate_limits)]
        try:
            return codex_app_server_module.account_status()
        finally:
            codex_app_server_module.DEFAULT_COMMAND = previous

    def test_initialize_sends_client_name_and_version(self) -> None:
        self.assertEqual(
            codex_app_server_module._client_info(),
            {"clientInfo": {"name": "trustyclaw-host", "version": "v1.0"}},
        )

    def test_account_status_reads_account_id_from_helper_for_active_chatgpt_account(self) -> None:
        # Real Codex 0.124.0 account/read returns email/plan/type, not the
        # account id. TrustyClaw stores only the public identity fields it uses.
        result = {"account": {"email": "dev@example.com", "planType": "pro", "type": "chatgpt"}}
        with patch("host.runtime.codex_app_server.read_codex_account_id", return_value="acct_123"):
            self.assertEqual(
                self.account_status_with_result(result),
                (
                    "active",
                    None,
                    {"account_id": "acct_123", "email": "dev@example.com", "planType": "pro"},
                ),
            )

    def test_account_status_reads_rate_limits_from_app_server(self) -> None:
        result = {
            "account": {
                "email": "dev@example.com",
                "planType": "pro",
                "refresh_token": "secret",
            }
        }
        rate_limits = {
            "rateLimits": {
                "limitId": "codex",
                "planType": "pro",
                "primary": {"usedPercent": 8, "windowDurationMins": 300, "resetsAt": 1782788897},
                "secondary": {"usedPercent": 11, "windowDurationMins": 10080, "resetsAt": 1783296254},
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "planType": "pro",
                    "primary": {"usedPercent": 8, "windowDurationMins": 300, "resetsAt": 1782788897},
                    "secondary": {"usedPercent": 11, "windowDurationMins": 10080, "resetsAt": 1783296254},
                    "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                },
                "codex_bengalfox": {
                    "limitId": "codex_bengalfox",
                    "limitName": "GPT-5.3-Codex-Spark",
                    "planType": "pro",
                    "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1782790263},
                    "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 1783377063},
                },
            },
        }
        with patch("host.runtime.codex_app_server.read_codex_account_id", return_value="acct_123"):
            self.assertEqual(
                self.account_status_with_result(result, rate_limits),
                (
                    "active",
                    None,
                    {
                        "account_id": "acct_123",
                        "email": "dev@example.com",
                        "planType": "pro",
                        "codex_usage": {
                            "rate_limits": {
                                "primary": {
                                    "used_percent": 8,
                                    "window_duration_mins": 300,
                                    "resets_at": 1782788897,
                                },
                                "secondary": {
                                    "used_percent": 11,
                                    "window_duration_mins": 10080,
                                    "resets_at": 1783296254,
                                },
                                "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                            },
                        },
                    },
                ),
            )

    def test_account_status_treats_empty_account_as_awaiting_login(self) -> None:
        for account in (None, {}, False):
            with self.subTest(account=account):
                self.assertEqual(
                    self.account_status_with_result({"account": account}),
                    ("awaiting_login", None, None),
                )

    def test_account_status_reports_fast_app_server_exit_and_stderr(self) -> None:
        previous = codex_app_server_module.DEFAULT_COMMAND
        codex_app_server_module.DEFAULT_COMMAND = [
            sys.executable,
            "-u",
            "-c",
            "import sys; sys.stderr.write('startup failed\\n'); sys.exit(7)",
        ]
        try:
            status, detail, account = codex_app_server_module.account_status()
        finally:
            codex_app_server_module.DEFAULT_COMMAND = previous
        self.assertEqual(status, "error")
        self.assertIn("exited with status 7", detail or "")
        self.assertIn("startup failed", detail or "")
        self.assertIsNone(account)

    def test_account_status_errors_for_account_when_helper_cannot_find_id(self) -> None:
        result = {"account": {"email": "dev@example.com", "planType": "pro", "type": "chatgpt"}}
        with patch("host.runtime.codex_app_server.read_codex_account_id", return_value=None):
            status, detail, account_id = self.account_status_with_result(result)
        self.assertEqual(status, "error")
        self.assertIn("without a supported account id", detail or "")
        self.assertIsNone(account_id)

    def test_read_codex_account_id_reads_helper_stdout(self) -> None:
        self.assertEqual(read_codex_account_id([sys.executable, "-c", "print(' acct_123 ')"]), "acct_123")
        self.assertIsNone(read_codex_account_id([sys.executable, "-c", "import sys; sys.exit(1)"]))

    def test_run_turn_emits_each_message_and_returns_the_last_one(self) -> None:
        messages: list[str] = []
        with CodexAppServer([sys.executable, "-u", "-c", FAKE_APP_SERVER]) as server:
            thread_id, output = run_turn(server, "do the task", None, lambda: [], messages.append, lambda _m: None)

        self.assertEqual(thread_id, "thread_1")
        self.assertEqual(messages, ["Hello", "Final answer"])
        self.assertEqual(output, "Final answer")

    def test_run_turn_returns_unflushed_deltas_on_completion(self) -> None:
        with CodexAppServer([sys.executable, "-u", "-c", FAKE_DELTA_ONLY_SERVER]) as server:
            _, output = run_turn(server, "do the task", None, lambda: [], lambda _m: None, lambda _m: None)
        self.assertEqual(output, "partial answer")

    def test_run_turn_retries_a_steer_that_raced_turn_startup(self) -> None:
        # "no active turn to steer" right after turn/start is transient; the
        # steer stays in the pending queue and is retried on the next loop
        # pass, not failed — and it is consumed exactly once, on delivery.
        pending = ["redirect"]
        with CodexAppServer([sys.executable, "-u", "-c", FAKE_STEER_RETRY_SERVER]) as server:
            _, output = run_turn(
                server, "do the task", None,
                lambda: list(pending), lambda _m: None, pending.remove,
            )
        self.assertEqual(output, "STEERED")
        self.assertEqual(pending, [])

    def test_run_turn_surfaces_a_non_transient_steer_error(self) -> None:
        pending = ["redirect"]
        with CodexAppServer([sys.executable, "-u", "-c", FAKE_STEER_REJECT_SERVER]) as server:
            with self.assertRaises(CodexAppServerError) as error:
                run_turn(
                    server, "do the task", None,
                    lambda: list(pending), lambda _m: None, pending.remove,
                )
        self.assertIn("malformed input", str(error.exception))
        # The rejected steer was never delivered, so it was never consumed.
        self.assertEqual(pending, ["redirect"])


if __name__ == "__main__":
    unittest.main()
