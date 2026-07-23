"""Personal Web App Builder contract and backend validation tests."""

from __future__ import annotations

from email.message import Message
from http import HTTPStatus
import importlib.util
import json
from pathlib import Path
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch

import pg_harness

from host.apps.personal_web_app_builder import backend
from host.runtime.core import app_platform
from host.runtime.core import db
from host.runtime.deploy import app_migrate, migrate


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "host" / "apps" / "personal_web_app_builder"


class PersonalWebAppBuilderContractTests(unittest.TestCase):
    def test_manifest_and_agent_contract_define_the_independent_builder(self) -> None:
        app = app_platform.app_by_id(backend.APP_ID)
        assert app is not None
        self.assertEqual(app.title, "Personal Web App Builder")
        self.assertEqual(app.allocation.port_offset, 6)
        self.assertEqual(app.db_schema, "app_personal_web_app_builder")
        self.assertEqual(app.release_stage, "stable")
        self.assertTrue(app.agent_api)
        self.assertTrue(app.capability_worker)
        instructions = (APP_DIR / "agent.md").read_text()
        self.assertIn("app.askAgent(message)", instructions)
        self.assertIn("app.set(path, value)", instructions)
        self.assertIn('`{"action":"set","expected_revision":3', instructions)

    def test_generated_ui_source_has_no_page_navigation_or_direct_agent_capability(self) -> None:
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        index = (APP_DIR / "ui" / "index.html").read_text()
        instructions = (APP_DIR / "agent.md").read_text()
        self.assertIn("new Worker(url)", source)
        self.assertIn("capabilityWorkerBootstrap", source)
        self.assertIn('"fetch", "XMLHttpRequest", "WebSocket"', source)
        self.assertIn('"A", "AUDIO", "BASE"', source)
        self.assertIn('message.type === "agent-request"', source)
        self.assertIn("void sendMessage(message.message.trim())", source)
        self.assertNotIn("requestAgentConfirmation", source)
        self.assertNotIn("agent-confirm", index)
        self.assertIn("MAX_WORKER_MUTATIONS_PER_TURN = 16", source)
        self.assertIn("render content exceeds its encoded size limit", source)
        self.assertIn("if (!fromGeneratedApp)", source)
        self.assertIn("MAX_CSS_CONDITION_BYTES = 512", source)
        self.assertNotIn('kind === "CSSSupportsRule"', source)
        self.assertIn("Function.prototype.constructor", source)
        self.assertIn("wasm-unsafe-eval", source)
        self.assertIn("if (workerRun !== run)", source)
        self.assertIn('event.type === "click" && changeControl', source)
        self.assertIn("contain: layout paint style", (APP_DIR / "ui" / "personal_web_app_builder.css").read_text())
        self.assertIn('rule.selectorText.includes("\\\\")', source)
        self.assertNotIn("window.open", source)
        self.assertNotIn("location.href", source)
        self.assertNotIn("location.assign", source)
        self.assertNotIn("new Function", source)
        self.assertIn("askAgent(message)", source)
        self.assertIn("onLoad(handler)", source)
        self.assertIn("load: !run.event", source)
        self.assertIn('"ABBR", "ADDRESS", "ARTICLE"', source)
        self.assertIn("safeCustomProperty", source)
        self.assertIn("background-image", source)
        self.assertIn("forbiddenCssValue", source)
        self.assertIn("if (!snapshot.session)", source)
        self.assertNotIn("if (!snapshot.tasks.length)", source)
        self.assertIn('fromGeneratedApp ? "/runtime/agent-requests" : "/messages"', source)
        self.assertIn('/conversation/events?since=${conversationEventsSeq}', source)
        self.assertIn('event.event_type !== "task.message"', source)
        self.assertIn("task.output_message !== lastAgentText", source)
        self.assertIn("Always register `app.onLoad`", instructions)
        self.assertIn("Use the full safe authoring palette", instructions)
        self.assertIn("The hard exclusions are security boundaries", instructions)
        self.assertIn("same authority as the human typing", instructions)
        self.assertIn("`Requested by user:` means", instructions)
        self.assertIn("`Requested by app:` means", instructions)
        self.assertIn('id="agent-settings"', index)
        self.assertIn('id="agent-settings-help"', index)
        self.assertIn('id="runtime-fixed"', index)
        self.assertIn('id="model-fixed"', index)
        self.assertIn('id="effort-fixed"', index)
        self.assertNotIn("Choose before first message", index)
        self.assertNotIn("Fixed for this session", index)
        self.assertIn('aria-label="Builder agent"', index)
        self.assertIn('aria-label="Builder effort level"', index)
        self.assertLess(index.index('id="agent-settings"'), index.index('id="chat-drawer"'))
        self.assertIn('id="first-run-guidance"', index)
        self.assertIn("Agent, Model, and Level are fixed when you send the first message", index)
        self.assertIn("Build it through Agent chat", index)
        self.assertIn("Use the app directly", index)
        self.assertIn("Its controls can update saved data", index)
        self.assertIn("const firstRun = !snapshot.session", source)
        self.assertIn('$("first-run-how").hidden = !firstRun', source)
        self.assertIn('$("first-run-guidance").hidden = !firstRun', source)
        self.assertNotIn('id="session-options"', index)
        self.assertNotRegex(index.lower(), r"<a(?:\s|>)")
        self.assertNotIn('value="pi"', index)

    def test_migration_uses_typed_columns_for_the_owned_bundle(self) -> None:
        migration = (APP_DIR / "migrations" / "0001_app_state.sql").read_text()
        for column in ("revision BIGINT", "html TEXT", "css TEXT", "javascript TEXT", "data_json TEXT"):
            self.assertIn(column, migration)
        self.assertNotIn("thread_tasks", migration)
        self.assertNotIn("builder_session", migration)


class AgentActionValidationTests(unittest.TestCase):
    def test_replace_app_validates_every_field_and_updates_once(self) -> None:
        action = {
            "action": "replace_app",
            "expected_revision": 4,
            "html": "<main>Hello</main>",
            "css": "main { display: grid; }",
            "javascript": "app.on('save', () => app.notify('saved'));",
            "data": {"items": [{"name": "first"}]},
        }
        changed = {**action, "revision": 5, "updated_at": "now"}
        with patch.object(backend, "_update_state", return_value=changed) as update:
            self.assertEqual(backend.apply_agent_action(action), {"app": changed})

        values = update.call_args.args[1]
        self.assertEqual(update.call_args.args[0], 4)
        self.assertEqual(values["html"], action["html"])
        self.assertEqual(json.loads(values["data_json"]), action["data"])

    def test_agent_action_rejects_extra_fields_and_dynamic_imports(self) -> None:
        base = {
            "action": "replace_ui",
            "expected_revision": 0,
            "html": "",
            "css": "",
            "javascript": "",
        }
        with self.assertRaises(backend.AppError) as extra:
            backend.apply_agent_action({**base, "url": "https://example.com"})
        self.assertEqual(extra.exception.status, HTTPStatus.BAD_REQUEST)

        for javascript in (
            "import('https://example.com/app.js')",
            "import /* hidden */ ('https://example.com/app.js')",
        ):
            with self.subTest(javascript=javascript), self.assertRaises(backend.AppError) as imported:
                backend.apply_agent_action({**base, "javascript": javascript})
            self.assertEqual(imported.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_agent_path_actions_share_the_runtime_mutation_contract(self) -> None:
        action = {
            "action": "set",
            "expected_revision": 4,
            "path": ["projects", "alpha", "status"],
            "value": "done",
        }
        changed = {"revision": 5, "data": {"projects": {"alpha": {"status": "done"}}}}
        with patch.object(backend, "apply_runtime_action", return_value={"app": changed}) as apply:
            self.assertEqual(backend.apply_agent_action(action), {"app": changed})
        apply.assert_called_once_with(action)

    def test_bundle_and_data_caps_are_measured_in_encoded_bytes(self) -> None:
        with self.assertRaises(backend.AppError) as html_error:
            backend._bounded_string("é" * (backend.MAX_HTML_BYTES // 2 + 1), "html", backend.MAX_HTML_BYTES)
        self.assertEqual(html_error.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        too_large = {"value": "é" * (backend.MAX_DATA_BYTES // 2)}
        with self.assertRaises(backend.AppError) as data_error:
            backend._validated_data(too_large)
        self.assertEqual(data_error.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        with self.assertRaises(backend.AppError) as chat_error:
            backend._bounded_required_text(
                "é" * (backend.MAX_CHAT_MESSAGE_BYTES // 2 + 1),
                "content",
                backend.MAX_CHAT_MESSAGE_BYTES,
            )
        self.assertEqual(chat_error.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

    def test_complete_serialized_state_must_fit_the_host_proxy(self) -> None:
        state = {
            "revision": 1,
            "html": "\x01" * backend.MAX_HTML_BYTES,
            "css": "",
            "javascript": "\x01" * backend.MAX_JAVASCRIPT_BYTES,
            "data": {},
            "updated_at": "now",
        }
        with self.assertRaises(backend.AppError) as error:
            backend._require_state_response_fits(state)
        self.assertEqual(error.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)


class RuntimeDataActionTests(unittest.TestCase):
    def test_set_delete_and_append_follow_the_typed_path(self) -> None:
        data = {"items": [{"name": "one", "done": False}], "tags": []}
        backend._mutate_data(data, "set", ["items", 0, "done"], True)
        backend._mutate_data(data, "append", ["tags"], "new")
        backend._mutate_data(data, "delete", ["items", 0, "name"], None)
        self.assertEqual(data, {"items": [{"done": True}], "tags": ["new"]})

    def test_runtime_path_rejects_shape_mismatches_and_unbounded_keys(self) -> None:
        with self.assertRaises(backend.AppError) as missing:
            backend._mutate_data({"items": []}, "set", ["items", 0], "bad")
        self.assertEqual(missing.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

        with self.assertRaises(backend.AppError):
            backend._validated_path(["x" * (backend.MAX_PATH_KEY_BYTES + 1)])
        with self.assertRaises(backend.AppError):
            backend._validated_path(["a"] * (backend.MAX_PATH_DEPTH + 1))

    def test_runtime_action_revision_conflict_fails_without_writing(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (7, '{"count":1}')
        transaction = MagicMock()
        transaction.__enter__.return_value = cursor
        with (
            patch.object(backend.db, "transaction", return_value=transaction),
            self.assertRaises(backend.AppError) as conflict,
        ):
            backend.apply_runtime_action({
                "action": "set", "expected_revision": 6, "path": ["count"], "value": 2,
            })

        self.assertEqual(conflict.exception.status, HTTPStatus.CONFLICT)
        self.assertEqual(cursor.execute.call_count, 2)


class ConversationTests(unittest.TestCase):
    SESSION = {
        "agent_runtime": "codex",
        "model": "gpt-5.6-terra",
        "effort": "high",
    }

    def test_conversation_bounds_history_before_it_crosses_the_app_backend_proxy(self) -> None:
        host_task = {
            "task_id": "task_1",
            "input_message": "Build it",
            "status": "completed",
            **self.SESSION,
        }
        with patch.object(
            backend, "call_admin_api", return_value={"tasks": [host_task]}
        ) as host:
            self.assertEqual(
                backend.browser_conversation(),
                {
                    "tasks": [host_task],
                    "session": self.SESSION,
                },
            )

        host.assert_called_once_with(
            "GET",
            "/v1/threads/builder/tasks?limit=20&message_bytes=12288",
        )

    def test_empty_host_thread_is_the_first_run_marker(self) -> None:
        with patch.object(
            backend, "call_admin_api", return_value={"tasks": []}
        ) as host:
            self.assertEqual(
                backend.browser_conversation(),
                {"tasks": [], "session": None},
            )
        host.assert_called_once_with(
            "GET",
            "/v1/threads/builder/tasks?limit=20&message_bytes=12288",
        )

    def test_conversation_events_proxy_the_builder_thread_from_since(self) -> None:
        events = {
            "events": [
                {
                    "seq": 5,
                    "task_id": "task_1",
                    "event_type": "task.message",
                    "payload": {"message": "Working on it.", "source": "agent"},
                }
            ]
        }
        with patch.object(backend, "call_admin_api", return_value=events) as host:
            self.assertEqual(
                backend.browser_conversation_events({"since": ["2"]}),
                events,
            )

        host.assert_called_once_with(
            "GET",
            "/v1/threads/builder/events?since=2&limit=5&message_bytes=12288",
        )

    def test_conversation_events_reject_invalid_queries_before_host_call(self) -> None:
        invalid_queries = (
            {"since": ["nope"]},
            {"since": ["1", "2"]},
            {"before": ["2"]},
        )
        for query in invalid_queries:
            with (
                self.subTest(query=query),
                patch.object(backend, "call_admin_api") as host,
                self.assertRaises(backend.AppError) as error,
            ):
                backend.browser_conversation_events(query)
            self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)
            host.assert_not_called()

    def test_follow_up_omits_configuration_and_leaves_it_to_the_host(self) -> None:
        host_task = {
            "task_id": "task_2",
            "thread_id": backend.THREAD_ID,
            "status": "queued",
        }
        with patch.object(backend, "call_admin_api", return_value=host_task) as host:
            self.assertEqual(
                backend.create_message(
                    {"content": "Continue building."},
                    requested_by="user",
                ),
                host_task,
            )

        host.assert_called_once_with(
            "POST",
            "/v1/tasks",
            {
                "input_message": "Requested by user:\nContinue building.",
                "thread_id": "builder",
            },
        )

    def test_app_callback_gets_durable_origin_without_a_second_thread(self) -> None:
        host_task = {
            "task_id": "task_3",
            "thread_id": backend.THREAD_ID,
            "status": "queued",
        }
        with patch.object(backend, "call_admin_api", return_value=host_task) as host:
            self.assertEqual(
                backend.route_browser(
                    "POST",
                    "/runtime/agent-requests",
                    {"content": "Refresh the analysis."},
                ),
                host_task,
            )

        host.assert_called_once_with(
            "POST",
            "/v1/tasks",
            {
                "input_message": "Requested by app:\nRefresh the analysis.",
                "thread_id": "builder",
            },
        )

    def test_first_message_sends_configuration_without_storing_it(self) -> None:
        host_task = {
            "task_id": "task_1",
            "thread_id": backend.THREAD_ID,
            "status": "queued",
        }
        with patch.object(backend, "call_admin_api", return_value=host_task) as host:
            self.assertEqual(
                backend.create_message(
                    {"content": "Build it.", **self.SESSION},
                    requested_by="user",
                ),
                host_task,
            )

        host.assert_called_once_with(
            "POST",
            "/v1/tasks",
            {
                "input_message": "Requested by user:\nBuild it.",
                "thread_id": "builder",
                **self.SESSION,
            },
        )

    def test_partial_configuration_is_rejected_before_the_host_call(self) -> None:
        with (
            patch.object(backend, "call_admin_api") as host,
            self.assertRaises(backend.AppError) as error,
        ):
            backend.create_message(
                {"content": "Build it.", "agent_runtime": "codex"},
                requested_by="user",
            )

        self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)
        host.assert_not_called()


class PersonalWebAppBuilderMockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        smoke_path = REPO_ROOT / "tests" / "apps" / "personal_web_app_builder" / "smoke.py"
        spec = importlib.util.spec_from_file_location("personal_builder_mock_contract", smoke_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.mock: ModuleType = module

    def setUp(self) -> None:
        self.mock.reset_mock_state()
        self.addCleanup(self.mock.reset_mock_state)

    def _seed_built_session(self) -> None:
        with self.mock.MOCK_LOCK:
            self.mock.APP.clear()
            self.mock.APP.update(self.mock._built_app())
            self.mock.HOST_THREAD_SESSION = dict(self.mock.DEFAULT_SESSION)

    def test_mock_starts_at_welcome_and_first_message_builds_the_app(self) -> None:
        self.assertEqual(self.mock.APP, self.mock._empty_app())
        self.assertEqual(self.mock.TASKS, [])
        self.assertIsNone(self.mock.HOST_THREAD_SESSION)

        created = self.mock._route_app_api(
            "POST",
            "messages",
            {
                "content": "Build a weekly focus dashboard.",
                **self.mock.DEFAULT_SESSION,
            },
        )
        self.assertEqual(created["status"], "running")
        self.mock.TASK_DEADLINES[created["task_id"]] = 0

        conversation = self.mock._route_app_api("GET", "conversation", None)

        self.assertEqual(conversation["session"], self.mock.DEFAULT_SESSION)
        self.assertEqual(conversation["tasks"][0]["status"], "completed")
        self.assertIn("Built the dashboard", conversation["tasks"][0]["output_message"])
        self.assertEqual(self.mock.APP["revision"], 1)
        self.assertTrue(self.mock.APP["html"])
        self.assertTrue(self.mock.APP["javascript"])

    def test_mock_runtime_actions_match_revisioned_typed_mutations(self) -> None:
        self._seed_built_session()
        changed = self.mock._route_app_api(
            "POST",
            "runtime/actions",
            {"action": "set", "expected_revision": 1, "path": ["count"], "value": 3},
        )
        appended = self.mock._route_app_api(
            "POST",
            "runtime/actions",
            {
                "action": "append",
                "expected_revision": 2,
                "path": ["priorities"],
                "value": "Polish local mock",
            },
        )

        self.assertEqual(changed["app"]["revision"], 2)
        self.assertEqual(appended["app"]["revision"], 3)
        self.assertEqual(appended["app"]["data"]["priorities"][-1], "Polish local mock")
        appended["app"]["data"]["count"] = 999
        self.assertEqual(self.mock.APP["data"]["count"], 3)
        with self.assertRaises(backend.AppError) as conflict:
            self.mock._route_app_api(
                "POST",
                "runtime/actions",
                {"action": "delete", "expected_revision": 1, "path": ["count"]},
            )
        self.assertEqual(conflict.exception.status, HTTPStatus.CONFLICT)

    def test_mock_chat_models_running_queue_completion_and_stop(self) -> None:
        self._seed_built_session()
        running = self.mock._route_app_api(
            "POST", "runtime/agent-requests", {"content": self.mock.AGENT_PROMPT}
        )
        queued = self.mock._route_app_api(
            "POST", "messages", {"content": "Then simplify the layout."}
        )

        self.assertEqual(running["status"], "running")
        self.assertTrue(running["input_message"].startswith("Requested by app:\n"))
        self.assertEqual(queued["status"], "queued")
        self.assertTrue(queued["input_message"].startswith("Requested by user:\n"))
        initial_events = self.mock._route_app_api(
            "GET", "conversation/events", None, {"since": ["0"]}
        )["events"]
        self.assertEqual(
            [
                event["payload"]["source"]
                for event in initial_events
                if event["task_id"] == running["task_id"]
            ],
            ["user", "agent"],
        )
        self.assertEqual(
            initial_events[-1]["payload"]["message"],
            self.mock.INTERIM_AGENT_MESSAGE,
        )
        self.assertEqual(
            self.mock._route_app_api(
                "POST", f"tasks/{queued['task_id']}/cancel", {}
            ),
            {"status": "accepted"},
        )
        self.mock.TASK_DEADLINES[running["task_id"]] = 0
        conversation = self.mock._route_app_api("GET", "conversation", None)
        completed = next(
            task for task in conversation["tasks"] if task["task_id"] == running["task_id"]
        )
        self.assertEqual(completed["status"], "completed")
        self.assertIn("refreshed the dashboard analysis", completed["output_message"])
        self.assertIn("analysis", self.mock.APP["data"])
        completed_events = self.mock._route_app_api(
            "GET",
            "conversation/events",
            None,
            {"since": [str(initial_events[-1]["seq"])]},
        )["events"]
        self.assertEqual(
            completed_events[-1]["payload"]["message"],
            completed["output_message"],
        )

    def test_mock_conversation_matches_bounded_newest_first_host_view(self) -> None:
        with self.mock.MOCK_LOCK:
            template = self.mock._completed_task_fixture()
            self.mock.TASKS[:] = [
                {
                    **template,
                    "task_id": f"task_builder_{index}",
                    "input_message": "é" * self.mock.builder_backend.CONVERSATION_MESSAGE_BYTES,
                    "created_at": f"2026-07-22T10:{index:02d}:00Z",
                    "updated_at": f"2026-07-22T10:{index:02d}:01Z",
                }
                for index in range(25)
            ]

        conversation = self.mock._route_app_api("GET", "conversation", None)

        self.assertEqual(len(conversation["tasks"]), backend.CONVERSATION_TASK_LIMIT)
        self.assertEqual(conversation["tasks"][0]["task_id"], "task_builder_24")
        self.assertLessEqual(
            len(conversation["tasks"][0]["input_message"].encode()),
            backend.CONVERSATION_MESSAGE_BYTES,
        )
        self.assertTrue(conversation["tasks"][0]["input_message"].endswith("…"))

    def test_mock_chat_enforces_first_and_follow_up_session_configuration(self) -> None:
        with self.mock.MOCK_LOCK:
            self.mock.TASKS.clear()
            self.mock.HOST_THREAD_SESSION = None
        with self.assertRaises(backend.AppError) as missing:
            self.mock._route_app_api("POST", "messages", {"content": "Build it."})
        self.assertEqual(missing.exception.status, HTTPStatus.BAD_REQUEST)

        created = self.mock._route_app_api(
            "POST",
            "messages",
            {
                "content": "Build it.",
                "agent_runtime": "codex",
                "model": "gpt-5.6-terra",
                "effort": "high",
            },
        )
        self.assertEqual(created["agent_runtime"], "codex")
        repeated = self.mock._route_app_api(
            "POST",
            "messages",
            {
                "content": "Change it.",
                "agent_runtime": "codex",
                "model": "gpt-5.6-terra",
                "effort": "high",
            },
        )
        self.assertEqual(repeated["agent_runtime"], "codex")
        with self.assertRaises(backend.AppError) as conflict:
            self.mock._route_app_api(
                "POST",
                "messages",
                {
                    "content": "Change it again.",
                    "agent_runtime": "claude_code",
                    "model": "opus",
                    "effort": "high",
                },
            )
        self.assertEqual(conflict.exception.status, HTTPStatus.CONFLICT)

    def test_mock_empty_history_returns_to_first_run_without_local_state(self) -> None:
        self._seed_built_session()
        with self.mock.MOCK_LOCK:
            self.mock.TASKS.clear()

        conversation = self.mock._route_app_api("GET", "conversation", None)
        self.assertEqual(conversation["tasks"], [])
        self.assertIsNone(conversation["session"])
        follow_up = self.mock._route_app_api(
            "POST",
            "messages",
            {"content": "Keep improving it.", **self.mock.DEFAULT_SESSION},
        )
        self.assertEqual(follow_up["agent_runtime"], "codex")


class PersonalWebAppBuilderDbTests(unittest.TestCase):
    DB_NAME = "trustyclaw_personal_builder_test"
    _initialized = False

    def setUp(self) -> None:
        pg_harness.ensure_database()
        if not self._initialized:
            pg_harness.create_database(self.DB_NAME)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_DB_NAME": self.DB_NAME})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(db.close_pool)
        if not self._initialized:
            migrate.up(quiet=True)
            with db.transaction() as cur:
                cur.execute(
                    """
                    DO $$
                    BEGIN
                      IF NOT EXISTS (
                        SELECT FROM pg_roles
                        WHERE rolname = 'trustyclaw-app-6'
                      ) THEN
                        CREATE ROLE "trustyclaw-app-6" LOGIN;
                      END IF;
                    END
                    $$;
                    """
                )
                cur.execute(
                    'CREATE SCHEMA IF NOT EXISTS app_personal_web_app_builder '
                    'AUTHORIZATION "trustyclaw-app-6"'
                )
            app = app_platform.app_by_id(backend.APP_ID)
            assert app is not None
            for version in app_migrate.pending(app.id):
                app_migrate.apply_sql(app.id, version, connection_user=app.db_role)
                app_migrate.record(app.id, version)
            PersonalWebAppBuilderDbTests._initialized = True
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_personal_web_app_builder")
            cur.execute(
                "UPDATE app_state SET revision = 0, html = '', css = '', javascript = '',"
                " data_json = '{}', updated_at = '1970-01-01T00:00:00Z'"
                " WHERE singleton = TRUE"
            )

    def test_agent_and_runtime_writes_share_one_revision_chain(self) -> None:
        initial = backend.load_app_state()
        self.assertEqual((initial["revision"], initial["data"]), (0, {}))

        created = backend.apply_agent_action({
            "action": "replace_app",
            "expected_revision": 0,
            "html": '<button data-action="increment">Add</button>',
            "css": "button { padding: 1rem; }",
            "javascript": "app.on('increment', () => app.set(['count'], app.data().count + 1));",
            "data": {"count": 1},
        })["app"]
        self.assertEqual((created["revision"], created["data"]), (1, {"count": 1}))

        changed = backend.apply_agent_action({
            "action": "set", "expected_revision": 1, "path": ["count"], "value": 2,
        })["app"]
        self.assertEqual((changed["revision"], changed["data"]), (2, {"count": 2}))

        with self.assertRaises(backend.AppError) as stale:
            backend.apply_agent_action({
                "action": "replace_data", "expected_revision": 1, "data": {"count": 3},
            })
        self.assertEqual(stale.exception.status, HTTPStatus.CONFLICT)


class RouteBoundaryTests(unittest.TestCase):
    def test_agent_namespace_does_not_expose_browser_routes(self) -> None:
        with self.assertRaises(backend.AppError) as error:
            backend.route_agent("GET", "/agent/snapshot", None)
        self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)

    def test_agent_proxy_is_pinned_to_the_builder_thread(self) -> None:
        handler = object.__new__(backend.Handler)
        handler.headers = Message()
        handler.headers["X-TrustyClaw-Agent-App-Proxy"] = backend.APP_ID
        handler.headers["X-TrustyClaw-Agent-Thread"] = "other"
        with self.assertRaises(backend.AppError) as error:
            handler._require_agent_proxy()
        self.assertEqual(error.exception.status, HTTPStatus.UNAUTHORIZED)


if __name__ == "__main__":
    unittest.main()
