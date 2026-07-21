from __future__ import annotations

from http import HTTPStatus
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
import unittest.mock
from unittest.mock import call, patch
import urllib.request

from host.apps.agent_chat import backend


class AgentChatBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), backend.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_session_options_endpoint_exposes_the_creation_matrix(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}/session-options",
            headers={"X-TrustyClaw-App-Proxy": "agent_chat"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())

        self.assertEqual(
            body,
            {
                "session_options": {
                    "codex": {
                        "gpt-5.6-terra": ["high", "max", "ultra"],
                        "gpt-5.6-sol": ["high", "max", "ultra"],
                        "gpt-5.6-luna": ["high", "max"],
                    },
                    "claude_code": {
                        "opus": ["high", "max", "ultracode"],
                        "fable": ["high", "max", "ultracode"],
                        "sonnet": ["high", "max", "ultracode"],
                    },
                    "pi": {
                        "deepseek.v3.2": ["medium", "high", "max"],
                        "qwen.qwen3-coder-next": ["medium", "high", "max"],
                        "moonshotai.kimi-k2.5": ["medium", "high", "max"],
                    },
                    "hermes": {
                        "deepseek.v3.2": ["high"],
                        "qwen.qwen3-coder-next": ["high"],
                        "moonshotai.kimi-k2.5": ["high"],
                    },
                }
            },
        )

    def test_follow_up_uses_host_owned_session_options(self) -> None:
        request = {"input_message": "continue", "thread_id": "existing"}
        response = {
            "task_id": "task_2",
            "thread_id": "existing",
            "agent_runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "max",
        }
        with (
            patch("host.apps.agent_chat.backend.call_admin_api", return_value=response) as call,
            patch("host.apps.agent_chat.backend._record_app_task") as record,
        ):
            self.assertEqual(backend.create_app_task(request), response)

        call.assert_called_once_with("POST", "/v1/tasks", request)
        record.assert_called_once_with("existing", "task_2")

    def test_create_without_thread_id_generates_the_next_successive_name(self) -> None:
        request = {
            "input_message": "start",
            "agent_runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "max",
        }
        response = {
            "task_id": "task_9",
            "thread_id": "thread-4",
            "agent_runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "max",
        }
        with (
            patch("host.apps.agent_chat.backend._reserve_generated_thread_id", return_value="thread-4") as reserve,
            patch("host.apps.agent_chat.backend.call_admin_api", return_value=response) as admin_call,
            patch("host.apps.agent_chat.backend._record_app_task") as record,
        ):
            self.assertEqual(backend.create_app_task(request), response)

        reserve.assert_called_once_with()
        admin_call.assert_called_once_with("POST", "/v1/tasks", {**request, "thread_id": "thread-4"})
        record.assert_called_once_with("thread-4", "task_9")

    def test_generated_thread_names_count_over_all_recorded_threads(self) -> None:
        cursor = unittest.mock.MagicMock()
        transaction = unittest.mock.MagicMock()
        transaction.__enter__.return_value = cursor
        for rows, expected in (
            ([], "thread-1"),
            ([("thread-2",), ("thread-11",), ("archived-name",), ("thread-03",)], "thread-12"),
        ):
            cursor.fetchall.return_value = rows
            cursor.fetchone.return_value = (expected,)
            with patch("host.apps.agent_chat.backend.db.transaction", return_value=transaction):
                self.assertEqual(backend._reserve_generated_thread_id(), expected)
            insert_args, _kwargs = cursor.execute.call_args
            self.assertIn("ON CONFLICT (thread_id) DO NOTHING", insert_args[0])
            self.assertEqual(insert_args[1], (expected,))
            cursor.reset_mock()

    def test_generated_thread_name_retries_past_a_concurrent_reservation(self) -> None:
        """A lost insert race means another request took the name: the next
        attempt sees the committed row and reserves the following number."""
        cursor = unittest.mock.MagicMock()
        transaction = unittest.mock.MagicMock()
        transaction.__enter__.return_value = cursor
        cursor.fetchall.side_effect = ([("thread-1",)], [("thread-1",), ("thread-2",)])
        cursor.fetchone.side_effect = (None, ("thread-3",))
        with patch("host.apps.agent_chat.backend.db.transaction", return_value=transaction):
            self.assertEqual(backend._reserve_generated_thread_id(), "thread-3")

    def test_list_app_threads_makes_one_bulk_call_and_counts_recorded_tasks(self) -> None:
        cursor = unittest.mock.MagicMock()
        transaction = unittest.mock.MagicMock()
        transaction.__enter__.return_value = cursor
        # thread-1 has two recorded tasks; thread-2 has one. thread-9 is
        # archived (absent from the join); thread-orphan has a reservation but
        # no recorded task (also absent). Both must stay out of the index even
        # though the host still returns summaries for them.
        cursor.fetchall.return_value = [
            ("thread-1", "task_9"),
            ("thread-1", "task_8"),
            ("thread-2", "task_3"),
        ]
        summaries = {
            "threads": [
                {
                    "thread_id": "thread-1",
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                    "last_used_at": "2026-07-17T10:00:00Z",
                    # Host count is inflated by an orphaned cancelled task; the
                    # app reports its own recorded count instead.
                    "task_count": 3,
                    "active_tasks": [
                        {"task_id": "task_9", "status": "running"},
                        {"task_id": "task_orphan", "status": "running"},
                    ],
                },
                {
                    "thread_id": "thread-orphan",
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                    "last_used_at": "2026-07-17T12:00:00Z",
                    "task_count": 1,
                    "active_tasks": [],
                },
                {
                    "thread_id": "thread-2",
                    "agent_runtime": "claude_code",
                    "model": "opus",
                    "effort": "max",
                    "last_used_at": "2026-07-17T09:00:00Z",
                    "task_count": 1,
                    "active_tasks": [],
                },
            ]
        }
        with (
            patch("host.apps.agent_chat.backend.db.transaction", return_value=transaction),
            patch("host.apps.agent_chat.backend.call_admin_api", return_value=summaries) as admin_call,
        ):
            response = backend.list_app_threads()

        admin_call.assert_called_once_with("GET", "/v1/threads")
        # Only threads with recorded tasks, newest-first by last_used_at.
        self.assertEqual([thread["thread_id"] for thread in response["threads"]], ["thread-1", "thread-2"])
        first = response["threads"][0]
        # Count is the recorded count (2), not the host's inflated 3, and the
        # orphaned active task is dropped.
        self.assertEqual(first["task_count"], 2)
        self.assertEqual(first["active_tasks"], [{"task_id": "task_9", "status": "running"}])

    def test_list_app_thread_events_proxies_with_since(self) -> None:
        events = {"events": [{"seq": 5, "task_id": "task_1", "event_type": "task.message"}]}
        with (
            patch("host.apps.agent_chat.backend._require_app_thread"),
            patch("host.apps.agent_chat.backend.call_admin_api", return_value=events) as admin_call,
        ):
            response = backend.list_app_thread_events("thread-1", {"since": ["2"]})

        admin_call.assert_called_once_with("GET", "/v1/threads/thread-1/events?since=2")
        self.assertEqual(response, events)

    def test_list_app_thread_events_rejects_non_numeric_since(self) -> None:
        with (
            patch("host.apps.agent_chat.backend._require_app_thread"),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.list_app_thread_events("thread-1", {"since": ["nope"]})

        self.assertEqual(error.exception.status, 400)

    def test_unknown_app_thread_error_comes_from_the_host(self) -> None:
        with (
            patch(
                "host.apps.agent_chat.backend.call_admin_api",
                side_effect=backend.AppError(HTTPStatus.BAD_REQUEST, "session configuration required"),
            ),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.create_app_task({"input_message": "start", "thread_id": "unknown"})

        self.assertEqual(error.exception.status, 400)
        self.assertEqual(error.exception.message, "session configuration required")

    def test_app_session_options_must_be_provided_together(self) -> None:
        with (
            self.assertRaises(backend.AppError) as error,
        ):
            backend.create_app_task(
                {
                    "input_message": "start",
                    "thread_id": "partial",
                    "agent_runtime": "codex",
                }
            )

        self.assertEqual(error.exception.status, 400)
        self.assertIn("must be provided together", error.exception.message)

    def test_mismatched_host_response_cancels_the_orphaned_task(self) -> None:
        request = {
            "input_message": "start",
            "thread_id": "new",
            "agent_runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "max",
        }
        response = {
            "task_id": "task_3",
            "thread_id": "new",
            "agent_runtime": "codex",
            "model": "gpt-5.6-terra",
            "effort": "max",
        }
        with (
            patch(
                "host.apps.agent_chat.backend.call_admin_api",
                side_effect=(response, {"status": "accepted"}),
            ) as admin_call,
            patch("host.apps.agent_chat.backend._record_app_task") as record,
            self.assertRaises(backend.AppError) as error,
        ):
            backend.create_app_task(request)

        self.assertEqual(error.exception.status, 502)
        self.assertEqual(
            admin_call.call_args_list,
            [
                call("POST", "/v1/tasks", request),
                call("POST", "/v1/tasks/task_3/cancel", {}),
            ],
        )
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
