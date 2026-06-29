from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from host.runtime import orchestrator
from host.runtime.network_policy import anthropic_request_denied, save_policy
from host.runtime.state import (
    load_state,
    read_agent_events,
    read_claude_account,
    read_openai_account_id,
    read_proxy_claude_account,
    read_proxy_openai_account_id,
    save_claude_account,
    save_openai_account_id,
    save_proxy_claude_account,
    save_proxy_openai_account_id,
    save_state,
    write_json,
)


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


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
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
        self.addCleanup(orchestrator._POOL.clear)
        self.addCleanup(orchestrator._CLOSING_THREADS.clear)
        save_policy(
            {
                "managed_ai_provider_network_access": {"openai": True, "claude": True},
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
            {"managed_ai_provider_network_access": {}, "allowed_network_access": {}},
            "2026-06-08T00:00:01Z",
        )
        self.seed_tasks(make_task(1, "stale-active-codex"), make_task(2, "stale-active-claude", runtime="claude_code"))

        self.assertIsNone(orchestrator._claim_next_task())
        self.assertEqual(self.task_status("task_1"), "queued")
        self.assertEqual(self.task_status("task_2"), "queued")

    def test_codex_and_claude_have_independent_three_task_claim_caps(self) -> None:
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
                return_value=("active", None, {"account_id": "acct", "access_token_sha256": "hash"}),
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
                return_value=("active", None, {"account_id": "acct", "access_token_sha256": "hash"}),
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
                return_value=("active", None, {"account_id": "acct", "access_token_sha256": "hash"}),
            ),
        ):
            orchestrator.run_next_task()

        self.assertEqual(seen, [None, "claude-session-1"])

    def test_claude_task_refreshes_account_pin_before_turn(self) -> None:
        self.seed_tasks(make_task(1, "chat", runtime="claude_code"))
        old_token = "old-token"
        fresh_token = "fresh-token"
        policy = {
            "allowed_network_access": {
                "api.anthropic.com": {"allow_http_methods": ["GET", "POST"], "anthropic_account_guard": True}
            }
        }
        save_claude_account(
            {
                "account_id": "acct",
                "access_token_sha256": hashlib.sha256(old_token.encode()).hexdigest(),
            }
        )
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

        def fake_run_turn(server, input_message, session_id, steers, on_message, steer_delivered):
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
            return "claude-session-1", "done"

        with (
            patch.object(orchestrator.claude_code, "ClaudeCodeSession", FakeServer),
            patch.object(orchestrator.claude_code, "run_turn", fake_run_turn),
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=(
                    "active",
                    None,
                    {"account_id": "acct", "access_token_sha256": hashlib.sha256(fresh_token.encode()).hexdigest()},
                ),
            ),
        ):
            orchestrator.run_next_task()

        self.assertEqual(self.task_status("task_1"), "completed")

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
        save_openai_account_id("acct-old")
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
        self.assertIsNone(read_openai_account_id())
        self.assertIsNone(read_proxy_openai_account_id())
        self.assertFalse(codex_busy.closed)
        self.assertIn("codex:codex-running", orchestrator._POOL)

    def test_stale_runtime_refresh_cannot_overwrite_disabled_policy_state(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "active"
        save_state(state)
        save_openai_account_id("acct-old")
        save_proxy_openai_account_id("acct-old")

        def status_after_policy_flip():
            save_policy(
                {"managed_ai_provider_network_access": {}, "allowed_network_access": {}},
                "2026-06-08T00:00:02Z",
            )
            return "awaiting_login", None, None

        with patch.object(orchestrator.codex_app_server, "account_status", side_effect=status_after_policy_flip):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "deactivated")

        state = load_state()
        self.assertEqual(state["agent_runtime_statuses"]["codex"]["status"], "deactivated")
        self.assertIsNone(read_openai_account_id())
        self.assertIsNone(read_proxy_openai_account_id())

    def test_runtime_refresh_rechecks_disabled_policy_inside_final_state_write(self) -> None:
        state = load_state()
        state["agent_runtime_statuses"]["codex"]["status"] = "awaiting_login"
        save_state(state)

        with (
            patch.object(orchestrator.codex_app_server, "account_status", return_value=("active", None, "acct-new")),
            patch.object(orchestrator, "_runtime_network_enabled", side_effect=[True, True, False]),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "deactivated")

        state = load_state()
        self.assertEqual(state["agent_runtime_statuses"]["codex"]["status"], "deactivated")
        self.assertIsNone(read_openai_account_id())
        self.assertIsNone(read_proxy_openai_account_id())

    def test_runtime_refresh_clears_pin_if_policy_disables_after_account_save(self) -> None:
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
        self.assertIsNone(read_openai_account_id())
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
                "managed_ai_provider_network_access": {"openai": True},
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

        self.assertEqual(calls, ["claude_code", "codex"])
        self.assertEqual(background, [("codex",)])


if __name__ == "__main__":
    unittest.main()
