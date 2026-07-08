from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import pg_harness

from host.runtime import orchestrator
from host.runtime.network_policy import anthropic_request_denied, save_policy
from host.runtime.state import (
    read_agent_events,
    read_claude_account,
    read_openai_account,
    read_proxy_claude_account,
    read_proxy_openai_account_id,
    save_claude_account,
    save_openai_account,
    save_proxy_claude_account,
    save_proxy_openai_account_id,
)
from state_seed import load_state, save_state


class FakeServer:
    """Stands in for CodexAppServer: records lifecycle calls and lets a test
    hold a turn open (via blocker) to observe concurrency."""

    instances: list["FakeServer"] = []

    def __init__(self, command: object = None) -> None:
        self.started = 0
        self.closed = False
        FakeServer.instances.append(self)

    def start(self, init_timeout: float = 60.0) -> None:
        self.started += 1

    def alive(self) -> bool:
        return self.started > 0 and not self.closed

    def close(self) -> None:
        self.closed = True


def make_task(number: int, thread_id: str, status: str = "queued", runtime: str = "codex") -> dict[str, object]:
    return {
        "task_id": f"task_{number}",
        "status": status,
        "agent_runtime": runtime,
        "thread_id": thread_id,
        "input_message": f"task {number}",
        "steer_messages": [],
        "created_at": "2026-06-08T00:00:00Z",
        "updated_at": "2026-06-08T00:00:00Z",
    }


def save_approved_openai_account(account_id: str, **extra: object) -> None:
    save_openai_account(
        {"account_id": account_id, "operator_approval": orchestrator.OPENAI_OPERATOR_APPROVAL, **extra}
    )


def save_attested_claude_account(account_id: str, **extra: object) -> None:
    save_claude_account(
        {"account_id": account_id, "identity_attestation": orchestrator.CLAUDE_IDENTITY_ATTESTATION, **extra}
    )


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.proxy_temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.proxy_temp_dir.cleanup)
        self.env_patch = patch.dict(
            "os.environ",
            {"TRUSTYCLAW_STATE_DIR": self.temp_dir.name, "TRUSTYCLAW_PROXY_STATE_DIR": self.proxy_temp_dir.name},
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        FakeServer.instances = []
        orchestrator._POOL.clear()
        orchestrator._CLOSING_THREADS.clear()
        orchestrator._CLAUDE_ATTESTATIONS.clear()
        self.addCleanup(orchestrator._POOL.clear)
        self.addCleanup(orchestrator._CLOSING_THREADS.clear)
        self.addCleanup(orchestrator._CLAUDE_ATTESTATIONS.clear)
        save_policy(
            {
                "managed_network_integrations": {"openai": {"enabled": True}, "claude": {"enabled": True}},
                "allowed_network_access": {},
            },
            "2026-06-08T00:00:00Z",
        )
        self.server_patch = patch.object(orchestrator.codex_app_server, "CodexAppServer", FakeServer)
        self.server_patch.start()
        self.addCleanup(self.server_patch.stop)
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "active"
        state["agent_runtime_statuses"]["claude_code"]["status"] = "active"
        save_state(state)

    def seed_tasks(self, *tasks: dict[str, object]) -> None:
        state = load_state()
        state["tasks"] = list(tasks)
        state["next_task_number"] = len(tasks) + 1
        save_state(state)

    def run_turn_stub(self, outputs: dict[str, str] | None = None, release: threading.Event | None = None):
        """A run_turn replacement: returns ("codex-<user thread>", output),
        optionally blocking until the test releases it."""

        def fake_run_turn(server, input_message, codex_thread_id, steers, on_message, steer_delivered):
            if release is not None:
                if not release.wait(timeout=10):
                    raise AssertionError("test never released the fake turn")
            return f"codex-{input_message}", (outputs or {}).get(input_message, "done")

        return fake_run_turn

    def task_status(self, task_id: str) -> str:
        return next(t["status"] for t in load_state()["tasks"] if t["task_id"] == task_id)

    def test_active_runtime_refresh_stamps_usage_last_checked_at(self) -> None:
        save_approved_openai_account("acct")
        with (
            patch.object(orchestrator, "utc_now", return_value="2026-06-29T23:10:00Z"),
            patch.object(
                orchestrator.codex_app_server,
                "account_status",
                return_value=(
                    "active",
                    None,
                    {
                        "account_id": "acct",
                        "codex_usage": {
                            "rate_limits": {
                                "primary": {
                                    "used_percent": 8,
                                    "window_duration_mins": 300,
                                    "resets_at": 1782788897,
                                }
                            }
                        },
                    },
                ),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "active")

        account = read_openai_account()
        self.assertEqual(account["codex_usage"]["last_checked_at"], "2026-06-29T23:10:00Z")

    def test_delivered_steers_are_consumed_from_state(self) -> None:
        # steers() hands the worker only the undelivered queue, and each
        # delivery removes its steer from state — the content survives as a
        # task.message event, so state holds no unbounded steer history.
        task = make_task(1, "t1")
        task["steer_messages"] = ["first", "second"]
        self.seed_tasks(task)
        observed: list[list[str]] = []

        def fake_run_turn(server, input_message, codex_thread_id, steers, on_message, steer_delivered):
            observed.append(steers())
            steer_delivered("first")
            observed.append(steers())
            return "codex-t1", "done"

        with patch.object(orchestrator.codex_app_server, "run_turn", fake_run_turn):
            orchestrator.run_next_task()

        self.assertEqual(observed, [["first", "second"], ["second"]])
        remaining = next(t for t in load_state()["tasks"] if t["task_id"] == "task_1")
        self.assertEqual(remaining["steer_messages"], ["second"])
        self.assertEqual(self.task_status("task_1"), "completed")
        events = [event for event in read_agent_events() if event.get("task_id") == "task_1"]
        self.assertEqual(
            [(event["event_type"], event.get("payload", {}).get("message")) for event in events],
            [("task.started", None), ("task.message", "task 1"), ("task.completed", None)],
        )

    def test_runs_up_to_worker_count_tasks_in_parallel_one_per_thread(self) -> None:
        # 4 queued Codex tasks on 3 distinct threads: t1 twice. At most the
        # per-runtime cap runs at once, and the two t1 tasks never run together.
        self.seed_tasks(
            make_task(1, "t1"), make_task(2, "t2"), make_task(3, "t1"), make_task(4, "t3")
        )
        release = threading.Event()
        started: list[str] = []

        def fake_run_turn(server, input_message, codex_thread_id, steers, on_message, steer_delivered):
            started.append(input_message)
            release.wait(timeout=10)
            return "codex-x", "done"

        with patch.object(orchestrator.codex_app_server, "run_turn", fake_run_turn):
            threads = [threading.Thread(target=orchestrator.run_next_task) for _ in range(4)]
            for thread in threads:
                thread.start()
            for _ in range(100):
                if len(started) == 3:
                    break
                threading.Event().wait(0.01)
            # The 4th claim found 3 Codex tasks running and bailed; task_3
            # shares t1 with running task_1 so it must still be queued.
            self.assertEqual(sorted(started), ["task 1", "task 2", "task 4"])
            self.assertEqual(self.task_status("task_3"), "queued")
            release.set()
            for thread in threads:
                thread.join(timeout=10)

        for task_id in ("task_1", "task_2", "task_4"):
            self.assertEqual(self.task_status(task_id), "completed")
        self.assertEqual(self.task_status("task_3"), "queued")
        # A later pass picks up the same-thread task once t1 is free.
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            orchestrator.run_next_task()
        self.assertEqual(self.task_status("task_3"), "completed")

    def test_busy_slots_count_against_worker_capacity_even_after_task_is_terminal(self) -> None:
        # A worker sets the task terminal before it releases the pool slot.
        # During that short unwind window, state may show fewer RUNNING tasks
        # than there are busy runtime processes. Claiming a different-thread
        # task for the same runtime then would temporarily exceed the
        # per-runtime process cap.
        self.seed_tasks(make_task(1, "new-codex"), make_task(2, "new-claude", runtime="claude_code"))
        with orchestrator._POOL_LOCK:
            for index in range(orchestrator.WORKER_COUNT_PER_RUNTIME):
                server = FakeServer()
                server.started = 1
                orchestrator._POOL[f"codex:busy-{index}"] = orchestrator._Slot(
                    server,
                    "codex",
                    f"busy-{index}",
                    True,
                    1.0,
                    f"task_done_{index}",
                )

        self.assertEqual(orchestrator._claim_next_task(), ("task_2", "claude_code", "new-claude", "task 2", None))
        self.assertEqual(self.task_status("task_1"), "queued")
        self.assertEqual(self.task_status("task_2"), "running")

    def test_claim_uses_current_provider_policy_not_only_cached_active_status(self) -> None:
        save_policy(
            {"managed_network_integrations": {}, "allowed_network_access": {}},
            "2026-06-08T00:00:01Z",
        )
        self.seed_tasks(make_task(1, "stale-active-codex"), make_task(2, "stale-active-claude", runtime="claude_code"))

        self.assertIsNone(orchestrator._claim_next_task())
        self.assertEqual(self.task_status("task_1"), "queued")
        self.assertEqual(self.task_status("task_2"), "queued")

    def test_codex_and_claude_have_independent_three_task_claim_caps(self) -> None:
        save_attested_claude_account("acct", access_token_sha256="f" * 64)
        tasks = []
        number = 1
        for index in range(4):
            tasks.append(make_task(number, f"codex-{index}", runtime="codex"))
            number += 1
            tasks.append(make_task(number, f"claude-{index}", runtime="claude_code"))
            number += 1
        self.seed_tasks(*tasks)
        release = threading.Event()
        started: list[tuple[str, str]] = []
        started_lock = threading.Lock()

        def record(runtime_type: str, input_message: str) -> None:
            with started_lock:
                started.append((runtime_type, input_message))

        def fake_codex_run_turn(server, input_message, codex_thread_id, steers, on_message, steer_delivered):
            record("codex", input_message)
            release.wait(timeout=10)
            return f"codex-{input_message}", "done"

        def fake_claude_run_turn(server, input_message, session_id, steers, on_message, steer_delivered):
            record("claude_code", input_message)
            release.wait(timeout=10)
            return f"claude-{input_message}", "done"

        with (
            patch.object(orchestrator.claude_code, "ClaudeCodeSession", FakeServer),
            patch.object(orchestrator.codex_app_server, "run_turn", fake_codex_run_turn),
            patch.object(orchestrator.claude_code, "run_turn", fake_claude_run_turn),
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct", "access_token_sha256": "f" * 64}),
            ),
        ):
            threads = [threading.Thread(target=orchestrator.run_next_task) for _ in range(8)]
            for thread in threads:
                thread.start()
            for _ in range(200):
                with started_lock:
                    if len(started) == orchestrator.WORKER_COUNT:
                        break
                threading.Event().wait(0.01)
            with started_lock:
                snapshot = list(started)
            self.assertEqual(len(snapshot), 6)
            self.assertEqual(sum(1 for runtime, _ in snapshot if runtime == "codex"), 3)
            self.assertEqual(sum(1 for runtime, _ in snapshot if runtime == "claude_code"), 3)
            release.set()
            for thread in threads:
                thread.join(timeout=10)

        completed = {task["task_id"] for task in load_state()["tasks"] if task["status"] == "completed"}
        queued = {task["task_id"] for task in load_state()["tasks"] if task["status"] == "queued"}
        self.assertEqual(len(completed), 6)
        self.assertEqual(queued, {"task_7", "task_8"})

    def test_completed_task_keeps_a_warm_server_that_the_next_task_reuses(self) -> None:
        self.seed_tasks(make_task(1, "chat"))
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            orchestrator.run_next_task()
        self.assertEqual(len(FakeServer.instances), 1)
        self.assertFalse(FakeServer.instances[0].closed)
        self.assertIn("codex:chat", orchestrator._POOL)

        # The follow-up task on the same thread reuses the warm server: no new
        # spawn, no second start().
        self.seed_tasks(make_task(1, "chat", status="completed"), make_task(2, "chat"))
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            orchestrator.run_next_task()
        self.assertEqual(len(FakeServer.instances), 1)
        self.assertEqual(FakeServer.instances[0].started, 1)
        self.assertEqual(self.task_status("task_2"), "completed")

    def test_completed_task_records_the_codex_thread_mapping_and_resumes_it(self) -> None:
        self.seed_tasks(make_task(1, "chat"))
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            orchestrator.run_next_task()
        mapping = load_state()["codex_threads"]["chat"]
        self.assertEqual(mapping["codex_thread_id"], "codex-task 1")

        seen: list[str | None] = []

        def recording_run_turn(server, input_message, codex_thread_id, steers, on_message, steer_delivered):
            seen.append(codex_thread_id)
            return "codex-task 1", "done"

        self.seed_tasks(make_task(1, "chat", status="completed"), make_task(2, "chat"))
        with patch.object(orchestrator.codex_app_server, "run_turn", recording_run_turn):
            orchestrator.run_next_task()
        self.assertEqual(seen, ["codex-task 1"])

    def test_claude_runtime_records_and_resumes_session_id(self) -> None:
        save_attested_claude_account("acct", access_token_sha256="f" * 64)
        self.seed_tasks(make_task(1, "chat", runtime="claude_code"))
        seen: list[str | None] = []

        def fake_run_turn(server, input_message, session_id, steers, on_message, steer_delivered):
            seen.append(session_id)
            return "claude-session-1", "done"

        with (
            patch.object(orchestrator.claude_code, "ClaudeCodeSession", FakeServer),
            patch.object(orchestrator.claude_code, "run_turn", fake_run_turn),
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct", "access_token_sha256": "f" * 64}),
            ),
        ):
            orchestrator.run_next_task()

        state = load_state()
        self.assertEqual(seen, [None])
        self.assertEqual(state["claude_sessions"]["chat"]["session_id"], "claude-session-1")
        self.assertNotIn("chat", state["codex_threads"])

        self.seed_tasks(
            make_task(1, "chat", status="completed", runtime="claude_code"),
            make_task(2, "chat", runtime="claude_code"),
        )
        with (
            patch.object(orchestrator.claude_code, "ClaudeCodeSession", FakeServer),
            patch.object(orchestrator.claude_code, "run_turn", fake_run_turn),
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct", "access_token_sha256": "f" * 64}),
            ),
        ):
            orchestrator.run_next_task()

        self.assertEqual(seen, [None, "claude-session-1"])

    def test_claude_task_repins_rotated_token_before_turn(self) -> None:
        # The Claude CLI refreshes its OAuth access token on its own schedule.
        # The bearer-token pin must follow that rotation (only the account
        # identity is anchored), and the pre-turn refresh is what re-pins it.
        self.seed_tasks(make_task(1, "chat", runtime="claude_code"))
        old_token = "old-token"
        fresh_token = "fresh-token"
        policy = {
            "allowed_network_access": {
                "api.anthropic.com": {"allow_http_methods": ["GET", "POST"], "anthropic_account_guard": True}
            }
        }
        save_attested_claude_account("acct", access_token_sha256=hashlib.sha256(old_token.encode()).hexdigest())
        save_proxy_claude_account(
            {
                "account_id": "acct",
                "access_token_sha256": hashlib.sha256(old_token.encode()).hexdigest(),
            }
        )
        self.assertIsNone(
            anthropic_request_denied(
                policy, "POST", "api.anthropic.com", "/v1/messages", [("Authorization", f"Bearer {old_token}")]
            )
        )

        with (
            patch.object(orchestrator.claude_code, "ClaudeCodeSession", FakeServer),
            patch.object(orchestrator.claude_code, "run_turn", self.run_turn_stub()),
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=(
                    "active",
                    None,
                    {"account_id": "acct", "access_token_sha256": hashlib.sha256(fresh_token.encode()).hexdigest()},
                ),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={
                    "access_token_sha256": hashlib.sha256(fresh_token.encode()).hexdigest(),
                    "account_uuid": "acct",
                },
            ),
        ):
            orchestrator.run_next_task()

        self.assertEqual(self.task_status("task_1"), "completed")
        self.assertEqual(read_claude_account()["access_token_sha256"], hashlib.sha256(fresh_token.encode()).hexdigest())
        self.assertEqual(read_proxy_claude_account()["access_token_sha256"], hashlib.sha256(fresh_token.encode()).hexdigest())
        self.assertIsNotNone(
            anthropic_request_denied(
                policy, "POST", "api.anthropic.com", "/v1/messages", [("Authorization", f"Bearer {old_token}")]
            )
        )
        self.assertIsNone(
            anthropic_request_denied(
                policy, "POST", "api.anthropic.com", "/v1/messages", [("Authorization", f"Bearer {fresh_token}")]
            )
        )

    def test_claude_refresh_rejects_token_attested_to_another_account(self) -> None:
        # Token rotation is allowed; a different *account* is not. The new
        # token's owner comes from the provider's profile endpoint, so forged
        # local metadata cannot help: the attested uuid decides.
        save_attested_claude_account("acct-trusted", access_token_sha256="0" * 64)
        save_proxy_claude_account({"account_id": "acct-trusted", "access_token_sha256": "0" * 64})

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-trusted", "access_token_sha256": "1" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={"access_token_sha256": "1" * 64, "account_uuid": "acct-attacker"},
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "error")

        self.assertEqual(read_claude_account()["account_id"], "acct-trusted")
        self.assertEqual(read_proxy_claude_account(), {})
        record = orchestrator.runtime_status_record("claude_code")
        self.assertIn("account changed", record["error_message"])

    def test_claude_refresh_rejects_attested_anchor_email_collision(self) -> None:
        save_attested_claude_account("acct-trusted", email="op@example.com", access_token_sha256="0" * 64)

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=(
                    "active",
                    None,
                    {"account_id": "acct-trusted", "email": "op@example.com", "access_token_sha256": "1" * 64},
                ),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={"access_token_sha256": "1" * 64, "account_uuid": "acct-attacker", "email": "op@example.com"},
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "error")

        self.assertEqual(read_claude_account()["account_id"], "acct-trusted")
        self.assertEqual(read_proxy_claude_account(), {})
        self.assertIn("account changed", orchestrator.runtime_status_record("claude_code").get("error_message", ""))

    def test_claude_refresh_skips_attestation_for_anchored_token_and_ignores_local_metadata(self) -> None:
        # An unchanged token was attested when it was anchored: no network
        # call, and the identity saved comes from the anchor, so agent-forged
        # local metadata never lands anywhere.
        save_attested_claude_account("acct-trusted", email="op@example.com", access_token_sha256="f" * 64)

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "forged-uuid", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("anchored token must not re-attest"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")

        self.assertEqual(read_claude_account()["account_id"], "acct-trusted")
        self.assertEqual(read_claude_account()["email"], "op@example.com")
        self.assertEqual(read_proxy_claude_account()["account_id"], "acct-trusted")
        self.assertEqual(read_proxy_claude_account()["access_token_sha256"], "f" * 64)

    def test_claude_legacy_anchor_without_login_stays_awaiting(self) -> None:
        # Pre-attestation releases could anchor Claude by local agent-writable
        # metadata such as email. That row is not a trusted anchor, so with no
        # operator login in flight the agent cannot self-promote it: the runtime
        # stays awaiting_login, never attests, and the stale row is left intact
        # until a fresh operator login re-captures it (see the login test below).
        save_claude_account(
            {"account_id": "op@example.com", "email": "op@example.com", "access_token_sha256": "f" * 64}
        )

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "op@example.com", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("legacy Claude row must not attest without an operator login"),
            ) as attest,
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "awaiting_login")

        attest.assert_not_called()
        account = read_claude_account()
        self.assertEqual(account["account_id"], "op@example.com")
        self.assertEqual(account["email"], "op@example.com")
        self.assertNotIn("identity_attestation", account)
        self.assertEqual(read_proxy_claude_account(), {})

    def test_claude_legacy_anchor_recaptured_by_operator_login_without_reset(self) -> None:
        # A pre-attestation upgrade row plus a completed operator login re-captures
        # through first-capture attestation, overwriting the legacy identity in
        # place. No separate reset is required (parity with an unapproved OpenAI
        # row, which a plain re-login also re-captures).
        save_claude_account(
            {"account_id": "op@example.com", "email": "stale@example.com", "access_token_sha256": "0" * 64}
        )
        state = load_state()
        state["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        state["claude_oauth"] = {
            "status": "completed",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2099-06-08T00:10:00Z",
            "access_token_sha256": "f" * 64,
        }
        save_state(state)

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "forged-uuid", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={
                    "access_token_sha256": "f" * 64,
                    "account_uuid": "acct-real",
                    "email": "op@example.com",
                    "organization_uuid": "org-real",
                },
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")

        account = read_claude_account()
        self.assertEqual(account["account_id"], "acct-real")
        self.assertEqual(account["email"], "op@example.com")
        self.assertEqual(account["identity_attestation"], orchestrator.CLAUDE_IDENTITY_ATTESTATION)
        self.assertEqual(read_proxy_claude_account()["account_id"], "acct-real")
        self.assertIsNone(load_state().get("claude_oauth"))

    def test_claude_attestation_failure_is_retryable(self) -> None:
        save_attested_claude_account("acct-trusted", access_token_sha256="0" * 64)
        probe = ("active", None, {"account_id": "acct-trusted", "access_token_sha256": "1" * 64})

        with (
            patch.object(orchestrator.claude_code, "account_status", return_value=probe),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=orchestrator.claude_code.ClaudeCodeError("could not reach the Claude profile endpoint"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "error")
        self.assertIn(
            "could not reach", orchestrator.runtime_status_record("claude_code").get("error_message", "")
        )
        self.assertEqual(read_claude_account()["access_token_sha256"], "0" * 64)
        self.assertEqual(read_proxy_claude_account(), {})

        with (
            patch.object(orchestrator.claude_code, "account_status", return_value=probe),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={"access_token_sha256": "1" * 64, "account_uuid": "acct-trusted"},
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")
        self.assertEqual(read_claude_account()["access_token_sha256"], "1" * 64)
        self.assertEqual(read_proxy_claude_account()["access_token_sha256"], "1" * 64)

    def test_claude_first_capture_anchors_attested_identity(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        state["claude_oauth"] = {
            "status": "completed",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2099-06-08T00:10:00Z",
            "access_token_sha256": "f" * 64,
        }
        save_state(state)

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "forged-uuid", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={
                    "access_token_sha256": "f" * 64,
                    "account_uuid": "acct-real",
                    "email": "op@example.com",
                    "organization_uuid": "org-real",
                },
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")

        account = read_claude_account()
        self.assertEqual(account["account_id"], "acct-real")
        self.assertEqual(account["email"], "op@example.com")
        self.assertEqual(account["organization_id"], "org-real")
        self.assertEqual(read_proxy_claude_account()["account_id"], "acct-real")
        self.assertIsNone(load_state().get("claude_oauth"))

    def test_claude_first_capture_requires_completed_token_hash(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        state["claude_oauth"] = {
            "status": "completed",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        save_state(state)

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-attacker", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("unhashed completed Claude OAuth must not attest or anchor"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "awaiting_login")

        self.assertEqual(orchestrator.runtime_status_record("claude_code"), {"status": "awaiting_login"})
        self.assertEqual(read_claude_account(), {})
        self.assertEqual(read_proxy_claude_account(), {})

    def test_claude_pending_oauth_cannot_attest_or_anchor_first_account(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        state["claude_oauth"] = {
            "status": "awaiting_code",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        save_state(state)

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-attacker", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("pending Claude OAuth must not trigger direct attestation egress"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "awaiting_login")

        self.assertEqual(orchestrator.runtime_status_record("claude_code"), {"status": "awaiting_login"})
        self.assertEqual(read_claude_account(), {})
        self.assertEqual(read_proxy_claude_account(), {})

    def test_full_pool_evicts_the_least_recently_used_idle_server(self) -> None:
        for number in range(1, orchestrator.WORKER_COUNT + 1):
            thread_id = f"t{number}"
            self.seed_tasks(make_task(number, thread_id))
            with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
                orchestrator.run_next_task()
        self.assertEqual(len(orchestrator._POOL), orchestrator.WORKER_COUNT)
        oldest = orchestrator._POOL["codex:t1"].server

        new_number = orchestrator.WORKER_COUNT + 1
        self.seed_tasks(make_task(new_number, f"t{new_number}"))
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            orchestrator.run_next_task()
        expected = {f"codex:t{number}" for number in range(2, orchestrator.WORKER_COUNT + 2)}
        self.assertEqual(set(orchestrator._POOL), expected)
        self.assertTrue(oldest.closed)

    def test_failed_turn_closes_the_server_instead_of_pooling_it(self) -> None:
        self.seed_tasks(make_task(1, "chat"))

        def failing_run_turn(server, input_message, codex_thread_id, steers, on_message, steer_delivered):
            raise orchestrator.codex_app_server.CodexAppServerError("turn failed")

        with patch.object(orchestrator.codex_app_server, "run_turn", failing_run_turn):
            orchestrator.run_next_task()
        self.assertEqual(self.task_status("task_1"), "failed")
        self.assertNotIn("codex:chat", orchestrator._POOL)
        self.assertTrue(FakeServer.instances[0].closed)

    def test_dead_warm_server_is_replaced_not_reused(self) -> None:
        self.seed_tasks(make_task(1, "chat"))
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            orchestrator.run_next_task()
        FakeServer.instances[0].closed = True  # the warm server died while idle

        self.seed_tasks(make_task(1, "chat", status="completed"), make_task(2, "chat"))
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            orchestrator.run_next_task()
        self.assertEqual(len(FakeServer.instances), 2)
        self.assertEqual(self.task_status("task_2"), "completed")

    def test_close_task_server_kills_the_running_turn_and_cancellation_sticks(self) -> None:
        self.seed_tasks(make_task(1, "chat"))
        running = threading.Event()
        release = threading.Event()

        def blocking_run_turn(server, input_message, codex_thread_id, steers, on_message, steer_delivered):
            running.set()
            if not release.wait(timeout=10):
                raise AssertionError("never released")
            if server.closed:  # what a real run_turn does on a dead server
                raise orchestrator.codex_app_server.CodexAppServerError("Codex app-server is not running")
            return "codex-x", "done"

        with patch.object(orchestrator.codex_app_server, "run_turn", blocking_run_turn):
            worker = threading.Thread(target=orchestrator.run_next_task)
            worker.start()
            self.assertTrue(running.wait(timeout=10))
            # The kill path: the API marks the task cancelled, then closes its server.
            state = load_state()
            state["tasks"][0]["status"] = "cancelled"
            save_state(state)
            orchestrator.close_task_server("task_1")
            release.set()
            worker.join(timeout=10)

        self.assertTrue(FakeServer.instances[0].closed)
        self.assertNotIn("codex:chat", orchestrator._POOL)
        # _finish_task saw the cancelled status and did not flip it to failed.
        self.assertEqual(self.task_status("task_1"), "cancelled")

    def test_deactivate_runtime_fails_running_tasks_and_closes_only_that_runtime(self) -> None:
        codex_idle = FakeServer()
        codex_idle.started = 1
        codex_busy = FakeServer()
        codex_busy.started = 1
        claude_idle = FakeServer()
        claude_idle.started = 1
        self.seed_tasks(
            make_task(1, "codex-running", status="running"),
            make_task(2, "codex-queued"),
            make_task(3, "claude-running", status="running", runtime="claude_code"),
        )
        with orchestrator._POOL_LOCK:
            orchestrator._POOL["codex:idle"] = orchestrator._Slot(codex_idle, "codex", "idle", False, 1.0, None)
            orchestrator._POOL["codex:codex-running"] = orchestrator._Slot(
                codex_busy,
                "codex",
                "codex-running",
                True,
                1.0,
                "task_1",
            )
            orchestrator._POOL["claude_code:idle"] = orchestrator._Slot(
                claude_idle,
                "claude_code",
                "idle",
                False,
                1.0,
                None,
            )

        orchestrator.deactivate_runtime("codex", "provider disabled")

        state = load_state()
        tasks = {task["task_id"]: task for task in state["tasks"]}
        self.assertEqual(tasks["task_1"]["status"], "failed")
        self.assertEqual(tasks["task_1"]["error_message"], "provider disabled")
        self.assertEqual(tasks["task_2"]["status"], "queued")
        self.assertEqual(tasks["task_3"]["status"], "running")
        self.assertTrue(codex_idle.closed)
        self.assertTrue(codex_busy.closed)
        self.assertFalse(claude_idle.closed)
        self.assertEqual(set(orchestrator._POOL), {"claude_code:idle"})
        self.assertEqual(orchestrator._CLOSING_THREADS, {})

    def test_runtime_status_loss_clears_account_pin_without_tearing_down_running_task(self) -> None:
        save_approved_openai_account("acct-old")
        save_proxy_openai_account_id("acct-old")
        codex_busy = FakeServer()
        codex_busy.started = 1
        self.seed_tasks(make_task(1, "codex-running", status="running"))
        with orchestrator._POOL_LOCK:
            orchestrator._POOL["codex:codex-running"] = orchestrator._Slot(
                codex_busy,
                "codex",
                "codex-running",
                True,
                1.0,
                "task_1",
            )

        with patch.object(orchestrator.codex_app_server, "account_status", return_value=("awaiting_login", None, None)):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        state = load_state()
        task = state["tasks"][0]
        self.assertEqual(task["status"], "running")
        self.assertEqual(read_openai_account().get("account_id"), "acct-old")
        self.assertIsNone(read_proxy_openai_account_id())
        self.assertFalse(codex_busy.closed)
        self.assertIn("codex:codex-running", orchestrator._POOL)

        def account_status_without_reseed() -> tuple[str, str | None, None]:
            self.assertIsNone(read_proxy_openai_account_id())
            return "awaiting_login", None, None

        with patch.object(orchestrator.codex_app_server, "account_status", side_effect=account_status_without_reseed):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertIsNone(read_proxy_openai_account_id())

    def test_codex_refresh_seeds_proxy_pin_from_trusted_account_before_guarded_status(self) -> None:
        save_approved_openai_account("acct-local")
        self.assertIsNone(read_proxy_openai_account_id())

        def account_status():
            self.assertEqual(read_proxy_openai_account_id(), "acct-local")
            return "active", None, {"account_id": "acct-local"}

        with patch.object(orchestrator.codex_app_server, "account_status", side_effect=account_status):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "active")

        self.assertEqual(read_proxy_openai_account_id(), "acct-local")

    def test_codex_seed_clears_proxy_pin_if_reset_wins_before_status_probe(self) -> None:
        save_approved_openai_account("acct-local")
        real_sync = orchestrator.proxy_state_client.sync_openai_account_id
        raced: list[str] = []

        def sync_with_reset_race(account_id):
            if account_id and not raced:
                raced.append(account_id)
                self.assertIsNone(orchestrator.reset_linked_account("codex"))
            real_sync(account_id)

        def account_status():
            self.assertIsNone(read_proxy_openai_account_id())
            return "awaiting_login", None, None

        with (
            patch.object(orchestrator.proxy_state_client, "sync_openai_account_id", side_effect=sync_with_reset_race),
            patch.object(orchestrator.codex_app_server, "account_status", side_effect=account_status),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(raced, ["acct-local"])
        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())

    def test_codex_legacy_openai_row_is_not_operator_approved(self) -> None:
        save_openai_account({"account_id": "acct-legacy"})

        with patch.object(
            orchestrator.codex_app_server,
            "account_status",
            return_value=("active", None, {"account_id": "acct-legacy"}),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account().get("account_id"), "acct-legacy")
        self.assertIsNone(read_proxy_openai_account_id())
        self.assertEqual(orchestrator.runtime_status_record("codex"), {"status": "awaiting_login"})

    def test_codex_initial_oauth_login_can_capture_first_trusted_account(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        state["codex_oauth"] = {
            "status": "awaiting_login",
            "device_code": "X",
            "login_id": "login",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        save_state(state)

        def account_status():
            # First-login capture now runs after the status poll (the poller is
            # what reads the completed login off the parked server), so the pin is
            # not seeded yet while the poller reads the account.
            self.assertIsNone(read_proxy_openai_account_id())
            return "active", None, {"account_id": "acct-local"}

        with (
            patch.object(orchestrator.codex_app_server, "read_completed_device_login_account_id", return_value="acct-local"),
            patch.object(orchestrator.codex_app_server, "account_status", side_effect=account_status),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "active")

        self.assertEqual(read_openai_account().get("account_id"), "acct-local")
        self.assertEqual(read_openai_account().get("operator_approval"), orchestrator.OPENAI_OPERATOR_APPROVAL)
        self.assertEqual(read_proxy_openai_account_id(), "acct-local")
        self.assertIsNone(load_state().get("codex_oauth"))

    def test_codex_active_reauth_closes_the_parked_login_server(self) -> None:
        # A reauth against an already-approved anchor parks a login server that
        # first-login capture skips; the active commit must still close it, or
        # every later status check keeps polling the leftover login process.
        save_approved_openai_account("acct-local")
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        state["codex_oauth"] = {
            "status": "awaiting_login",
            "device_code": "X",
            "login_id": "relogin",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        save_state(state)

        class _Parked:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        parked = _Parked()
        with orchestrator.codex_app_server._login_lock:
            orchestrator.codex_app_server._login_server = parked  # type: ignore[assignment]
            orchestrator.codex_app_server._login_id = "relogin"
        try:
            with patch.object(
                orchestrator.codex_app_server,
                "account_status",
                return_value=("active", None, {"account_id": "acct-local"}),
            ):
                self.assertEqual(orchestrator.refresh_runtime_status("codex"), "active")

            self.assertTrue(parked.closed)
            with orchestrator.codex_app_server._login_lock:
                self.assertIsNone(orchestrator.codex_app_server._login_server)
            self.assertIsNone(load_state().get("codex_oauth"))
        finally:
            orchestrator.codex_app_server.close_login_server()

    def test_codex_pending_oauth_without_completed_login_cannot_capture_first_account(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        state["codex_oauth"] = {
            "status": "awaiting_login",
            "device_code": "X",
            "login_id": "login",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        save_state(state)

        with (
            patch.object(orchestrator.codex_app_server, "read_completed_device_login_account_id", return_value=None),
            patch.object(
                orchestrator.codex_app_server,
                "account_status",
                return_value=("active", None, {"account_id": "acct-attacker"}),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())
        self.assertIsNotNone(load_state().get("codex_oauth"))

    def test_codex_refresh_rejects_agent_changed_account_id(self) -> None:
        save_approved_openai_account("acct-trusted")
        save_proxy_openai_account_id("acct-trusted")

        with patch.object(
            orchestrator.codex_app_server,
            "account_status",
            return_value=("active", None, {"account_id": "acct-attacker"}),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "error")

        self.assertEqual(read_openai_account().get("account_id"), "acct-trusted")
        self.assertIsNone(read_proxy_openai_account_id())
        record = orchestrator.runtime_status_record("codex")
        self.assertEqual(record["status"], "error")
        self.assertIn("account changed", record["error_message"])

    def test_codex_refresh_without_oauth_cannot_create_first_account_anchor(self) -> None:
        with patch.object(
            orchestrator.codex_app_server,
            "account_status",
            return_value=("active", None, {"account_id": "acct-attacker"}),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())

    def test_expired_codex_oauth_cannot_create_first_account_anchor(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        state["codex_oauth"] = {
            "status": "awaiting_login",
            "device_code": "X",
            "login_id": "login",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2000-06-08T00:10:00Z",
        }
        save_state(state)

        with (
            patch.object(
                orchestrator.codex_app_server,
                "read_completed_device_login_account_id",
                side_effect=AssertionError("expired OAuth must not seed provider pin"),
            ),
            patch.object(
                orchestrator.codex_app_server,
                "account_status",
                return_value=("active", None, {"account_id": "acct-attacker"}),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())
        self.assertIsNone(load_state().get("codex_oauth"))

    def test_expired_claude_oauth_cannot_create_first_account_anchor(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["claude_code"]["status"] = "awaiting_login"
        state["claude_oauth"] = {
            "status": "awaiting_code",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2000-06-08T00:10:00Z",
        }
        save_state(state)

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-attacker", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("expired OAuth must not attest unapproved Claude account"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "awaiting_login")

        self.assertEqual(read_claude_account(), {})
        self.assertEqual(read_proxy_claude_account(), {})
        self.assertIsNone(load_state().get("claude_oauth"))

    def test_claude_refresh_without_oauth_cannot_attest_or_anchor_first_account(self) -> None:
        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-attacker", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("unapproved Claude account must not trigger direct attestation egress"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "awaiting_login")

        self.assertEqual(orchestrator.runtime_status_record("claude_code"), {"status": "awaiting_login"})
        self.assertEqual(read_claude_account(), {})
        self.assertEqual(read_proxy_claude_account(), {})

    def test_claude_attestation_waits_for_policy_replacement_and_rechecks_disable(self) -> None:
        save_attested_claude_account("acct-trusted", access_token_sha256="0" * 64)

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-trusted", "access_token_sha256": "1" * 64}),
            ),
            patch.object(orchestrator, "_runtime_network_enabled", side_effect=[True, True, True, False, False]),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("disabled Claude provider must not trigger direct attestation egress"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "deactivated")

        self.assertEqual(read_claude_account()["account_id"], "acct-trusted")
        self.assertEqual(read_proxy_claude_account(), {})

    def test_claude_attestation_rechecks_operator_approval_before_helper_egress(self) -> None:
        save_attested_claude_account("acct-trusted", access_token_sha256="0" * 64)

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-trusted", "access_token_sha256": "1" * 64}),
            ),
            patch.object(orchestrator, "_claude_attestation_allowed", side_effect=[True, False]) as allowed,
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("revoked Claude approval must not trigger direct attestation egress"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "error")

        self.assertEqual(allowed.call_count, 2)
        self.assertEqual(read_claude_account()["account_id"], "acct-trusted")
        self.assertEqual(read_proxy_claude_account(), {})

    def test_codex_refresh_clears_seeded_proxy_pin_when_account_is_not_active(self) -> None:
        save_approved_openai_account("acct-local")
        with patch.object(orchestrator.codex_app_server, "account_status", return_value=("awaiting_login", None, None)):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account().get("account_id"), "acct-local")
        self.assertIsNone(read_proxy_openai_account_id())

    def test_reset_deletes_the_linked_account_guard_and_nothing_else(self) -> None:
        self.seed_tasks(make_task(1, "chat", runtime="codex"))
        snapshot = load_state()
        snapshot["agent_runtime_statuses"]["codex"]["status"] = "active"
        snapshot["codex_oauth"] = {
            "status": "awaiting_login",
            "device_code": "X",
            "login_id": "login",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        save_state(snapshot)
        save_approved_openai_account("acct-local")
        save_proxy_openai_account_id("acct-local")

        self.assertIsNone(orchestrator.reset_linked_account("codex"))

        # Guard state is gone, cached status no longer allows new claims, and
        # queued work remains queued until a fresh linked account is active.
        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())
        self.assertIsNone(load_state().get("codex_oauth"))
        self.assertEqual(orchestrator.runtime_status("codex"), "awaiting_login")
        self.assertEqual(self.task_status("task_1"), "queued")
        reset_events = [event for event in read_agent_events() if event["event_type"] == "agent_runtime.linked_account_reset"]
        self.assertEqual([event.get("payload") for event in reset_events], [{"agent_runtime": "codex"}])

    def test_reset_kills_running_runtime_tasks(self) -> None:
        self.seed_tasks(make_task(1, "chat", status="running", runtime="codex"))
        server = FakeServer()
        server.started = 1
        with orchestrator._POOL_LOCK:
            orchestrator._POOL["codex:chat"] = orchestrator._Slot(server, "codex", "chat", True, 1.0, "task_1")
        save_approved_openai_account("acct-local")
        save_proxy_openai_account_id("acct-local")

        self.assertIsNone(orchestrator.reset_linked_account("codex"))

        self.assertEqual(self.task_status("task_1"), "failed")
        self.assertTrue(server.closed)
        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())

    def test_reset_continues_when_runtime_close_fails(self) -> None:
        class FailingCloseServer(FakeServer):
            def close(self) -> None:
                raise PermissionError("cannot signal helper")

        self.seed_tasks(
            make_task(1, "chat", status="running", runtime="codex"),
            make_task(2, "other", status="running", runtime="codex"),
        )
        bad_server = FailingCloseServer()
        bad_server.started = 1
        good_server = FakeServer()
        good_server.started = 1
        with orchestrator._POOL_LOCK:
            orchestrator._POOL["codex:chat"] = orchestrator._Slot(bad_server, "codex", "chat", True, 1.0, "task_1")
            orchestrator._POOL["codex:other"] = orchestrator._Slot(good_server, "codex", "other", True, 1.0, "task_2")
        save_approved_openai_account("acct-local")
        save_proxy_openai_account_id("acct-local")

        self.assertIsNone(orchestrator.reset_linked_account("codex"))

        self.assertEqual(self.task_status("task_1"), "failed")
        self.assertEqual(self.task_status("task_2"), "failed")
        self.assertFalse(bad_server.closed)
        self.assertTrue(good_server.closed)
        self.assertEqual(orchestrator._POOL, {})
        self.assertEqual(orchestrator._CLOSING_THREADS, {"codex:chat": 1})
        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())

    def test_reset_during_slow_probe_cannot_resurrect_account(self) -> None:
        # The stale probe classified the runtime before the reset; the anchor
        # check inside the commit mutation is what stops it from re-approving
        # the logged-out account.
        save_approved_openai_account("acct-local")

        def account_status():
            self.assertIsNone(orchestrator.reset_linked_account("codex"))
            return "active", None, {"account_id": "acct-local"}

        with patch.object(orchestrator.codex_app_server, "account_status", side_effect=account_status):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())

        # Even if local agent credentials survive outside this orchestrator
        # helper, they stay unapproved: the next probe still reports
        # awaiting_login and re-anchors nothing.
        with patch.object(
            orchestrator.codex_app_server,
            "account_status",
            return_value=("active", None, {"account_id": "acct-local"}),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")
        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())

    def test_reset_between_refresh_commit_and_pin_write_leaves_no_pin(self) -> None:
        # The pin table is written after the commit mutation; a reset landing
        # in that gap must win. The post-write anchor re-check clears the
        # stale pin immediately.
        save_approved_openai_account("acct-local")
        real_sync = orchestrator.proxy_state_client.sync_openai_account_id
        probe_done: list[bool] = []
        raced: list[str] = []

        def sync_with_reset_race(account_id):
            if account_id and probe_done and not raced:
                raced.append(account_id)
                self.assertIsNone(orchestrator.reset_linked_account("codex"))
            real_sync(account_id)

        def account_status():
            probe_done.append(True)
            return "active", None, {"account_id": "acct-local"}

        with (
            patch.object(orchestrator.proxy_state_client, "sync_openai_account_id", side_effect=sync_with_reset_race),
            patch.object(orchestrator.codex_app_server, "account_status", side_effect=account_status),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(raced, ["acct-local"])
        self.assertEqual(orchestrator.runtime_status("codex"), "awaiting_login")
        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())

    def test_stale_runtime_refresh_cannot_overwrite_disabled_policy_state(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "active"
        save_state(state)
        save_approved_openai_account("acct-old")
        save_proxy_openai_account_id("acct-old")

        def status_after_policy_flip():
            save_policy(
                {"managed_network_integrations": {}, "allowed_network_access": {}},
                "2026-06-08T00:00:02Z",
            )
            return "awaiting_login", None, None

        with patch.object(orchestrator.codex_app_server, "account_status", side_effect=status_after_policy_flip):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "deactivated")

        state = load_state()
        self.assertEqual(state["agent_runtime_statuses"]["codex"]["status"], "deactivated")
        self.assertEqual(read_openai_account().get("account_id"), "acct-old")
        self.assertIsNone(read_proxy_openai_account_id())

    def test_runtime_refresh_rechecks_disabled_policy_inside_final_state_write(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        save_state(state)

        with (
            patch.object(orchestrator.codex_app_server, "account_status", return_value=("active", None, "acct-new")),
            patch.object(orchestrator, "_runtime_network_enabled", side_effect=[True, True, True, False]),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "deactivated")

        state = load_state()
        self.assertEqual(state["agent_runtime_statuses"]["codex"]["status"], "deactivated")
        self.assertIsNone(read_openai_account().get("account_id"))
        self.assertIsNone(read_proxy_openai_account_id())

    def test_runtime_refresh_clears_pin_if_policy_disables_after_account_save(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        state["codex_oauth"] = {
            "status": "awaiting_login",
            "device_code": "X",
            "login_id": "login",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2099-06-08T00:10:00Z",
        }
        save_state(state)

        with (
            patch.object(orchestrator.codex_app_server, "read_completed_device_login_account_id", return_value="acct-new"),
            patch.object(orchestrator.codex_app_server, "account_status", return_value=("active", None, "acct-new")),
            patch.object(orchestrator, "_runtime_network_enabled", side_effect=[True, True, True, True, False]),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "deactivated")

        state = load_state()
        self.assertEqual(state["agent_runtime_statuses"]["codex"]["status"], "deactivated")
        self.assertEqual(read_openai_account().get("account_id"), "acct-new")
        self.assertIsNone(read_proxy_openai_account_id())

    def test_thread_stays_unclaimable_while_its_old_server_is_closing(self) -> None:
        # The kill/teardown window: while a thread's previous app-server is
        # still shutting down, a queued task on that thread must not start (its
        # thread/resume could race the dying process and fork the conversation).
        # Tasks on other threads are unaffected.
        release = threading.Event()

        class SlowCloseServer(FakeServer):
            def close(self) -> None:
                if not release.wait(timeout=10):
                    raise AssertionError("test never released the slow close")
                super().close()

        old = SlowCloseServer()
        self.seed_tasks(make_task(1, "chat"), make_task(2, "other"))
        with orchestrator._POOL_LOCK:
            orchestrator._begin_close_locked("codex", "chat")
        closer = threading.Thread(target=orchestrator._finish_close, args=("codex:chat", old))
        closer.start()
        try:
            with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
                orchestrator.run_next_task()  # skips chat, claims the other thread
                orchestrator.run_next_task()  # nothing else is claimable yet
            self.assertEqual(self.task_status("task_1"), "queued")
            self.assertEqual(self.task_status("task_2"), "completed")
        finally:
            release.set()
            closer.join(timeout=10)
        self.assertNotIn("codex:chat", orchestrator._CLOSING_THREADS)
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            orchestrator.run_next_task()
        self.assertEqual(self.task_status("task_1"), "completed")

    def test_server_acquire_failure_fails_the_task_instead_of_orphaning_it(self) -> None:
        # The task is marked RUNNING at claim time. If spawning its server then
        # blows up, the failure must land on the task; an escaped exception
        # would leave it RUNNING forever with no worker attached.
        self.seed_tasks(make_task(1, "chat"))

        def exploding_server(command: object = None) -> FakeServer:
            raise OSError("cannot spawn app-server")

        with patch.object(orchestrator.codex_app_server, "CodexAppServer", exploding_server):
            orchestrator.run_next_task()
        self.assertEqual(self.task_status("task_1"), "failed")
        self.assertNotIn("codex:chat", orchestrator._POOL)

    def test_busy_pool_slot_blocks_same_thread_claim_until_release(self) -> None:
        # A task can be terminal in state while its worker has not yet released
        # the slot (finish happens before the finally). The claim rule must
        # treat such a thread as busy, or a same-thread task could start while
        # the previous server is still attached to the Codex thread.
        self.seed_tasks(make_task(1, "chat", status="failed"), make_task(2, "chat"), make_task(3, "other"))
        stale = FakeServer()
        stale.started = 1
        with orchestrator._POOL_LOCK:
            orchestrator._POOL["codex:chat"] = orchestrator._Slot(stale, "codex", "chat", True, 0.0, "task_1")

        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            orchestrator.run_next_task()  # must skip chat, claim the other thread
            orchestrator.run_next_task()  # chat is still blocked
        self.assertEqual(self.task_status("task_2"), "queued")
        self.assertEqual(self.task_status("task_3"), "completed")

        # The stale worker releases (unhealthy); the thread becomes claimable.
        orchestrator._release_server("codex", "chat", stale, healthy=False)
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            orchestrator.run_next_task()
        self.assertEqual(self.task_status("task_2"), "completed")

    def test_tasks_stay_queued_until_the_runtime_is_active(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        save_state(state)
        self.seed_tasks(make_task(1, "chat"))
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            orchestrator.run_next_task()
        self.assertEqual(self.task_status("task_1"), "queued")
        self.assertEqual(FakeServer.instances, [])

    def test_status_loop_rechecks_each_runtime_on_its_own_cadence(self) -> None:
        class StopLoop(Exception):
            pass

        now = [0.0]
        sleeps = {"count": 0}
        calls: list[str] = []

        def fake_refresh(runtime_type: str) -> str:
            calls.append(runtime_type)
            if runtime_type == "codex" and calls.count("codex") > 1:
                raise AssertionError("active Codex was rechecked at the pending-runtime cadence")
            return "active" if runtime_type == "codex" else "awaiting_login"

        def fake_sleep(seconds: float) -> None:
            sleeps["count"] += 1
            if sleeps["count"] >= 2:
                raise StopLoop
            now[0] += seconds

        with (
            patch.object(orchestrator, "refresh_runtime_status", fake_refresh),
            patch.object(orchestrator.time, "monotonic", lambda: now[0]),
            patch.object(orchestrator.time, "sleep", fake_sleep),
        ):
            with self.assertRaises(StopLoop):
                orchestrator.runtime_status_loop()

        self.assertEqual(calls.count("codex"), 1)
        self.assertEqual(calls.count("claude_code"), 2)

    def test_policy_change_refreshes_reenabled_runtime_without_waiting_for_poll_cadence(self) -> None:
        save_policy(
            {
                "managed_network_integrations": {"openai": {"enabled": True}},
                "allowed_network_access": {},
            },
            "2026-06-08T00:00:01Z",
        )
        calls: list[str] = []
        background: list[tuple[str, ...]] = []

        class InlineThread:
            def __init__(self, target, args, daemon):  # type: ignore[no-untyped-def]
                self.target = target
                self.args = args
                self.daemon = daemon

            def start(self) -> None:
                background.append(self.args[0])
                self.target(*self.args)

        def fake_refresh(runtime_type: str) -> str:
            calls.append(runtime_type)
            return "active"

        with (
            patch.object(orchestrator, "refresh_runtime_status", fake_refresh),
            patch.object(orchestrator.threading, "Thread", InlineThread),
        ):
            orchestrator.reconcile_runtime_status_after_policy_change()

        # The disabled runtime deactivates directly (no provider probe, no
        # refresh serialization); only the enabled one is refreshed, in the
        # background.
        self.assertEqual(calls, ["codex"])
        self.assertEqual(background, [("codex",)])
        self.assertEqual(orchestrator.runtime_status("claude_code"), "deactivated")



class StartWorkersOrderTests(unittest.TestCase):
    def test_start_workers_refreshes_github_credentials_before_workers(self) -> None:
        order: list[str] = []
        with (
            patch(
                "host.runtime.orchestrator.github_credential.reconcile",
                side_effect=lambda: order.append("refresh"),
            ),
            patch(
                "host.runtime.orchestrator.threading.Thread",
                side_effect=lambda *a, **k: order.append("thread") or _NoopThread(),
            ),
        ):
            orchestrator.start_workers()
        # The synchronous refresh must land before any worker/poller thread
        # is spawned, so a queued task cannot start on a stale token.
        self.assertEqual(order[0], "refresh")
        self.assertIn("thread", order)


class _NoopThread:
    def start(self) -> None:
        return None

if __name__ == "__main__":
    unittest.main()
