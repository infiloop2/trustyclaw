from __future__ import annotations

import json
import sys
from typing import Any
import unittest
from unittest.mock import patch

from host.runtime.admin_api import codex_app_server as codex_app_server_module
from host.runtime.admin_api.codex_app_server import (
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
        assert msg["params"]["model"] == "gpt-5.6-sol"
        assert "effort" not in msg["params"]
        send({"id": msg["id"], "result": {"thread": {"id": "thread_1"}}})
    elif method == "thread/resume":
        assert msg["params"]["threadId"] == "thread_existing"
        assert msg["params"]["model"] == "gpt-5.6-sol"
        assert msg["params"]["developerInstructions"].endswith(
            "App instructions:\nUse only /agent/workspace."
        )
        assert "effort" not in msg["params"]
        send({"id": msg["id"], "result": {"thread": {"id": "thread_existing"}}})
    elif method == "turn/start":
        assert msg["params"]["model"] == "gpt-5.6-sol"
        assert msg["params"]["effort"] == "ultra"
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


def fake_account_server(
    result: dict[str, Any],
    rate_limits: dict[str, Any] | None = None,
    *,
    rate_limits_error: bool = False,
    refresh_error: str | None = None,
) -> str:
    result_json = json.dumps(result)
    rate_limits_json = json.dumps(rate_limits or {})
    rate_limits_error_json = json.dumps(rate_limits_error)
    refresh_error_json = json.dumps(refresh_error)
    return f"""
import json, sys

result = json.loads({result_json!r})
rate_limits = json.loads({rate_limits_json!r})
rate_limits_error = json.loads({rate_limits_error_json!r})
refresh_error = json.loads({refresh_error_json!r})
account_reads = 0

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({{"id": msg["id"], "result": {{}}}})
    elif method == "account/read":
        account_reads += 1
        assert msg["params"]["refreshToken"] is (account_reads > 1)
        if account_reads > 1 and refresh_error is not None:
            send({{"id": msg["id"], "error": {{"message": refresh_error}}}})
            break
        send({{"id": msg["id"], "result": result}})
    elif method == "account/rateLimits/read":
        if rate_limits_error:
            send({{"id": msg["id"], "error": {{"message": "denied by network policy"}}}})
        else:
            send({{"id": msg["id"], "result": rate_limits}})
            break
    """


def fake_login_server(
    result: dict[str, object],
    *,
    completed_login_id: str | None = None,
    completed_success: bool = True,
) -> str:
    # The real account/login/completed notification carries only loginId,
    # success, and error; it has no account id. The trusted id is read from the
    # login tokens through the root helper after completion.
    result_json = json.dumps(result)
    completed_json = json.dumps(
        {
            "loginId": completed_login_id,
            "success": completed_success,
            "error": None if completed_success else "failed",
        }
        if completed_login_id is not None
        else None
    )
    return f"""
import json, sys

result = json.loads({result_json!r})
completed = json.loads({completed_json!r})

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({{"id": msg["id"], "result": {{}}}})
    elif method == "account/login/start":
        send({{"id": msg["id"], "result": {{
            "type": "chatgptDeviceCode",
            "loginId": "login-1",
            "verificationUrl": "https://auth.openai.com/device",
            "userCode": "CODE-1"
        }}}})
    elif method == "account/read":
        if completed is not None:
            send({{"method": "account/login/completed", "params": completed}})
        send({{"id": msg["id"], "result": result}})
    elif method == "account/rateLimits/read":
        send({{"id": msg["id"], "result": {{}}}})
"""


def fake_login_server_with_delayed_account() -> str:
    return r"""
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

reads = 0
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({"id": msg["id"], "result": {}})
    elif method == "account/login/start":
        send({"id": msg["id"], "result": {
            "type": "chatgptDeviceCode",
            "loginId": "login-1",
            "verificationUrl": "https://auth.openai.com/device",
            "userCode": "CODE-1"
        }})
    elif method == "account/read":
        reads += 1
        if reads == 1:
            send({"method": "account/login/completed", "params": {
                "loginId": "login-1",
                "success": True,
                "error": None
            }})
            send({"id": msg["id"], "result": {"account": None}})
        else:
            send({"id": msg["id"], "result": {"account": {"email": "dev@example.com"}}})
"""


class FakeLoginServer:
    def __init__(
        self,
        account: object,
        rate_limits: dict[str, Any] | None = None,
        account_error: str | None = None,
        completed: set[str] | None = None,
        alive: bool = True,
    ) -> None:
        self.account = account
        self.account_error = account_error
        self.is_alive = alive
        self.rate_limits = rate_limits or {}
        self.completed = set(completed or set())
        self.calls: list[str] = []
        self.closed = False

    def alive(self) -> bool:
        return self.is_alive

    def call(self, method: str, params: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
        self.calls.append(method)
        if method == "account/read":
            if self.account_error is not None:
                raise CodexAppServerError(self.account_error)
            return {"account": self.account}
        if method == "account/rateLimits/read":
            return self.rate_limits
        raise AssertionError(f"unexpected method: {method}")

    def collect_completed_logins(self) -> set[str]:
        # Destructive drain, like the real CodexAppServer: a completion is
        # surfaced once and the status poller records it.
        completed = set(self.completed)
        self.completed = set()
        return completed

    def close(self) -> None:
        self.closed = True

    def stderr_tail(self) -> str:
        return ""


def park_login(server: Any, login_id: str = "login-x") -> None:
    with codex_app_server_module._login_lock:
        codex_app_server_module._parked_login = codex_app_server_module._ParkedLogin(
            server=server, login_id=login_id  # type: ignore[arg-type]
        )


def parked_server() -> Any:
    with codex_app_server_module._login_lock:
        parked = codex_app_server_module._parked_login
    return parked.server if parked is not None else None


class CodexAppServerTests(unittest.TestCase):
    def setUp(self) -> None:
        # The live-validation verdict is a process-global memo; isolate tests.
        codex_app_server_module.clear_live_validation_failure()
        self.addCleanup(codex_app_server_module.clear_live_validation_failure)

    def tearDown(self) -> None:
        codex_app_server_module.close_login_server()

    def account_status_with_result(
        self,
        result: dict[str, Any],
        rate_limits: dict[str, Any] | None = None,
        *,
        rate_limits_error: bool = False,
        refresh_error: str | None = None,
        force_provider_probe: bool = False,
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        previous = codex_app_server_module.DEFAULT_COMMAND
        codex_app_server_module.DEFAULT_COMMAND = [
            sys.executable,
            "-u",
            "-c",
            fake_account_server(
                result,
                rate_limits,
                rate_limits_error=rate_limits_error,
                refresh_error=refresh_error,
            ),
        ]
        try:
            return codex_app_server_module.account_status(force_provider_probe=force_provider_probe)
        finally:
            codex_app_server_module.DEFAULT_COMMAND = previous

    def test_thread_id_becomes_a_thread_scope_argument_pair(self) -> None:
        # The helper consumes the pair and names the systemd scope after the
        # host thread; non-task servers (status probes, logins) add nothing.
        server = codex_app_server_module.CodexAppServer(
            command=["/bin/echo"], thread_id="mission_pursuit__ws-3"
        )
        self.assertEqual(
            server._command,
            ["/bin/echo", "--thread-scope", "mission_pursuit__ws-3"],
        )
        self.assertEqual(
            codex_app_server_module.CodexAppServer(command=["/bin/echo"])._command, ["/bin/echo"]
        )

    def test_initialize_sends_client_name_and_version(self) -> None:
        self.assertEqual(
            codex_app_server_module._client_info(),
            {"clientInfo": {"name": "trustyclaw-host", "version": "v1.0"}},
        )

    def test_account_status_reads_account_id_from_helper_for_active_chatgpt_account(self) -> None:
        # Real Codex 0.124.0 account/read returns email/plan/type, not the
        # account id. TrustyClaw stores only the public identity fields it uses.
        result = {"account": {"email": "dev@example.com", "planType": "pro", "type": "chatgpt"}}
        with patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="acct_123"):
            self.assertEqual(
                self.account_status_with_result(result),
                (
                    "active",
                    None,
                    {"account_id": "acct_123", "email": "dev@example.com", "plan_type": "pro"},
                ),
            )

    def test_account_status_refreshes_credentials_after_rate_limit_read_failure(self) -> None:
        # A pinned account whose live usage read fails may hold stale cached
        # credentials. account/read with refreshToken=true validates the
        # credential through the auth endpoint without weakening the
        # data-plane guard.
        result = {"account": {"email": "dev@example.com", "planType": "pro", "type": "chatgpt"}}
        with (
            patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="acct_123"),
            patch("host.runtime.admin_api.codex_app_server.read_proxy_openai_account_id", return_value="acct_123"),
        ):
            self.assertEqual(
                self.account_status_with_result(result, rate_limits_error=True),
                (
                    "active",
                    None,
                    {"account_id": "acct_123", "email": "dev@example.com", "plan_type": "pro"},
                ),
            )

    def test_account_status_never_forces_a_refresh_before_the_first_pin(self) -> None:
        # An unapproved account has no proxy pin, so the guarded usage probe is
        # always denied; that is routine, not a credential problem. The account
        # classifies as readable (without usage) so the refresh settles it as
        # awaiting operator approval, and its refresh token is never rotated:
        # the refresh_error would fail this test if the fallback ran.
        result = {"account": {"email": "dev@example.com", "planType": "pro", "type": "chatgpt"}}
        with (
            patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="acct_123"),
            patch("host.runtime.admin_api.codex_app_server.read_proxy_openai_account_id", return_value=None),
        ):
            self.assertEqual(
                self.account_status_with_result(
                    result,
                    rate_limits_error=True,
                    refresh_error="401 Invalid authentication credentials",
                ),
                (
                    "active",
                    None,
                    {"account_id": "acct_123", "email": "dev@example.com", "plan_type": "pro"},
                ),
            )

    def test_account_status_maps_failed_credential_refresh_to_awaiting_login(self) -> None:
        result = {"account": {"email": "dev@example.com", "planType": "pro", "type": "chatgpt"}}
        with (
            patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="acct_123"),
            patch("host.runtime.admin_api.codex_app_server.read_proxy_openai_account_id", return_value="acct_123"),
        ):
            self.assertEqual(
                self.account_status_with_result(
                    result,
                    rate_limits_error=True,
                    refresh_error="401 Invalid authentication credentials",
                ),
                ("awaiting_login", None, None),
            )

    def test_failed_credential_refresh_is_not_retried_until_a_new_login(self) -> None:
        # The verdict about a rejected credential is final: later polls repeat
        # it without provider traffic (a successful-refresh fake would flip the
        # status back to active if the fallback ran again). An operator login
        # or account reset clears the verdict and revalidates from scratch.
        result = {"account": {"email": "dev@example.com", "planType": "pro", "type": "chatgpt"}}
        with (
            patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="acct_123"),
            patch("host.runtime.admin_api.codex_app_server.read_proxy_openai_account_id", return_value="acct_123"),
        ):
            self.assertEqual(
                self.account_status_with_result(
                    result,
                    rate_limits_error=True,
                    refresh_error="401 Invalid authentication credentials",
                ),
                ("awaiting_login", None, None),
            )
            self.assertEqual(
                self.account_status_with_result(result, rate_limits_error=True),
                ("awaiting_login", None, None),
            )
            codex_app_server_module.clear_live_validation_failure()
            self.assertEqual(
                self.account_status_with_result(result, rate_limits_error=True),
                (
                    "active",
                    None,
                    {"account_id": "acct_123", "email": "dev@example.com", "plan_type": "pro"},
                ),
            )

    def test_explicit_refresh_bypasses_failed_credential_refresh_memo(self) -> None:
        result = {"account": {"email": "dev@example.com", "planType": "pro", "type": "chatgpt"}}
        with (
            patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="acct_123"),
            patch("host.runtime.admin_api.codex_app_server.read_proxy_openai_account_id", return_value="acct_123"),
        ):
            self.assertEqual(
                self.account_status_with_result(
                    result,
                    rate_limits_error=True,
                    refresh_error="401 Invalid authentication credentials",
                ),
                ("awaiting_login", None, None),
            )
            self.assertEqual(
                self.account_status_with_result(
                    result,
                    rate_limits_error=True,
                    force_provider_probe=True,
                ),
                (
                    "active",
                    None,
                    {"account_id": "acct_123", "email": "dev@example.com", "plan_type": "pro"},
                ),
            )

    def test_failed_credential_refresh_error_is_retried_after_the_window(self) -> None:
        # A non-authentication failure (network, app-server) is remembered too,
        # but only for LIVE_VALIDATION_RETRY_SECONDS: infrastructure recovers
        # on its own, so the next scheduled recheck revalidates.
        result = {"account": {"email": "dev@example.com", "planType": "pro", "type": "chatgpt"}}
        with (
            patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="acct_123"),
            patch("host.runtime.admin_api.codex_app_server.read_proxy_openai_account_id", return_value="acct_123"),
        ):
            status, error_message, account = self.account_status_with_result(
                result, rate_limits_error=True, refresh_error="app-server unreachable"
            )
            self.assertEqual((status, account), ("error", None))
            assert error_message is not None
            self.assertIn("app-server unreachable", error_message)
            self.assertEqual(
                self.account_status_with_result(result, rate_limits_error=True),
                (status, error_message, None),
            )
            failure = codex_app_server_module._live_validation_failure
            assert failure is not None
            codex_app_server_module._live_validation_failure = (
                failure[0],
                failure[1],
                failure[2] - codex_app_server_module.LIVE_VALIDATION_RETRY_SECONDS - 1,
            )
            self.assertEqual(
                self.account_status_with_result(result, rate_limits_error=True),
                (
                    "active",
                    None,
                    {"account_id": "acct_123", "email": "dev@example.com", "plan_type": "pro"},
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
        with patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="acct_123"):
            self.assertEqual(
                self.account_status_with_result(result, rate_limits),
                (
                    "active",
                    None,
                    {
                        "account_id": "acct_123",
                        "email": "dev@example.com",
                        "plan_type": "pro",
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

    def test_account_status_polls_parked_device_login_server_until_active_without_closing_it(self) -> None:
        login_server = FakeLoginServer({"email": "dev@example.com", "planType": "pro", "type": "chatgpt"})
        park_login(login_server)

        with (
            patch("host.runtime.admin_api.codex_app_server.CodexAppServer", side_effect=AssertionError("started new server")),
            patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="acct_123"),
        ):
            self.assertEqual(
                codex_app_server_module.account_status(),
                (
                    "active",
                    None,
                    {"account_id": "acct_123", "email": "dev@example.com", "plan_type": "pro"},
                ),
            )

        self.assertEqual(login_server.calls, ["account/read", "account/rateLimits/read"])
        self.assertFalse(login_server.closed)

    def test_account_status_discards_only_the_same_dead_parked_login_server(self) -> None:
        login_server = FakeLoginServer(None, alive=False)
        park_login(login_server)

        self.assertIsNone(codex_app_server_module._current_login_server())

        self.assertIsNone(parked_server())
        self.assertTrue(login_server.closed)

    def test_login_server_pop_ignores_stale_server_reference(self) -> None:
        old_server = FakeLoginServer(None, alive=False)
        new_server = FakeLoginServer(None)
        park_login(new_server)

        self.assertIsNone(codex_app_server_module._pop_parked(lambda parked: parked.server is old_server))

        self.assertIs(parked_server(), new_server)
        self.assertFalse(old_server.closed)
        self.assertFalse(new_server.closed)

    def test_close_login_server_closes_whatever_is_parked(self) -> None:
        server = FakeLoginServer(None)
        park_login(server)

        codex_app_server_module.close_login_server()

        self.assertTrue(server.closed)
        self.assertIsNone(parked_server())

    def test_close_completed_login_server_ignores_a_newer_login_id(self) -> None:
        new_login = FakeLoginServer(None)
        park_login(new_login, "login-2")

        codex_app_server_module.close_completed_login_server("login-1")

        self.assertFalse(new_login.closed)
        self.assertIs(parked_server(), new_login)

    def test_close_completed_login_server_closes_the_matching_login(self) -> None:
        server = FakeLoginServer(None)
        park_login(server, "login-1")

        codex_app_server_module.close_completed_login_server("login-1")

        self.assertTrue(server.closed)
        self.assertIsNone(parked_server())

    def test_account_status_keeps_parked_device_login_server_while_awaiting_login(self) -> None:
        login_server = FakeLoginServer(None)
        park_login(login_server)

        with patch("host.runtime.admin_api.codex_app_server.CodexAppServer", side_effect=AssertionError("started new server")):
            self.assertEqual(codex_app_server_module.account_status(), ("awaiting_login", None, None))

        self.assertEqual(login_server.calls, ["account/read"])
        self.assertFalse(login_server.closed)

    def test_account_status_keeps_parked_device_login_server_for_login_required_error(self) -> None:
        login_server = FakeLoginServer(None, account_error="not logged in")
        park_login(login_server)

        with patch("host.runtime.admin_api.codex_app_server.CodexAppServer", side_effect=AssertionError("started new server")):
            self.assertEqual(codex_app_server_module.account_status(), ("awaiting_login", None, None))

        self.assertEqual(login_server.calls, ["account/read"])
        self.assertFalse(login_server.closed)

    def test_account_status_persists_login_completion_for_capture(self) -> None:
        # The status poller is the sole reader of the parked login server: reading
        # it must drain the completion notification so read_completed_device_login_account_id
        # can later look it up, even while the account itself still reads empty.
        login_server = FakeLoginServer(None, completed={"login-1"})
        park_login(login_server, "login-1")

        # The poller captures the trusted account id from the login tokens the
        # instant it observes the completion, so patch the helper around the poll.
        with (
            patch("host.runtime.admin_api.codex_app_server.CodexAppServer", side_effect=AssertionError("started new server")),
            patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="acct_123"),
        ):
            self.assertEqual(codex_app_server_module.account_status(), ("awaiting_login", None, None))

        self.assertEqual(login_server.calls, ["account/read"])
        self.assertFalse(login_server.closed)
        # Capture is a pure lookup of the id recorded at completion.
        self.assertEqual(codex_app_server_module.read_completed_device_login_account_id("login-1"), "acct_123")

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
        with patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value=None):
            status, detail, account_id = self.account_status_with_result(result)
        self.assertEqual(status, "error")
        self.assertIn("without a supported account id", detail or "")
        self.assertIsNone(account_id)

    def test_read_codex_account_id_reads_helper_stdout(self) -> None:
        self.assertEqual(read_codex_account_id([sys.executable, "-c", "print(' acct_123 ')"]), "acct_123")
        self.assertIsNone(read_codex_account_id([sys.executable, "-c", "import sys; sys.exit(1)"]))

    def test_completed_device_login_account_id_requires_matching_completion_notification(self) -> None:
        previous = codex_app_server_module.DEFAULT_COMMAND
        codex_app_server_module.DEFAULT_COMMAND = [
            sys.executable,
            "-u",
            "-c",
            fake_login_server(
                {"account": {"email": "dev@example.com"}},
                completed_login_id="login-1",
            ),
        ]
        try:
            login = codex_app_server_module.start_device_login()
            with patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="acct_123"):
                self.assertIsNone(codex_app_server_module.read_completed_device_login_account_id("other-login"))
                # Once the completion notification for this login is recorded, capture
                # reads the trusted account id from the login tokens (root helper).
                codex_app_server_module.account_status()
                self.assertEqual(
                    codex_app_server_module.read_completed_device_login_account_id(login.login_id),
                    "acct_123",
                )
        finally:
            codex_app_server_module.close_login_server()
            codex_app_server_module.DEFAULT_COMMAND = previous

    def test_completed_device_login_account_id_preserves_completion_before_account_appears(self) -> None:
        previous = codex_app_server_module.DEFAULT_COMMAND
        codex_app_server_module.DEFAULT_COMMAND = [sys.executable, "-u", "-c", fake_login_server_with_delayed_account()]
        try:
            login = codex_app_server_module.start_device_login()
            with patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="acct_123"):
                self.assertIsNone(codex_app_server_module.read_completed_device_login_account_id(login.login_id))
                # The first status read surfaces the completion notification before the
                # account itself reads back; the poller records it so capture succeeds.
                self.assertEqual(codex_app_server_module.account_status(), ("awaiting_login", None, None))
                self.assertEqual(
                    codex_app_server_module.read_completed_device_login_account_id(login.login_id),
                    "acct_123",
                )
        finally:
            codex_app_server_module.close_login_server()
            codex_app_server_module.DEFAULT_COMMAND = previous

    def test_completed_device_login_account_id_fails_closed_when_helper_has_no_id(self) -> None:
        previous = codex_app_server_module.DEFAULT_COMMAND
        codex_app_server_module.DEFAULT_COMMAND = [
            sys.executable,
            "-u",
            "-c",
            fake_login_server({"account": {"email": "dev@example.com"}}, completed_login_id="login-1"),
        ]
        try:
            login = codex_app_server_module.start_device_login()
            # Completion is recorded, but the helper cannot read a supported id: fail
            # closed rather than anchor an empty account.
            with patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value=None):
                codex_app_server_module.account_status()
                with self.assertRaises(CodexAppServerError) as error:
                    codex_app_server_module.read_completed_device_login_account_id(login.login_id)
            self.assertIn("completed login did not include", str(error.exception))
        finally:
            codex_app_server_module.close_login_server()
            codex_app_server_module.DEFAULT_COMMAND = previous

    def test_completed_device_login_account_id_ignores_active_account_without_completion(self) -> None:
        previous = codex_app_server_module.DEFAULT_COMMAND
        codex_app_server_module.DEFAULT_COMMAND = [
            sys.executable,
            "-u",
            "-c",
            fake_login_server({"account": {"email": "attacker@example.com"}}),
        ]
        try:
            login = codex_app_server_module.start_device_login()
            # An active account with no completion notification for this login (an
            # attacker-swapped auth file) must never be captured as the anchor: with
            # no recorded completion, the helper id is never even read.
            with patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="auth_file_acct"):
                codex_app_server_module.account_status()
                self.assertIsNone(codex_app_server_module.read_completed_device_login_account_id(login.login_id))
        finally:
            codex_app_server_module.close_login_server()
            codex_app_server_module.DEFAULT_COMMAND = previous

    def test_completed_device_login_account_id_ignores_wrong_login_completion(self) -> None:
        previous = codex_app_server_module.DEFAULT_COMMAND
        codex_app_server_module.DEFAULT_COMMAND = [
            sys.executable,
            "-u",
            "-c",
            fake_login_server({"account": {"email": "attacker@example.com"}}, completed_login_id="other-login"),
        ]
        try:
            login = codex_app_server_module.start_device_login()
            # A completion notification for a different login id must not satisfy
            # capture for this login.
            with patch("host.runtime.admin_api.codex_app_server.read_codex_account_id", return_value="auth_file_acct"):
                codex_app_server_module.account_status()
                self.assertIsNone(codex_app_server_module.read_completed_device_login_account_id(login.login_id))
        finally:
            codex_app_server_module.close_login_server()
            codex_app_server_module.DEFAULT_COMMAND = previous

    def test_run_turn_emits_each_message_and_returns_the_last_one(self) -> None:
        messages: list[str] = []
        with CodexAppServer([sys.executable, "-u", "-c", FAKE_APP_SERVER]) as server:
            thread_id, output = run_turn(
                server,
                "do the task",
                None,
                "gpt-5.6-sol",
                "ultra",
                lambda: [],
                messages.append,
                lambda _m: None,
            )

        self.assertEqual(thread_id, "thread_1")
        self.assertEqual(messages, ["Hello", "Final answer"])
        self.assertEqual(output, "Final answer")

    def test_new_app_thread_receives_manifest_instructions_as_developer_instructions(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []

        class RecordingServer:
            app_instructions = "Use only /agent/workspace."

            def call(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
                calls.append((method, params))
                return {"thread": {"id": "thread_1"}}

        thread = codex_app_server_module._start_thread(RecordingServer(), "gpt-5.6-sol")  # type: ignore[arg-type]

        self.assertEqual(thread["id"], "thread_1")
        self.assertEqual(calls[0][0], "thread/start")
        instructions = calls[0][1]["developerInstructions"]
        self.assertIn("You are running inside TrustyClaw", instructions)
        self.assertIn("App instructions:\nUse only /agent/workspace.", instructions)

    def test_run_turn_refreshes_instructions_and_model_when_resuming_a_thread(self) -> None:
        with CodexAppServer([sys.executable, "-u", "-c", FAKE_APP_SERVER]) as server:
            server.app_instructions = "Use only /agent/workspace."
            thread_id, output = run_turn(
                server,
                "continue",
                "thread_existing",
                "gpt-5.6-sol",
                "ultra",
                lambda: [],
                lambda _m: None,
                lambda _m: None,
            )

        self.assertEqual(thread_id, "thread_existing")
        self.assertEqual(output, "Final answer")

    def test_run_turn_returns_unflushed_deltas_on_completion(self) -> None:
        with CodexAppServer([sys.executable, "-u", "-c", FAKE_DELTA_ONLY_SERVER]) as server:
            _, output = run_turn(
                server,
                "do the task",
                None,
                "gpt-5.6-terra",
                "high",
                lambda: [],
                lambda _m: None,
                lambda _m: None,
            )
        self.assertEqual(output, "partial answer")

    def test_run_turn_retries_a_steer_that_raced_turn_startup(self) -> None:
        # "no active turn to steer" right after turn/start is transient; the
        # steer stays in the pending queue and is retried on the next loop
        # pass, not failed — and it is consumed exactly once, on delivery.
        pending = ["redirect"]
        with CodexAppServer([sys.executable, "-u", "-c", FAKE_STEER_RETRY_SERVER]) as server:
            _, output = run_turn(
                server, "do the task", None, "gpt-5.6-terra", "high",
                lambda: list(pending), lambda _m: None, pending.remove,
            )
        self.assertEqual(output, "STEERED")
        self.assertEqual(pending, [])

    def test_run_turn_surfaces_a_non_transient_steer_error(self) -> None:
        pending = ["redirect"]
        with CodexAppServer([sys.executable, "-u", "-c", FAKE_STEER_REJECT_SERVER]) as server:
            with self.assertRaises(CodexAppServerError) as error:
                run_turn(
                    server, "do the task", None, "gpt-5.6-terra", "high",
                    lambda: list(pending), lambda _m: None, pending.remove,
                )
        self.assertIn("malformed input", str(error.exception))
        # The rejected steer was never delivered, so it was never consumed.
        self.assertEqual(pending, ["redirect"])


if __name__ == "__main__":
    unittest.main()
