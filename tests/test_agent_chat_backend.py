from __future__ import annotations

from http import HTTPStatus
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
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
