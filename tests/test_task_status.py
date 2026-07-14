from __future__ import annotations

import unittest

from host.runtime import task_status as ts


class TaskStatusTests(unittest.TestCase):
    def test_legal_transitions_apply_and_stamp(self) -> None:
        task = {"status": ts.QUEUED, "updated_at": "old"}
        ts.set_status(task, ts.RUNNING, now="t1")
        self.assertEqual(task["status"], ts.RUNNING)
        self.assertEqual(task["updated_at"], "t1")
        ts.set_status(task, ts.COMPLETED, now="t2")
        self.assertEqual(task["status"], ts.COMPLETED)

    def test_illegal_transition_raises(self) -> None:
        # Terminal states are final: a completed task can never move again.
        done = {"status": ts.COMPLETED, "updated_at": "t"}
        with self.assertRaises(ValueError):
            ts.set_status(done, ts.RUNNING, now="t3")
        # queued cannot jump straight to completed.
        queued = {"status": ts.QUEUED, "updated_at": "t"}
        with self.assertRaises(ValueError):
            ts.set_status(queued, ts.COMPLETED, now="t3")



if __name__ == "__main__":
    unittest.main()
