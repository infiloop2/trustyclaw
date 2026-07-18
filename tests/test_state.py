"""Tests for the admin-state storage accessors (host.runtime.core.state).

These pin the contracts the rest of the runtime is built on: mutation() spans
whole check-then-act cycles (no lost updates), an exception rolls the whole
transaction back (including events appended inside it), readers see committed
snapshots (never a torn multi-row write), and event seqs never appear twice in
the log. They run against the scratch cluster from pg_harness.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import patch

import pg_harness

from host.runtime.core import db, pgclient, secretbox, state
from state_seed import read_agent_events, read_network_events
from host.runtime.core.state import (
    load_config,
    network_proxy_cert_files,
    read_claude_account,
    read_openai_account,
    read_proxy_claude_account,
    read_proxy_openai_account_id,
    save_config,
    save_claude_account,
    save_openai_account,
    save_proxy_claude_account,
    save_proxy_openai_account_id,
)


def make_task(number: int, status: str = "queued", thread_id: str = "t1") -> dict[str, object]:
    return {
        "task_id": f"task_{number}",
        "status": status,
        "agent_runtime": "codex",
        "model": "gpt-5.6-terra",
        "effort": "high",
        "thread_id": thread_id,
        "input_message": f"task {number}",
        "steer_messages": [],
        "created_at": "2026-06-08T00:00:00Z",
        "updated_at": f"2026-06-08T00:00:{number % 60:02d}Z",
    }


def insert_task(cur: Any, task: dict[str, object]) -> None:
    state.save_thread_session(
        cur,
        str(task["agent_runtime"]),
        str(task["thread_id"]),
        None,
        str(task["updated_at"]),
        str(task["model"]),
        str(task["effort"]),
    )
    state.insert_task(cur, task)


class StateStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_STATE_DIR": self.temp_dir.name})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_mutation_persists_on_normal_exit_and_rolls_back_on_exception(self) -> None:
        with state.mutation() as cur:
            insert_task(cur, make_task(1))
        with self.assertRaises(RuntimeError):
            with state.mutation() as cur:
                task = state.get_task("task_1", cur)
                assert task is not None
                task["status"] = "running"
                state.save_task(cur, task)
                state.append_agent_event(cur, "task.started", "task_1", {})
                raise RuntimeError("abort the transaction")
        # The status write and the event both rolled back together.
        task = state.get_task("task_1")
        assert task is not None
        self.assertEqual(task["status"], "queued")
        self.assertEqual(read_agent_events(), [])

    def test_task_rows_round_trip(self) -> None:
        with state.mutation() as cur:
            task = make_task(1)
            task["steer_messages"] = ["steer me"]
            insert_task(cur, task)
        loaded = state.get_task("task_1")
        assert loaded is not None
        self.assertEqual(loaded["input_message"], "task 1")
        self.assertEqual(loaded["steer_messages"], ["steer me"])
        self.assertIsNone(loaded["output_message"])
        self.assertEqual([t["task_id"] for t in state.active_tasks()], ["task_1"])

    def test_thread_session_configuration_is_immutable(self) -> None:
        with state.mutation() as cur:
            state.save_thread_session(
                cur,
                "codex",
                "fixed-thread",
                None,
                "2026-06-08T00:00:00Z",
                "gpt-5.6-terra",
                "high",
            )

        with self.assertRaises(ValueError), state.mutation() as cur:
            state.save_thread_session(
                cur,
                "codex",
                "fixed-thread",
                None,
                "2026-06-08T00:00:01Z",
                "gpt-5.6-sol",
                "high",
            )

        with state.mutation() as cur:
            config = state.thread_session_config(cur, "fixed-thread")
        assert config is not None
        self.assertEqual((config["model"], config["effort"]), ("gpt-5.6-terra", "high"))

    def test_task_number_allocation_is_dense_and_rolls_back(self) -> None:
        with state.mutation() as cur:
            self.assertEqual(state.allocate_task_number(cur), 1)
        with self.assertRaises(RuntimeError):
            with state.mutation() as cur:
                self.assertEqual(state.allocate_task_number(cur), 2)
                raise RuntimeError("abort: the number must be reusable")
        with state.mutation() as cur:
            self.assertEqual(state.allocate_task_number(cur), 2)

    def test_concurrent_mutations_never_lose_increments(self) -> None:
        # The mutation lock must span each whole read-modify-write cycle:
        # interleaved cycles would drop increments. 8 threads x 25 must land.
        threads, per_thread = 8, 25
        barrier = threading.Barrier(threads)
        errors: list[BaseException] = []

        def work() -> None:
            try:
                barrier.wait(timeout=10)
                for _ in range(per_thread):
                    with state.mutation() as cur:
                        state.allocate_task_number(cur)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        workers = [threading.Thread(target=work) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
        self.assertEqual(errors, [])
        with state.mutation() as cur:
            self.assertEqual(state.allocate_task_number(cur), threads * per_thread + 1)

    def test_reads_inside_a_mutation_see_committed_state_and_do_not_deadlock(self) -> None:
        # Helpers called from inside a mutation may take read-only snapshots
        # (the lock is reentrant and reads run on their own connections); the
        # snapshot sees the last *committed* state, not the in-progress write.
        with state.mutation() as cur:
            state.set_oauth_login(cur, "claude", {"status": "awaiting_code", "login_url": "u", "expires_at": "e"})
            self.assertIsNone(state.oauth_login("claude"))
        login = state.oauth_login("claude")
        assert login is not None
        self.assertEqual(login["status"], "awaiting_code")

    def test_concurrent_readers_never_see_a_torn_multi_row_write(self) -> None:
        # A writer keeps two oauth rows in sync inside one mutation; a reader
        # must never observe them apart (each read is one statement snapshot).
        def read_pair() -> tuple[Any, Any]:
            with db.transaction() as cur:
                cur.execute("SELECT runtime, status FROM oauth_logins")
                rows = dict(cur.fetchall())
            return rows.get("codex"), rows.get("claude")

        def login_pair(value: int) -> tuple[dict[str, Any], dict[str, Any]]:
            codex = {"status": f"step-{value}", "login_url": "u", "expires_at": "e",
                     "device_code": "D", "login_id": "L"}
            claude = {"status": f"step-{value}", "login_url": "u", "expires_at": "e"}
            return codex, claude

        with state.mutation() as cur:
            codex, claude = login_pair(0)
            state.set_oauth_login(cur, "codex", codex)
            state.set_oauth_login(cur, "claude", claude)
        stop = threading.Event()
        errors: list[str] = []

        def reader() -> None:
            while not stop.is_set():
                left, right = read_pair()
                if left != right:
                    errors.append(f"torn read: codex={left} claude={right}")
                    return

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        try:
            for value in range(1, 100):
                with state.mutation() as cur:
                    codex, claude = login_pair(value)
                    state.set_oauth_login(cur, "codex", codex)
                    state.set_oauth_login(cur, "claude", claude)
        finally:
            stop.set()
            reader_thread.join(timeout=30)
        self.assertEqual(errors, [])

    def test_aborted_mutation_never_leaves_a_duplicate_event_seq(self) -> None:
        with self.assertRaises(RuntimeError):
            with state.mutation() as cur:
                state.append_agent_event(cur, "test.aborted", None, {})
                raise RuntimeError("abort after allocating a seq")
        with state.mutation() as cur:
            state.append_agent_event(cur, "test.committed", None, {})
        events = read_agent_events()
        self.assertEqual([event["event_type"] for event in events], ["test.committed"])
        seqs = [event["seq"] for event in events]
        self.assertEqual(len(seqs), len(set(seqs)), f"duplicate event seqs: {seqs}")

    def test_event_pages_are_newest_first_and_cursor_bounded(self) -> None:
        with state.mutation() as cur:
            for index in range(8):
                state.append_agent_event(cur, "task.message", "task_1" if index % 2 else None, {"message": f"m{index}", "source": "user"})
        page = state.page_agent_events_before(None, limit=5)
        self.assertEqual(len(page), 5)
        seqs = [event["seq"] for event in page]
        self.assertEqual(seqs, sorted(seqs, reverse=True))
        older = state.page_agent_events_before(seqs[-1], limit=5)
        self.assertTrue(older)
        self.assertTrue(all(event["seq"] < seqs[-1] for event in older))
        task_page = state.page_task_events("task_1", None)
        self.assertTrue(all(event["task_id"] == "task_1" for event in task_page))

    def test_thread_events_span_all_tasks_oldest_first_and_page_by_since(self) -> None:
        with state.mutation() as cur:
            insert_task(cur, make_task(1, thread_id="chat"))
            insert_task(cur, make_task(2, thread_id="chat"))
            insert_task(cur, make_task(3, thread_id="other"))
            state.append_agent_event(cur, "task.message", "task_1", {"message": "a", "source": "user"})
            state.append_agent_event(cur, "task.message", "task_2", {"message": "b", "source": "agent"})
            state.append_agent_event(cur, "task.message", "task_3", {"message": "c", "source": "agent"})
            state.append_agent_event(cur, "task.message", "task_1", {"message": "d", "source": "agent"})
        page = state.page_thread_events("chat", None, 100)
        # Both of the thread's tasks, oldest-first, and never the other thread's.
        self.assertEqual([event["payload"]["message"] for event in page], ["a", "b", "d"])
        seqs = [event["seq"] for event in page]
        self.assertEqual(seqs, sorted(seqs))
        # A since cursor returns only newer events.
        rest = state.page_thread_events("chat", seqs[0], 100)
        self.assertEqual([event["payload"]["message"] for event in rest], ["b", "d"])
        # limit caps the page.
        self.assertEqual(len(state.page_thread_events("chat", None, 1)), 1)

    def test_prune_keeps_the_newest_finished_tasks_and_sessions(self) -> None:
        with state.mutation() as cur:
            for number in range(1, 8):
                insert_task(cur, make_task(number, status="completed", thread_id=f"t{number}"))
            insert_task(cur, make_task(8, status="queued", thread_id="t8"))
            for number in range(1, 8):
                state.save_thread_session(
                    cur,
                    "codex",
                    f"t{number}",
                    f"ct_{number}",
                    f"2026-06-08T00:00:{number:02d}Z",
                    "gpt-5.6-terra",
                    "high",
                )
        with state.mutation() as cur:
            state.prune_finished_tasks(cur, 3)
            state.prune_thread_sessions(cur, "codex", 3)
        remaining = {task["task_id"] for task in [t for t in state.active_tasks()]}
        self.assertEqual(remaining, {"task_8"})  # active tasks always survive
        kept = {task["task_id"] for task in state.tasks_for_thread("t7", 10)}
        self.assertEqual(kept, {"task_7"})  # among the newest three finished
        self.assertIsNone(state.get_task("task_1"))
        with state.mutation() as cur:
            self.assertIsNone(state.thread_session(cur, "codex", "t1"))
            self.assertIsNotNone(state.thread_session(cur, "codex", "t7"))

    def test_event_logs_prune_to_the_newest_cap(self) -> None:
        # Retention is a primary-key range delete below MAX(seq) - cap: cheap
        # enough for the append cadence even at the 10M production caps, pinned
        # here with small ones.
        with state.mutation() as cur:
            for index in range(8):
                state.append_agent_event(cur, "task.message", None, {"message": f"m{index}", "source": "user"})
        with patch.object(state, "MAX_EVENTS", 5), state.mutation() as cur:
            state.prune_agent_events(cur)
        seqs = [event["seq"] for event in read_agent_events()]
        self.assertEqual(len(seqs), 5)
        self.assertEqual(seqs, sorted(seqs))

        for index in range(8):
            state.append_network_event("https", "GET", "example.com", 443, f"/p{index}", "", True)
        with patch.object(state, "MAX_EVENTS", 5), state.mutation() as cur:
            state.prune_network_events(cur)
        network_seqs = [event["seq"] for event in read_network_events()]
        self.assertEqual(len(network_seqs), 5)
        self.assertEqual(network_seqs, sorted(network_seqs))

    def test_network_event_url_fields_are_size_capped(self) -> None:
        # The agent's own request stream feeds this log; without field caps a
        # hostile client could turn the row cap into unbounded disk growth.
        state.append_network_event(
            "https", "GET", "h" * 600, 443, "/" + "p" * 5000, "q" * 5000, False, reason_code="r" * 900
        )
        event = read_network_events()[-1]
        self.assertEqual(len(event["host"]), 512)
        self.assertEqual(len(event["path"]), 2048)
        self.assertEqual(len(event["query"]), 2048)
        self.assertEqual(len(event["reason_code"]), 128)

    def test_proxy_cert_files_can_be_split_from_admin_state(self) -> None:
        # TLS material is the one proxy-owned file family left: the ssl module
        # and openssl consume paths, and the CA key stays out of the database.
        with tempfile.TemporaryDirectory() as proxy_tmp, patch.dict(
            "os.environ",
            {"TRUSTYCLAW_STATE_DIR": self.temp_dir.name, "TRUSTYCLAW_PROXY_STATE_DIR": proxy_tmp},
        ):
            cert_files = network_proxy_cert_files("example.com")
            self.assertEqual(cert_files.ca_cert, Path(proxy_tmp) / "network_proxy_ca.crt")
            self.assertEqual(cert_files.ca_key, Path(proxy_tmp) / "network_proxy_ca.key")
            self.assertEqual(cert_files.directory, Path(proxy_tmp) / "generated-certs")

    def test_proxy_pins_and_network_policy_live_in_the_database(self) -> None:
        save_proxy_openai_account_id("acct_pin")
        save_proxy_claude_account({"access_token_sha256": "d" * 64})
        self.assertEqual(read_proxy_openai_account_id(), "acct_pin")
        self.assertEqual(read_proxy_claude_account(), {"access_token_sha256": "d" * 64})
        self.assertIsNone(state.network_policy_record())
        state.save_network_policy({"network_integrations": {}}, "2026-06-08T00:00:00Z")
        record = state.network_policy_record()
        assert record is not None
        self.assertEqual(record["updated_at"], "2026-06-08T00:00:00Z")

    def test_provider_account_anchor_is_immutable_in_the_database(self) -> None:
        # The anchor guard trigger: an anchored row accepts metadata rewrites
        # for the same account and the reset that clears it, and nothing else.
        anchored = {
            "account_id": "acct-1",
            "identity_attestation": "anthropic_oauth_profile",
            "email": "op@example.com",
        }
        save_claude_account(anchored)
        save_claude_account(anchored | {"claude_usage": {"weekly_used_percent": 3}})
        self.assertEqual(read_claude_account()["claude_usage"], {"weekly_used_percent": 3})
        with self.assertRaises(pgclient.Error):
            save_claude_account(anchored | {"account_id": "acct-2"})
        with self.assertRaises(pgclient.Error):
            # Stripping the approval marker would demote the row so a second
            # write could re-anchor it; the guard refuses the demotion.
            save_claude_account({"account_id": "acct-1", "email": "op@example.com"})
        with self.assertRaises(pgclient.Error):
            with state.mutation() as cur:
                cur.execute("DELETE FROM provider_accounts WHERE provider = %s", ("claude",))
        self.assertEqual(read_claude_account()["account_id"], "acct-1")
        save_claude_account(None)  # the operator reset clears the anchor
        save_claude_account({"account_id": "acct-2", "identity_attestation": "anthropic_oauth_profile"})
        self.assertEqual(read_claude_account()["account_id"], "acct-2")

    def test_provider_account_rows_without_an_anchor_are_unrestricted(self) -> None:
        # Legacy or unapproved rows carry no approval marker: they are not
        # anchors, and the first-capture flow may overwrite them freely.
        save_openai_account({"account_id": "legacy"})
        save_openai_account({"account_id": "other"})
        save_openai_account({"account_id": "acct-1", "operator_approval": "codex_device_login"})
        with self.assertRaises(pgclient.Error):
            save_openai_account({"account_id": "acct-2", "operator_approval": "codex_device_login"})
        self.assertEqual(read_openai_account()["account_id"], "acct-1")

    def test_network_policy_round_trips_claude_web_search(self) -> None:
        controls = {
            "network_integrations": {"claude": {"enabled": True, "web_search": True}},
        }
        state.save_network_policy(controls, "2026-06-08T00:00:00Z")
        record = state.network_policy_record()
        assert record is not None
        self.assertEqual(record["controls"], controls)
        self.assertTrue(state.read_claude_web_search())
        # Disabling web search clears the row and the read helper reports off.
        state.save_network_policy(
            {"network_integrations": {"claude": {"enabled": True}}},
            "2026-06-08T00:00:01Z",
        )
        self.assertFalse(state.read_claude_web_search())
        record = state.network_policy_record()
        assert record is not None
        self.assertNotIn("web_search", record["controls"]["network_integrations"]["claude"])

    def test_network_policy_round_trips_integrations_and_github_repos(self) -> None:
        controls = {
            "network_integrations": {
                "openai": {"enabled": True},
                "github": {
                    "enabled": True,
                    "write_repositories": [
                        {"owner": "infiloop2", "repo": "trustyclaw"},
                        {"owner": "infiloop2", "repo": "infibot"},
                    ],
                },
                "npm_packages": {"enabled": True},
                "custom": {"domains": {"example.com": {"allow_http_methods": ["GET"]}}},
            },
        }
        state.save_network_policy(controls, "2026-06-08T00:00:00Z")
        record = state.network_policy_record()
        assert record is not None
        self.assertEqual(record["controls"], controls)
        # Narrowing the repository list round-trips too.
        narrowed = {
            "network_integrations": {
                "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "trustyclaw"}]},
            },
        }
        state.save_network_policy(narrowed, "2026-06-08T00:00:01Z")
        record = state.network_policy_record()
        assert record is not None
        self.assertEqual(record["controls"], narrowed)
        # Replacing with a github-free policy clears the repository rows too.
        state.save_network_policy(
            {"network_integrations": {"claude": {"enabled": True}}},
            "2026-06-08T00:00:02Z",
        )
        record = state.network_policy_record()
        assert record is not None
        self.assertEqual(
            record["controls"]["network_integrations"], {"claude": {"enabled": True}}
        )

    def test_require_dot_github_approval_round_trips(self) -> None:
        controls = {
            "network_integrations": {
                "github": {
                    "enabled": True,
                    "write_repositories": [{"owner": "infiloop2", "repo": "trustyclaw"}],
                    "require_dot_github_approval": True,
                }
            },
        }
        state.save_network_policy(controls, "2026-06-08T00:00:00Z")
        record = state.network_policy_record()
        assert record is not None
        self.assertEqual(record["controls"], controls)

    def test_pending_pushes_lifecycle(self) -> None:
        state.enqueue_pending_push(
            "abc123",
            "infiloop2",
            "trustyclaw",
            [{"old": "0" * 40, "new": "1" * 40, "ref": "refs/heads/main"}],
            [".github/workflows/ci.yml"],
        )
        pending = state.read_pending_pushes()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], "abc123")
        self.assertEqual(pending[0]["changed_paths"], [".github/workflows/ci.yml"])
        self.assertEqual(pending[0]["status"], "pending")
        self.assertEqual(state.get_pending_push("abc123")["ref_updates"][0]["ref"], "refs/heads/main")
        approved = state.resolve_pending_push("abc123", "approved")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(state.get_pending_push("abc123")["status"], "approved")
        # Resolving a row that is no longer pending is a programming error
        # (the caller checks under RESOLVE_LOCK) and fails loudly.
        with self.assertRaises(RuntimeError):
            state.resolve_pending_push("abc123", "rejected")
        self.assertEqual(state.get_pending_push("abc123")["status"], "approved")

    def test_encrypt_secret_refuses_non_string_values(self) -> None:
        # Secrets are either absent or non-empty strings; anything else is a
        # programming error that must never be stored (unencrypted or at all).
        self.assertIsNone(state._encrypt_secret(None))
        for bad in ("", 42, b"bytes", {"nested": "value"}):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                state._encrypt_secret(bad)

    def test_github_credential_round_trips_and_masks_metadata(self) -> None:
        self.assertEqual(state.read_github_credential_metadata(), {"configured": False})
        state.save_github_credential(
            {
                "mode": "pat",
                "token": "github_pat_secret",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "not_checked"},
            }
        )
        self.assertEqual(state.read_github_credential()["token"], "github_pat_secret")
        with state.db.transaction() as cur:
            cur.execute("SELECT token FROM github_credential")
            raw_token = cur.fetchone()[0]
        self.assertTrue(raw_token.startswith("enc:v1:"))
        self.assertNotIn("github_pat_secret", raw_token)
        metadata = state.read_github_credential_metadata()
        self.assertEqual(metadata["mode"], "pat")
        self.assertTrue(metadata["configured"])
        self.assertNotIn("github_pat_secret", str(metadata))

        state.save_github_credential(
            {
                "mode": "app",
                "app_id": "12345",
                "installation_id": "67890",
                "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "not_checked"},
            }
        )
        with state.db.transaction() as cur:
            cur.execute("SELECT private_key_pem FROM github_credential")
            (raw_key,) = cur.fetchone()
        self.assertTrue(raw_key.startswith("enc:v1:"))
        # The minted working token lives only in the proxy row; its expiry
        # surfaces as app_token_expires_at in the credential metadata.
        state.save_proxy_github_token("ghs_minted", "2026-06-08T01:00:00Z")
        self.assertEqual(
            state.read_proxy_github_token_record(),
            {"token": "ghs_minted", "expires_at": "2026-06-08T01:00:00Z"},
        )
        metadata = state.read_github_credential_metadata()
        self.assertEqual(metadata["mode"], "app")
        self.assertEqual(metadata["app_token_expires_at"], "2026-06-08T01:00:00Z")
        self.assertNotIn("ghs_minted", str(metadata))
        self.assertNotIn("PRIVATE KEY", str(metadata))
        state.save_proxy_github_token(None)

        state.save_github_credential(None)
        self.assertEqual(state.read_github_credential_metadata(), {"configured": False})

    def test_proxy_github_token_round_trips_and_drives_injection_headers(self) -> None:
        from host.network_integrations.github.guard import rewrite_request_headers as github_credential_headers

        self.assertIsNone(state.read_proxy_github_token())
        # Without a working token: agent-supplied Authorization is stripped
        # on GitHub domains (a smuggled token cannot substitute another
        # identity), nothing is injected, and other domains pass untouched.
        smuggled = [("Authorization", "token smuggled"), ("Accept", "application/json")]
        self.assertEqual(github_credential_headers("api.github.com", smuggled), [("Accept", "application/json")])
        self.assertEqual(github_credential_headers("example.com", smuggled), smuggled)

        state.save_proxy_github_token("ghs_working")
        self.assertEqual(state.read_proxy_github_token(), "ghs_working")
        # The row itself holds secretbox ciphertext (encrypted at rest like
        # every other secret); only the read path yields the plaintext.
        with db.transaction() as cur:
            cur.execute("SELECT token FROM proxy_github_token")
            (stored,) = cur.fetchone()
        self.assertTrue(stored.startswith(secretbox.PREFIX))
        self.assertNotIn("ghs_working", stored)
        # A replaced row can never serve a cached stale token.
        state.save_proxy_github_token("ghs_replaced")
        self.assertEqual(state.read_proxy_github_token(), "ghs_replaced")
        state.save_proxy_github_token("ghs_working")
        self.assertEqual(state.read_proxy_github_token(), "ghs_working")
        # REST hosts take Bearer; git smart HTTP (and raw/codeload) take the
        # token as the Basic password with the x-access-token username.
        self.assertEqual(
            github_credential_headers("api.github.com", smuggled),
            [("Accept", "application/json"), ("Authorization", "Bearer ghs_working")],
        )
        import base64 as _b64

        basic = _b64.b64encode(b"x-access-token:ghs_working").decode()
        self.assertEqual(
            github_credential_headers("github.com", [("Authorization", "token smuggled")]),
            [("Authorization", f"Basic {basic}")],
        )
        # Signed-URL domains are strip-only: an Authorization header breaks
        # the presigned download, and the signed URL is the access control.
        self.assertEqual(
            github_credential_headers("objects.githubusercontent.com", [("Authorization", "token x")]), []
        )
        self.assertEqual(
            github_credential_headers("github-cloud.githubusercontent.com", [("Authorization", "token x")]), []
        )
        state.save_proxy_github_token(None)
        self.assertIsNone(state.read_proxy_github_token())
        with self.assertRaises(ValueError):
            state.save_proxy_github_token("")

    def test_github_repo_audits_upsert_and_prune(self) -> None:
        self.assertEqual(state.read_github_repo_audits(), {})
        state.save_github_repo_audit(
            "infiloop2",
            "trustyclaw",
            {"visibility": "public", "pages_public": False},
            None,
        )
        state.save_github_repo_audit("infiloop2", "infibot", {}, "audit fetch failed: 403")
        audits = state.read_github_repo_audits()
        self.assertEqual(
            audits[("infiloop2", "trustyclaw")]["facts"],
            {"visibility": "public", "pages_public": False},
        )
        self.assertNotIn("error", audits[("infiloop2", "trustyclaw")])
        self.assertEqual(audits[("infiloop2", "infibot")]["error"], "audit fetch failed: 403")
        # Re-auditing replaces the stored facts for that repo.
        state.save_github_repo_audit(
            "infiloop2",
            "trustyclaw",
            {"visibility": "private", "pages_public": None},
            None,
        )
        self.assertEqual(
            state.read_github_repo_audits()[("infiloop2", "trustyclaw")]["facts"],
            {"visibility": "private", "pages_public": None},
        )
        # An errored row is never fresh: the poller retries it on the next
        # pass instead of waiting out the TTL.
        from host.runtime.admin_api import github_repo_audit

        audits = state.read_github_repo_audits()
        self.assertTrue(github_repo_audit._stale(audits[("infiloop2", "infibot")]))
        self.assertFalse(github_repo_audit._stale(audits[("infiloop2", "trustyclaw")]))
        # Pruning drops repositories no longer in the policy.
        state.prune_github_repo_audits({("infiloop2", "trustyclaw")})
        self.assertEqual(list(state.read_github_repo_audits()), [("infiloop2", "trustyclaw")])

    def test_admin_provider_accounts_live_in_the_database(self) -> None:
        save_openai_account({"account_id": "acct_rich", "planType": "pro"})
        save_claude_account({"access_token_sha256": "e" * 64})

        self.assertEqual(read_openai_account(), {"account_id": "acct_rich", "planType": "pro"})
        self.assertEqual(read_claude_account(), {"access_token_sha256": "e" * 64})
        # Clearing leaves an empty record.
        save_openai_account(None)
        save_claude_account(None)
        self.assertEqual(read_openai_account(), {})
        self.assertEqual(read_claude_account(), {})

    def test_clearing_openai_account_leaves_an_empty_record(self) -> None:
        save_openai_account({"account_id": "acct", "planType": "pro"})
        save_openai_account(None)

        self.assertEqual(read_openai_account(), {})

    def test_config_replaces_wholesale(self) -> None:
        hash_1 = "1" * 64
        save_config({"agent_name": "one", "admin_password_sha256": hash_1})
        self.assertEqual(load_config()["agent_name"], "one")
        self.assertEqual(load_config()["admin_password_sha256"], hash_1)
        save_config({"agent_name": "two"})
        self.assertEqual(load_config(), {"agent_name": "two"})

    def test_host_runtime_has_no_third_party_imports(self) -> None:
        # The runtime is standard library only — the admin-state database is
        # spoken to by the in-repo protocol client, not a driver. This walks
        # every host/ module and rejects any import outside the stdlib, the
        # host package itself, and the in-repo tools package (which is
        # likewise pinned to the stdlib by test_tools), so a dependency
        # cannot sneak back in.
        import ast
        import sys

        repo_root = Path(__file__).resolve().parents[1]
        allowed_roots = set(sys.stdlib_module_names) | {"host", "tools"}
        offenders: list[str] = []
        for path in sorted((repo_root / "host").rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots = [node.module.split(".")[0]]
                else:
                    continue
                for root in roots:
                    if root not in allowed_roots:
                        offenders.append(f"{path.relative_to(repo_root)}: {root}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
