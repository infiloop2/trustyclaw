"""Tests for the in-repo PostgreSQL wire-protocol client, against the scratch
cluster from pg_harness. These pin the narrow contract the runtime relies on:
parameter/result round-trips in text format, multi-statement simple queries,
error surfacing with SQLSTATE while the connection stays usable, and the
placeholder discipline."""

from __future__ import annotations

import os
import threading
import unittest
from unittest.mock import patch

import pg_harness

from host.runtime import db, pgclient


def connect() -> pgclient.Connection:
    return pgclient.connect(
        socket_dir=os.environ["TRUSTYCLAW_DB_SOCKET_DIR"],
        dbname=os.environ["TRUSTYCLAW_DB_NAME"],
        user=os.environ["TRUSTYCLAW_DB_USER"],
    )


class PgClientTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        self.conn = connect()
        self.addCleanup(self.conn.close)

    def test_parameter_and_result_types_round_trip(self) -> None:
        self.conn.execute(
            "CREATE TEMP TABLE round_trip (t TEXT, i BIGINT, f DOUBLE PRECISION,"
            " b BOOLEAN, j JSONB, missing TEXT)"
        )
        self.conn.execute(
            "INSERT INTO round_trip VALUES (%s, %s, %s, %s, %s, %s)",
            ["héllo", -42, 1234.5678901, True, pgclient.Jsonb({"k": [1, None, "x"]}), None],
        )
        result = self.conn.execute("SELECT t, i, f, b, j, missing FROM round_trip")
        self.assertEqual(result.rows, [("héllo", -42, 1234.5678901, True, {"k": [1, None, "x"]}, None)])
        self.assertEqual([name for name, _ in result.columns], ["t", "i", "f", "b", "j", "missing"])

    def test_simple_query_runs_multiple_statements(self) -> None:
        # Migration files are multi-statement scripts executed in one call;
        # the last result set is the one returned.
        result = self.conn.execute(
            "CREATE TEMP TABLE multi (n INT); INSERT INTO multi VALUES (1), (2);"
            " SELECT n FROM multi ORDER BY n"
        )
        self.assertEqual(result.rows, [(1,), (2,)])

    def test_errors_carry_sqlstate_and_leave_the_connection_usable(self) -> None:
        with self.assertRaises(pgclient.Error) as raised:
            self.conn.execute("SELECT no_such_column")
        self.assertEqual(raised.exception.sqlstate, "42703")
        self.assertEqual(self.conn.execute("SELECT 7").rows, [(7,)])

    def test_transaction_rollback_after_error(self) -> None:
        self.conn.execute("CREATE TABLE txn_check (n INT)")
        self.conn.execute("BEGIN")
        self.conn.execute("INSERT INTO txn_check VALUES (%s)", [1])
        with self.assertRaises(pgclient.Error):
            self.conn.execute("SELECT broken")
        self.conn.execute("ROLLBACK")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM txn_check").rows, [(0,)])

    def test_placeholder_count_mismatch_is_rejected(self) -> None:
        with self.assertRaises(pgclient.Error):
            self.conn.execute("SELECT %s, %s", ["only-one"])
        with self.assertRaises(pgclient.Error):
            self.conn.execute("SELECT 1", ["unexpected"])

    def test_unsupported_parameter_types_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            self.conn.execute("SELECT %s", [object()])

    def test_null_and_empty_string_stay_distinct(self) -> None:
        result = self.conn.execute("SELECT %s::text, %s::text", ["", None])
        self.assertEqual(result.rows, [("", None)])

    def test_large_values_round_trip(self) -> None:
        # Task messages can be 50k characters and API responses carry
        # whole API payloads; framing must handle values beyond one recv().
        big = "x" * 300_000
        self.conn.execute("CREATE TEMP TABLE big_values (t TEXT)")
        self.conn.execute("INSERT INTO big_values VALUES (%s)", [big])
        self.assertEqual(self.conn.execute("SELECT t FROM big_values").rows, [(big,)])

    def test_session_budget_queues_bursts_instead_of_failing(self) -> None:
        # More concurrent transactions than the active-session budget must
        # queue briefly (sessions are millisecond-lived), never error.
        db.close_pool()
        with patch.object(db, "_ACTIVE", threading.BoundedSemaphore(2)):
            errors: list[BaseException] = []
            barrier = threading.Barrier(6)

            def work() -> None:
                try:
                    barrier.wait(timeout=10)
                    for _ in range(5):
                        with db.transaction() as cur:
                            cur.execute("SELECT 1")
                except BaseException as exc:  # noqa: BLE001 - surfaced below
                    errors.append(exc)

            workers = [threading.Thread(target=work) for _ in range(6)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=30)
            self.assertEqual(errors, [])
        db.close_pool()

    def test_session_budget_exhaustion_fails_with_a_clear_error(self) -> None:
        # A transaction held open past the checkout timeout (a bug by
        # contract) surfaces as a budget error, not a hang.
        db.close_pool()
        with patch.object(db, "_ACTIVE", threading.BoundedSemaphore(1)), patch.object(
            db, "CHECKOUT_TIMEOUT_SECONDS", 0.2
        ):
            release = threading.Event()
            started = threading.Event()

            def holder() -> None:
                with db.transaction() as cur:
                    cur.execute("SELECT 1")
                    started.set()
                    release.wait(timeout=10)

            thread = threading.Thread(target=holder)
            thread.start()
            try:
                self.assertTrue(started.wait(timeout=10))
                with self.assertRaisesRegex(pgclient.Error, "session budget exhausted"):
                    with db.transaction():
                        pass
            finally:
                release.set()
                thread.join(timeout=10)
        db.close_pool()

    def test_closed_connection_refuses_queries(self) -> None:
        conn = connect()
        conn.close()
        self.assertTrue(conn.closed)
        with self.assertRaises(pgclient.Error):
            conn.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
