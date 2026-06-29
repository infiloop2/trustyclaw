"""Tests for the state.json transaction primitives (state_update/read_state).

These pin the locking contract the rest of the runtime is built on: the lock
spans the whole load-mutate-save cycle (no lost updates), an exception skips
the save, reads are consistent snapshots, and event sequence numbers are never
reused even when a transaction aborts after allocating one.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from host.runtime.state import (
    append_agent_event,
    load_state,
    network_policy_files,
    network_proxy_cert_files,
    read_claude_account,
    read_openai_account_id,
    read_proxy_claude_account,
    read_proxy_openai_account_id,
    read_agent_events,
    read_state,
    save_claude_account,
    save_openai_account_id,
    save_proxy_claude_account,
    save_proxy_openai_account_id,
    save_state,
    state_update,
)


class StateTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_STATE_DIR": self.temp_dir.name})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.state_file = Path(self.temp_dir.name) / "state.json"

    def test_state_update_persists_on_normal_exit(self) -> None:
        with state_update() as state:
            state["marker"] = "saved"
        self.assertEqual(read_state()["marker"], "saved")

    def test_state_update_skips_save_on_exception(self) -> None:
        with state_update() as state:
            state["committed"] = True
        with self.assertRaises(RuntimeError):
            with state_update() as state:
                state["committed"] = False
                raise RuntimeError("abort the transaction")
        self.assertTrue(read_state()["committed"])

    def test_state_update_persists_on_early_return(self) -> None:
        # A `return` from inside the block is a normal exit: the save runs.
        def mutate_and_return() -> str:
            with state_update() as state:
                state["early"] = "returned"
                return "done"

        self.assertEqual(mutate_and_return(), "done")
        self.assertEqual(read_state()["early"], "returned")

    def test_no_op_transaction_skips_the_rewrite(self) -> None:
        # Polling paths (the worker claim loop) enter a transaction every few
        # seconds and usually change nothing; they must not rewrite the file.
        with state_update() as state:
            state["value"] = 1
        before = self.state_file.stat()
        with state_update() as state:
            self.assertEqual(state["value"], 1)
        with state_update() as state:
            state["value"] = 1  # rewriting the same value is still a no-op
        after = self.state_file.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        with state_update() as state:
            state["value"] = 2  # a real change still persists
        self.assertEqual(read_state()["value"], 2)

    def test_read_state_returns_a_detached_snapshot(self) -> None:
        with state_update() as state:
            state["value"] = 1
        snapshot = read_state()
        snapshot["value"] = 999
        snapshot["tasks"].append({"task_id": "task_evil"})
        self.assertEqual(read_state()["value"], 1)
        self.assertEqual(read_state()["tasks"], [])

    def test_read_state_inside_state_update_does_not_deadlock(self) -> None:
        # Helpers called from inside a transaction may take read-only snapshots
        # (the lock is reentrant); the snapshot sees the last *saved* state,
        # not the in-progress mutation.
        with state_update() as state:
            state["phase"] = "mutating"
            self.assertNotIn("phase", read_state())
        self.assertEqual(read_state()["phase"], "mutating")

    def test_concurrent_updates_never_lose_increments(self) -> None:
        # The reason the lock must span load->mutate->save: interleaved cycles
        # would drop increments. 8 threads x 25 increments must land exactly.
        threads, per_thread = 8, 25
        with state_update() as state:
            state["counter"] = 0
        barrier = threading.Barrier(threads)
        errors: list[BaseException] = []

        def work() -> None:
            try:
                barrier.wait(timeout=10)
                for _ in range(per_thread):
                    with state_update() as state:
                        state["counter"] = int(state["counter"]) + 1
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        workers = [threading.Thread(target=work) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(read_state()["counter"], threads * per_thread)

    def test_proxy_state_can_be_split_from_admin_state(self) -> None:
        with tempfile.TemporaryDirectory() as proxy_tmp, patch.dict(
            "os.environ",
            {"TRUSTYCLAW_STATE_DIR": self.temp_dir.name, "TRUSTYCLAW_PROXY_STATE_DIR": proxy_tmp},
        ):
            policy_files = network_policy_files()
            cert_files = network_proxy_cert_files("example.com")
            self.assertEqual(policy_files.controls, Path(proxy_tmp) / "network_controls.json")
            self.assertEqual(policy_files.lock, Path(proxy_tmp) / ".network_policy.lock")
            self.assertEqual(cert_files.ca_cert, Path(proxy_tmp) / "network_proxy_ca.crt")
            self.assertEqual(cert_files.ca_key, Path(proxy_tmp) / "network_proxy_ca.key")
            self.assertEqual(cert_files.directory, Path(proxy_tmp) / "generated-certs")

    def test_provider_account_files_are_private_in_admin_and_proxy_state(self) -> None:
        save_openai_account_id("acct_smoke")
        save_claude_account({"access_token_sha256": "hash"})

        openai_path = Path(self.temp_dir.name) / "openai_account.json"
        claude_path = Path(self.temp_dir.name) / "claude_account.json"
        self.assertEqual(oct(openai_path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(claude_path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(read_openai_account_id(), "acct_smoke")
        self.assertEqual(read_claude_account(), {"access_token_sha256": "hash"})

        with tempfile.TemporaryDirectory() as proxy_tmp, patch.dict(
            "os.environ",
            {"TRUSTYCLAW_STATE_DIR": self.temp_dir.name, "TRUSTYCLAW_PROXY_STATE_DIR": proxy_tmp},
        ):
            save_proxy_openai_account_id("acct_proxy")
            save_proxy_claude_account({"access_token_sha256": "proxy_hash"})
            proxy_openai_path = Path(proxy_tmp) / "openai_account.json"
            proxy_claude_path = Path(proxy_tmp) / "claude_account.json"
            self.assertEqual(oct(proxy_openai_path.stat().st_mode & 0o777), "0o600")
            self.assertEqual(oct(proxy_claude_path.stat().st_mode & 0o777), "0o600")
            self.assertEqual(read_proxy_openai_account_id(), "acct_proxy")
            self.assertEqual(read_proxy_claude_account(), {"access_token_sha256": "proxy_hash"})

    def test_concurrent_readers_see_consistent_snapshots_during_updates(self) -> None:
        # Writers keep two fields in sync inside one transaction; a reader must
        # never observe them apart (a torn read would mean load escaped the
        # transaction lock or saw a partial file).
        with state_update() as state:
            state["a"] = state["b"] = 0
        stop = threading.Event()
        errors: list[str] = []

        def reader() -> None:
            while not stop.is_set():
                snapshot = read_state()
                if snapshot["a"] != snapshot["b"]:
                    errors.append(f"torn read: a={snapshot['a']} b={snapshot['b']}")
                    return

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        try:
            for value in range(1, 100):
                with state_update() as state:
                    state["a"] = value
                    state["b"] = value
        finally:
            stop.set()
            reader_thread.join(timeout=30)
        self.assertEqual(errors, [])

    def test_aborted_transaction_never_reuses_an_event_seq(self) -> None:
        # append_agent_event persists the advanced counter immediately (inside
        # the transaction) so that even an abort after the append cannot lead
        # to a reused seq — duplicate seqs would break since-based pagination.
        with self.assertRaises(RuntimeError):
            with state_update() as state:
                append_agent_event(state, "test.aborted", None, {})
                raise RuntimeError("abort after allocating a seq")
        with state_update() as state:
            append_agent_event(state, "test.committed", None, {})
        seqs = [event["seq"] for event in read_agent_events()]
        self.assertEqual(len(seqs), len(set(seqs)), f"duplicate event seqs: {seqs}")
        self.assertEqual(sorted(seqs), seqs)

    def test_bare_load_and_save_remain_for_single_threaded_callers(self) -> None:
        # Tests and the bootstrap use load_state/save_state directly; they must
        # observe the same file the transactions use.
        state = load_state()
        state["direct"] = True
        save_state(state)
        self.assertTrue(read_state()["direct"])

    def test_load_state_repairs_malformed_runtime_status_container(self) -> None:
        self.state_file.write_text('{"agent_runtime_statuses": "bad"}\n')

        state = load_state()

        self.assertEqual(
            state["agent_runtime_statuses"],
            {
                "codex": {"status": "loading"},
                "claude_code": {"status": "loading"},
            },
        )


if __name__ == "__main__":
    unittest.main()
