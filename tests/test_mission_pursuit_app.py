"""Mission Pursuit app backend tests.

The pure-logic tests cover the agent action protocol: action and view-block
validation, schedule timing, and input composition budgets. The
database-backed tests run the app migration on the scratch cluster and
exercise the full run worker (message -> dispatch -> mid-turn agent
calls through the /agent/ routes -> reap) against a stubbed host admin API.
"""

from __future__ import annotations

import calendar
from http import HTTPStatus
import importlib.util
import json
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

import pg_harness

from host.apps.workspace_kit import engine, server
from host.apps.workspace_kit.config import DomainAction
from host.runtime import app_migrate, app_platform, db, migrate

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_mp_backend() -> Any:
    spec = importlib.util.spec_from_file_location(
        "mission_pursuit_backend", REPO_ROOT / "host" / "apps" / "mission_pursuit" / "backend.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Mission Pursuit is now a thin config over the shared engine. The generic
# machinery the tests drive lives on the engine (bound here to Mission Pursuit's
# config); the dream-cycle seed and its constants stay on the thin mp module.
mp = _load_mp_backend()
engine.configure(mp.CONFIG)
backend = engine

CODEX_SETTINGS = {
    "agent_runtime": "codex",
    "model": "gpt-5.6-terra",
    "effort": "high",
}
CLAUDE_SETTINGS = {
    "agent_runtime": "claude_code",
    "model": "opus",
    "effort": "max",
}


def first_message(content: str, settings: dict[str, str] = CODEX_SETTINGS) -> dict[str, str]:
    return {"content": content, **settings}


class RequestCapTests(unittest.TestCase):
    def test_request_body_cap_fits_the_advertised_message_limit(self) -> None:
        # The admin bridge reserializes UI bodies with ASCII escaping, up to
        # 12 encoded bytes per character.
        self.assertGreaterEqual(backend.MAX_REQUEST_BODY_BYTES, 12 * backend.USER_MESSAGE_LIMIT + 4096)


class RunWorkerWakeTests(unittest.TestCase):
    def test_workspace_poll_only_wakes_the_single_run_worker(self) -> None:
        expected = {"workspace": {"goal": "test"}}
        with (
            patch.object(backend, "workspace_snapshot", return_value=expected),
            patch.object(backend.RUN_WORKER_WAKE, "set") as wake,
            patch.object(backend, "run_worker_tick") as tick,
        ):
            result = backend.route_ui_request("GET", "/workspace", None)

        self.assertEqual(result, expected)
        wake.assert_called_once_with()
        tick.assert_not_called()

    def test_autonomous_fallback_is_thirty_seconds(self) -> None:
        self.assertEqual(backend.RUN_WORKER_IDLE_SECONDS, 30.0)


class ProxyMarkerTests(unittest.TestCase):
    class StubHandler:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers

    def test_operator_and_agent_routes_require_distinct_exact_proxy_markers(self) -> None:
        host = self.StubHandler({"X-TrustyClaw-App-Proxy": backend.APP_ID})
        agent = self.StubHandler({"X-TrustyClaw-Agent-App-Proxy": backend.APP_ID})
        server.Handler._require_host_proxy(host)  # type: ignore[arg-type]
        server.Handler._require_agent_proxy(agent)  # type: ignore[arg-type]

        for headers, check in (
            ({}, server.Handler._require_host_proxy),
            ({"X-TrustyClaw-App-Proxy": "another_app"}, server.Handler._require_host_proxy),
            ({"X-TrustyClaw-App-Proxy": backend.APP_ID}, server.Handler._require_agent_proxy),
        ):
            with self.subTest(headers=headers), self.assertRaises(backend.AppError) as error:
                check(self.StubHandler(headers))  # type: ignore[arg-type]
            self.assertEqual(error.exception.status, HTTPStatus.UNAUTHORIZED)

    def test_unexpected_backend_exception_is_redacted(self) -> None:
        sent: list[tuple[HTTPStatus, dict[str, Any]]] = []

        class Stub:
            path = "/workspace"
            headers = {"X-TrustyClaw-App-Proxy": backend.APP_ID}

            def _read_body(self) -> None:
                return None

            def _require_host_proxy(self) -> None:
                return None

            def _send_json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
                sent.append((status, body))

        with (
            patch.object(backend, "route_ui_request", side_effect=RuntimeError("database secret")),
            patch.object(server.LOGGER, "exception") as logged,
        ):
            server.Handler._handle(Stub(), "GET")  # type: ignore[arg-type]

        logged.assert_called_once()
        self.assertEqual(sent, [(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": "internal server error"}})])


class ActionShapeTests(unittest.TestCase):
    def test_unknown_action_lists_allowed_actions(self) -> None:
        error = backend.validate_action_shape({"action": "launch_rocket"})
        self.assertIn("unknown action", error)
        self.assertIn("create_artifact", error)

    def test_unknown_action_name_is_bounded_by_encoded_bytes(self) -> None:
        action_name = "\U0001f680" * 10_000
        error = backend.validate_action_shape({"action": action_name})
        self.assertIsNotNone(error)
        self.assertNotIn(action_name, error or "")
        self.assertLess(len((error or "").encode()), 1_000)

    def test_set_goal(self) -> None:
        self.assertIsNone(backend.validate_action_shape({"action": "set_goal", "goal": "x"}))
        self.assertIsNone(backend.validate_action_shape({"action": "set_goal", "goal": ""}))
        self.assertIn("goal must be at most", backend.validate_action_shape({"action": "set_goal", "goal": "x" * 501}))
        self.assertIn("unsupported field", backend.validate_action_shape({"action": "set_goal", "goal": "x", "bonus": 1}))

    def test_create_artifact(self) -> None:
        good = {"action": "create_artifact", "artifact_id": "notes", "title": "Notes", "data": {"a": 1}}
        self.assertIsNone(backend.validate_action_shape(good))
        self.assertIn("artifact_id must match", backend.validate_action_shape({**good, "artifact_id": "Bad Slug"}))
        self.assertIn("missing required field", backend.validate_action_shape({"action": "create_artifact", "artifact_id": "a"}))
        too_big = {**good, "data": "x" * (backend.DATA_LIMIT + 1)}
        self.assertIn("data must be at most", backend.validate_action_shape(too_big))
        non_finite = {**good, "data": {"x": float("nan")}}
        self.assertIn("finite numbers", backend.validate_action_shape(non_finite))
        nested: Any = []
        for _ in range(100_000):
            nested = [nested]
        self.assertIn("JSON-serializable", backend.validate_action_shape({**good, "data": nested}))

    def test_update_artifact_needs_a_field_and_allows_view_null(self) -> None:
        self.assertIn(
            "needs at least one of",
            backend.validate_action_shape({"action": "update_artifact", "artifact_id": "a"}),
        )
        self.assertIsNone(backend.validate_action_shape({"action": "update_artifact", "artifact_id": "a", "view": None}))
        self.assertIn(
            "view must be a list",
            backend.validate_action_shape({"action": "create_artifact", "artifact_id": "a", "title": "t", "view": None}),
        )

    def test_create_schedule_cadence(self) -> None:
        base = {"action": "create_schedule", "schedule_id": "brief", "title": "Brief", "prompt": "do it"}
        self.assertIsNone(backend.validate_action_shape({**base, "every_minutes": 30}))
        self.assertIsNone(backend.validate_action_shape({**base, "at": "2027-01-01T09:00:00Z"}))
        self.assertIn("needs every_minutes", backend.validate_action_shape(base))
        self.assertIn("not both", backend.validate_action_shape({**base, "every_minutes": 30, "at": "2027-01-01T09:00:00Z"}))
        self.assertIn("must be between", backend.validate_action_shape({**base, "every_minutes": 1}))
        self.assertIn("must be an integer", backend.validate_action_shape({**base, "every_minutes": True}))
        self.assertIn("must be an integer", backend.validate_action_shape({**base, "every_minutes": 30.5}))
        self.assertIn("UTC timestamp", backend.validate_action_shape({**base, "at": "tomorrow"}))

    def test_update_schedule(self) -> None:
        self.assertIn(
            "needs at least one of",
            backend.validate_action_shape({"action": "update_schedule", "schedule_id": "a"}),
        )
        self.assertIsNone(backend.validate_action_shape({"action": "update_schedule", "schedule_id": "a", "enabled": False}))
        self.assertIn(
            "enabled must be",
            backend.validate_action_shape({"action": "update_schedule", "schedule_id": "a", "enabled": "yes"}),
        )


class BuilderActionShapeTests(unittest.TestCase):
    def test_set_measurement(self) -> None:
        self.assertIsNone(backend.validate_action_shape({"action": "set_measurement", "measurement": "weekly installs"}))
        self.assertIsNone(backend.validate_action_shape({"action": "set_measurement", "measurement": ""}))
        error = backend.validate_action_shape({"action": "set_measurement", "measurement": "x" * (backend.MEASUREMENT_LIMIT + 1)})
        self.assertIn("at most", error or "")

    def test_remember_and_forget(self) -> None:
        self.assertIsNone(backend.validate_action_shape({"action": "remember", "memory_id": "boss_tz", "content": "UTC+2"}))
        self.assertIsNone(backend.validate_action_shape({"action": "forget", "memory_id": "boss_tz"}))
        self.assertIn("memory_id must match", backend.validate_action_shape({"action": "remember", "memory_id": "Bad Slug", "content": "x"}) or "")
        self.assertIn("at most", backend.validate_action_shape({"action": "remember", "memory_id": "ok", "content": "x" * (backend.MEMORY_CONTENT_LIMIT + 1)}) or "")
        self.assertIn("unsupported field", backend.validate_action_shape({"action": "forget", "memory_id": "ok", "extra": 1}) or "")

    def test_upsert_tool(self) -> None:
        self.assertIsNone(
            backend.validate_action_shape(
                {"action": "upsert_tool", "tool_id": "gmail", "title": "Gmail", "priority": "must_have", "status": "implemented", "note": "ask to enable"}
            )
        )
        self.assertIn(
            "priority must be one of",
            backend.validate_action_shape({"action": "upsert_tool", "tool_id": "gmail", "title": "Gmail", "priority": "critical", "status": "enabled"}) or "",
        )
        self.assertIn(
            "status must be one of",
            backend.validate_action_shape({"action": "upsert_tool", "tool_id": "gmail", "title": "Gmail", "priority": "must_have", "status": "on"}) or "",
        )
        self.assertIsNone(backend.validate_action_shape({"action": "delete_tool", "tool_id": "gmail"}))

class DreamScheduleTests(unittest.TestCase):
    def test_next_dream_run_is_the_next_utc_dream_hour(self) -> None:
        before = calendar.timegm((2026, 7, 9, mp.DREAM_HOUR_UTC - 1, 30, 0, 0, 0, 0))
        after = calendar.timegm((2026, 7, 9, mp.DREAM_HOUR_UTC, 0, 1, 0, 0, 0))
        self.assertEqual(backend.format_utc(mp._next_dream_epoch(before)), f"2026-07-09T{mp.DREAM_HOUR_UTC:02d}:00:00Z")
        self.assertEqual(backend.format_utc(mp._next_dream_epoch(after)), f"2026-07-10T{mp.DREAM_HOUR_UTC:02d}:00:00Z")

    def test_dream_prompt_fits_the_schedule_prompt_cap(self) -> None:
        self.assertLessEqual(len(mp.DREAM_PROMPT), backend.SCHEDULE_PROMPT_LIMIT)


class ViewValidationTests(unittest.TestCase):
    def test_all_block_types_validate(self) -> None:
        view = [
            {"type": "heading", "text": "Title", "level": 1},
            {"type": "text", "text": "Some **bold** words"},
            {"type": "callout", "title": "Heads up", "text": "Something changed", "tone": "warning"},
            {"type": "metrics", "items": [{"label": "Total", "value": "42", "delta": "+3"}]},
            {"type": "cards", "items": [{"title": "Launch", "text": "Ready", "badge": "On track", "tone": "success"}]},
            {"type": "details", "items": [{"label": "Owner", "value": "Sam"}]},
            {"type": "list", "style": "number", "items": ["First", "Second"]},
            {"type": "table", "columns": ["A", "B"], "rows": [["1", "2"]]},
            {"type": "checklist", "items": [{"text": "step", "done": True}]},
            {"type": "progress", "label": "Build", "value": 55},
            {"type": "timeline", "items": [{"title": "Kickoff", "status": "done", "time": "Monday"}]},
            {"type": "kanban", "columns": [{"title": "Doing", "items": ["Ship it"]}, {"title": "Done", "items": []}]},
            {"type": "chart", "kind": "bar", "label": "Runs", "points": [{"label": "Mon", "value": 3}, {"label": "Tue", "value": 5}]},
            {"type": "code", "text": "x = 1", "language": "python"},
            {"type": "button", "control_id": "approve", "label": "Approve", "tone": "primary"},
            {"type": "toggle", "control_id": "auto_run", "label": "Auto-run", "value": True},
            {"type": "field", "control_id": "owner_note", "label": "Owner note", "value": "Ready", "placeholder": "Add a note"},
            {"type": "divider"},
        ]
        self.assertIsNone(backend.validate_view(view))

    def test_unknown_block_type_names_allowed_types(self) -> None:
        error = backend.validate_view([{"type": "iframe", "src": "https://evil"}])
        self.assertIn("unknown block type", error)
        self.assertIn("chart", error)

    def test_block_count_cap(self) -> None:
        error = backend.validate_view([{"type": "divider"}] * (backend.MAX_VIEW_BLOCKS + 1))
        self.assertIn("at most", error)

    def test_serialized_view_cap(self) -> None:
        view = [{"type": "text", "text": "x" * 4000}] * 5
        self.assertIn("characters when serialized", backend.validate_view(view))

    def test_chart_rejects_bad_points(self) -> None:
        self.assertIn(
            "2 to 60 points",
            backend.validate_view([{"type": "chart", "kind": "bar", "points": [{"label": "a", "value": 1}]}]),
        )
        self.assertIn(
            "finite numbers",
            backend.validate_view(
                [{"type": "chart", "kind": "line", "points": [{"label": "a", "value": 1}, {"label": "b", "value": True}]}]
            ),
        )
        self.assertIn(
            "kind must be",
            backend.validate_view([{"type": "chart", "kind": "pie", "points": [{"label": "a", "value": 1}, {"label": "b", "value": 2}]}]),
        )

    def test_table_row_shape_must_match_columns(self) -> None:
        error = backend.validate_view([{"type": "table", "columns": ["A"], "rows": [["1", "2"]]}])
        self.assertIn("match the number of columns", error)

    def test_progress_bounds(self) -> None:
        self.assertIn("between 0 and 100", backend.validate_view([{"type": "progress", "value": 120}]))

    def test_rich_blocks_reject_unbounded_or_unknown_shapes(self) -> None:
        self.assertIn(
            "callout tone",
            backend.validate_view([{"type": "callout", "text": "x", "tone": "rainbow"}]) or "",
        )
        self.assertIn(
            "1 to 12",
            backend.validate_view([{"type": "cards", "items": [{"title": str(i)} for i in range(13)]}]) or "",
        )
        self.assertIn(
            "timeline status",
            backend.validate_view([{"type": "timeline", "items": [{"title": "x", "status": "blocked"}]}]) or "",
        )
        self.assertIn(
            "1 to 6",
            backend.validate_view(
                [{"type": "kanban", "columns": [{"title": str(i), "items": []} for i in range(7)]}]
            )
            or "",
        )

    def test_interactive_blocks_are_typed_and_control_ids_are_unique(self) -> None:
        self.assertIn(
            "button tone",
            backend.validate_view([{"type": "button", "control_id": "go", "label": "Go", "tone": "script"}]) or "",
        )
        self.assertIn(
            "true or false",
            backend.validate_view([{"type": "toggle", "control_id": "auto", "label": "Auto", "value": "yes"}]) or "",
        )
        self.assertIn(
            "at most 1000",
            backend.validate_view(
                [{"type": "field", "control_id": "note", "label": "Note", "value": "x" * (backend.FIELD_VALUE_LIMIT + 1)}]
            )
            or "",
        )
        self.assertIn(
            "duplicate control_id",
            backend.validate_view(
                [
                    {"type": "button", "control_id": "same", "label": "First"},
                    {"type": "toggle", "control_id": "same", "label": "Second", "value": False},
                ]
            )
            or "",
        )


class InteractionRequestShapeTests(unittest.TestCase):
    def test_request_rejects_capability_fields_and_invalid_ids_before_storage(self) -> None:
        with self.assertRaises(backend.AppError) as extra:
            backend.submit_artifact_interaction(
                {
                    "artifact_id": "tracker",
                    "control_id": "approve",
                    "value": True,
                    "route": "/agent/actions",
                }
            )
        self.assertEqual(extra.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("unsupported field: route", extra.exception.message)

        with self.assertRaises(backend.AppError) as invalid:
            backend.submit_artifact_interaction(
                {"artifact_id": "tracker", "control_id": "../../escape", "value": True}
            )
        self.assertEqual(invalid.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("control_id must match", invalid.exception.message)


class ScheduleTimingTests(unittest.TestCase):
    def test_parse_and_format_round_trip(self) -> None:
        self.assertEqual(backend.format_utc(backend.parse_utc("2026-07-09T15:00:00Z")), "2026-07-09T15:00:00Z")
        self.assertIsNone(backend.parse_utc("2026-07-09 15:00"))

    def test_next_run_is_drift_free(self) -> None:
        due = backend.parse_utc("2026-07-09T15:00:00Z")
        # Fired 7 minutes late on a 5-minute cadence: the next slot stays on
        # the original grid (:10), not 5 minutes after the late fire (:12).
        now = due + 7 * 60
        self.assertEqual(backend.format_utc(backend.schedule_next_run(due, 5, now)), "2026-07-09T15:10:00Z")
        # Fired on time: exactly one period later.
        self.assertEqual(backend.format_utc(backend.schedule_next_run(due, 5, due)), "2026-07-09T15:05:00Z")

    def test_past_one_shot_clamps_to_now(self) -> None:
        now = backend.parse_utc("2026-07-09T15:00:00Z")
        result = backend._initial_next_run({"at": "2020-01-01T00:00:00Z"}, now)
        self.assertEqual(result, "2026-07-09T15:00:00Z")

    def test_cadence_text(self) -> None:
        self.assertEqual(backend.cadence_text(30, None), "every 30 minutes")
        self.assertEqual(backend.cadence_text(60, None), "every hour")
        self.assertEqual(backend.cadence_text(2880, None), "every 2 days")
        self.assertEqual(backend.cadence_text(None, "2026-07-09T15:00:00Z"), "once at 2026-07-09T15:00:00Z")


class InputCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        # The engine holds one module-level config: in production each app is
        # its own process, but sibling app test modules configure the shared
        # engine at import time, so reassert Mission Pursuit's config here.
        backend.configure(mp.CONFIG)

    def test_ui_exposes_the_host_session_option_matrix(self) -> None:
        response = backend.route_ui_request("GET", "/session-options", None)
        self.assertIn("codex", response["session_options"])
        self.assertIn("claude_code", response["session_options"])
        self.assertIn("gpt-5.6-terra", response["session_options"]["codex"])

    def test_digest_lists_workspace_state(self) -> None:
        digest = backend.build_digest(
            "Ship v1",
            "Weekly active installs",
            [
                {
                    "schedule_id": "brief",
                    "title": "Brief",
                    "every_minutes": 30,
                    "next_run_at": "2026-07-09T15:00:00Z",
                    "enabled": True,
                    "last_run_status": "completed",
                }
            ],
            [{"artifact_id": "notes", "title": "Notes", "has_view": True, "data_chars": 120, "updated_at": "2026-07-09T14:00:00Z"}],
            [{"memory_id": "boss_timezone", "content": "The human works from UTC+2."}],
            [{"tool_id": "gmail", "title": "Gmail", "priority": "must_have", "status": "implemented", "note": "ask to enable"}],
        )
        self.assertIn("Goal: Ship v1", digest)
        self.assertIn("Measurement: Weekly active installs", digest)
        self.assertIn("brief: \"Brief\" every 30 minutes, enabled", digest)
        self.assertIn("notes: \"Notes\"", digest)
        self.assertIn("boss_timezone: The human works from UTC+2.", digest)
        self.assertIn('gmail: "Gmail" must_have, implemented — ask to enable', digest)

    def test_digest_truncates_long_artifact_lists(self) -> None:
        artifacts = [
            {"artifact_id": f"a{i}", "title": f"A{i}", "has_view": False, "data_chars": 4, "updated_at": "2026-01-01T00:00:00Z"}
            for i in range(backend.DIGEST_ARTIFACT_LINES + 5)
        ]
        digest = backend.build_digest("", "", [], artifacts, [], [])
        self.assertIn("...and 5 more", digest)

    def test_digest_budget_trims_artifacts_before_memories(self) -> None:
        artifacts = [
            {"artifact_id": f"artifact_{i}", "title": "T" * backend.TITLE_LIMIT, "has_view": True, "data_chars": 16000, "updated_at": "2026-01-01T00:00:00Z"}
            for i in range(40)
        ]
        memories = [{"memory_id": f"memory_{i}", "content": "m" * backend.MEMORY_CONTENT_LIMIT} for i in range(backend.MAX_MEMORIES)]
        unbudgeted = backend.build_digest("goal", "measure", [], artifacts, memories, [])
        budget = len(unbudgeted) - 2000
        digest = backend.build_digest("goal", "measure", [], artifacts, memories, [], budget=budget)
        self.assertLessEqual(len(digest), budget)
        # Memories survive whole while the artifact tail is trimmed.
        self.assertEqual(digest.count("- memory_"), backend.MAX_MEMORIES)
        self.assertIn("more", digest)

    def test_digest_budget_floor_always_fits_a_full_chat_input(self) -> None:
        # Worst-case everything: setup brief + bounded provider-rotation
        # context + maximum message must leave the floor-trimmed digest room.
        schedules = [
            {"schedule_id": f"s{i}" + "s" * 46, "title": "T" * backend.TITLE_LIMIT, "every_minutes": 7, "next_run_at": "2026-07-09T15:00:00Z", "enabled": True, "last_run_status": "completed"}
            for i in range(backend.MAX_SCHEDULES)
        ]
        artifacts = [
            {"artifact_id": f"a{i}" + "a" * 46, "title": "T" * backend.TITLE_LIMIT, "has_view": True, "data_chars": 16000, "updated_at": "2026-07-09T15:00:00Z"}
            for i in range(backend.MAX_ARTIFACTS)
        ]
        memories = [{"memory_id": f"m{i}" + "m" * 46, "content": "x" * backend.MEMORY_CONTENT_LIMIT} for i in range(backend.MAX_MEMORIES)]
        tools = [
            {"tool_id": f"t{i}" + "t" * 46, "title": "T" * backend.TITLE_LIMIT, "priority": "good_to_have", "status": "not_implemented", "note": "n" * backend.TOOL_NOTE_LIMIT}
            for i in range(backend.MAX_TOOLS)
        ]
        message = "m" * backend.USER_MESSAGE_LIMIT
        recent = (
            "Human: " + "h" * backend.RECENT_CONTEXT_MESSAGE_LIMIT + "\n"
            "Agent: " + "a" * backend.RECENT_CONTEXT_MESSAGE_LIMIT
        )
        brief_length = len(backend.SETUP_BRIEF) + 2
        recent_length = len("== Recent conversation ==\n") + len(recent) + 2
        budget = backend.HOST_INPUT_LIMIT - brief_length - recent_length - len(message) - 64
        digest = backend.build_digest("g" * backend.GOAL_LIMIT, "m" * backend.MEASUREMENT_LIMIT, schedules, artifacts, memories, tools, budget=budget)
        composed = backend.compose_input("chat", digest, message, setup=True, recent_context=recent)
        self.assertLessEqual(len(composed), backend.HOST_INPUT_LIMIT)
        self.assertIn("== Recent conversation ==", composed)

    def test_composed_chat_input_fits_host_limit(self) -> None:
        digest = backend.build_digest("g" * backend.GOAL_LIMIT, "", [], [], [], [])
        message = "m" * backend.USER_MESSAGE_LIMIT
        composed = backend.compose_input("chat", digest, message)
        self.assertLessEqual(len(composed), backend.HOST_INPUT_LIMIT)
        self.assertTrue(composed.startswith("== Workspace state =="))
        self.assertNotIn("== Builder setup ==", composed)
        self.assertIn("== Message from the human ==", composed)

    def test_recent_context_is_between_setup_and_workspace_state(self) -> None:
        composed = backend.compose_input(
            "chat",
            "== Workspace state ==\nGoal: x",
            "continue",
            setup=True,
            recent_context="Human: prior\nAgent: understood",
        )
        self.assertLess(
            composed.index("== Builder setup =="),
            composed.index("== Recent conversation =="),
        )
        self.assertLess(
            composed.index("== Recent conversation =="),
            composed.index("== Workspace state =="),
        )

    def test_recent_context_is_one_bounded_exchange_oldest_first(self) -> None:
        class Cursor:
            parameters: tuple[int, int] | None = None

            def execute(self, _query: str, parameters: tuple[int, int]) -> None:
                self.parameters = parameters

            def fetchall(self) -> list[tuple[str, str]]:
                # SQL returns newest first; composition reverses the two rows.
                return [
                    ("agent", "a" * (backend.RECENT_CONTEXT_MESSAGE_LIMIT + 10)),
                    ("user", "u" * (backend.RECENT_CONTEXT_MESSAGE_LIMIT + 10)),
                ]

        cursor = Cursor()
        context = backend.build_recent_context(cursor, 99)
        self.assertEqual(cursor.parameters, (99, backend.RECENT_CONTEXT_MESSAGES))
        clipped = backend.RECENT_CONTEXT_MESSAGE_LIMIT - 1
        self.assertTrue(context.startswith("Human: " + "u" * clipped + "…\n"))
        self.assertTrue(context.endswith("Agent: " + "a" * clipped + "…"))

    def test_setup_brief_rides_along_until_a_goal_exists(self) -> None:
        digest = backend.build_digest("", "", [], [], [], [])
        composed = backend.compose_input("chat", digest, "hi", setup=True)
        self.assertIn("== Builder setup ==", composed)
        self.assertIn("ambitious goal", composed)


class ClipEncodedTextTests(unittest.TestCase):
    def test_ascii_within_budget_is_untouched(self) -> None:
        self.assertEqual(backend.clip_encoded_text("hello", 100), ("hello", False))

    def test_ascii_over_budget_clips_to_budget(self) -> None:
        clipped, truncated = backend.clip_encoded_text("x" * 200, 50)
        self.assertTrue(truncated)
        self.assertEqual(clipped, "x" * 50)

    def test_budget_counts_encoded_bytes_not_characters(self) -> None:
        # Each emoji encodes to 12 bytes (a surrogate pair of \uXXXX escapes)
        # under default json.dumps, so far fewer than budget/1 characters fit.
        clipped, truncated = backend.clip_encoded_text("\U0001f600" * 100, 120)
        self.assertTrue(truncated)
        self.assertEqual(clipped, "\U0001f600" * 10)
        self.assertLessEqual(len(json.dumps(clipped).encode()) - 2, 120)

    def test_escaped_quotes_count_toward_budget(self) -> None:
        clipped, truncated = backend.clip_encoded_text('"' * 100, 50)
        self.assertTrue(truncated)
        self.assertEqual(clipped, '"' * 25)


class MissionPursuitDbTests(unittest.TestCase):
    DB_NAME = "trustyclaw_mission_pursuit_test"
    _initialized = False

    def setUp(self) -> None:
        # Re-bind the shared engine to Mission Pursuit in case another app's
        # test module reconfigured it during import.
        backend.configure(mp.CONFIG)
        pg_harness.ensure_database()
        if not MissionPursuitDbTests._initialized:
            pg_harness.create_database(self.DB_NAME)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_DB_NAME": self.DB_NAME})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        # Do not leave this app-specific database in the process-wide pool
        # after the environment is restored for the next test module.
        self.addCleanup(db.close_pool)
        if not MissionPursuitDbTests._initialized:
            migrate.up(quiet=True)
            with db.transaction() as cur:
                cur.execute(
                    """
                    DO $$
                    BEGIN
                      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'trustyclaw-app-mission_pursuit') THEN
                        CREATE ROLE "trustyclaw-app-mission_pursuit" LOGIN;
                      END IF;
                    END
                    $$;
                    """
                )
                cur.execute('CREATE SCHEMA IF NOT EXISTS app_mission_pursuit AUTHORIZATION "trustyclaw-app-mission_pursuit"')
            app = app_platform.app_by_id("mission_pursuit")
            assert app is not None
            for version in app_migrate.pending(app.id):
                app_migrate.apply_sql(app.id, version, connection_user=app.db_role)
                app_migrate.record(app.id, version)
            MissionPursuitDbTests._initialized = True
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute(
                "TRUNCATE workspace, messages, runs, schedules, artifacts, memories, tools"
                " RESTART IDENTITY CASCADE"
            )
        backend._DISPATCH_ATTEMPTS.clear()
        backend._AGENT_ACTIONS_BY_TASK.clear()
        self.host_calls: list[tuple[str, str, Any]] = []
        self.host_responses: dict[str, Any] = {}
        self.host_threads: set[str] = set()

        def fake_admin_api(method: str, path: str, body: Any = None) -> dict[str, Any]:
            self.host_calls.append((method, path, body))
            if method == "POST" and path == "/v1/tasks":
                assert isinstance(body, dict)
                thread_id = body.get("thread_id")
                assert isinstance(thread_id, str)
                config_fields = {"agent_runtime", "model", "effort"} & set(body)
                if thread_id in self.host_threads:
                    self.assertEqual(config_fields, set(), "existing host threads reject session config")
                else:
                    self.assertEqual(
                        config_fields,
                        {"agent_runtime", "model", "effort"},
                        "new host threads require complete session config",
                    )
            key = f"{method} {path}"
            response = self.host_responses.get(key)
            if response is None:
                raise backend.AppError(HTTPStatus.NOT_FOUND, f"unstubbed host call: {key}")
            if isinstance(response, Exception):
                raise response
            if callable(response):
                result = response()
            else:
                result = response
            if method == "POST" and path == "/v1/tasks":
                self.host_threads.add(body["thread_id"])
            return result

        self.api_patch = patch.object(backend, "call_admin_api", side_effect=fake_admin_api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)

    def _messages(self) -> list[tuple[str, str]]:
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute("SELECT role, content FROM messages ORDER BY id ASC")
            return cur.fetchall()

    def _runs(self) -> list[dict[str, Any]]:
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute("SELECT id, kind, status, task_id, schedule_id FROM runs ORDER BY id ASC")
            return [
                {"id": r[0], "kind": r[1], "status": r[2], "task_id": r[3], "schedule_id": r[4]}
                for r in cur.fetchall()
            ]

    def _current_thread(self) -> str:
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute("SELECT thread_seq FROM workspace WHERE singleton = TRUE")
            row = cur.fetchone()
        return f"ws-{row[0] if row else 1}"

    # agent_call drives the agent surface with the trusted app-visible thread
    # marker derived by the host from the runtime scope.
    def agent_call(
        self, method: str, path: str, body: Any = None, thread_id: str | None = None
    ) -> dict[str, Any]:
        return backend.route_agent_request(
            method, path, body, thread_id=thread_id if thread_id is not None else self._current_thread()
        )

    def agent_action(self, action: dict[str, Any]) -> dict[str, Any]:
        return self.agent_call("POST", "/agent/actions", action)

    def agent_action_error(self, action: Any) -> tuple[int, str]:
        try:
            self.agent_action(action)
        except backend.AppError as exc:
            return int(exc.status), exc.message
        raise AssertionError(f"action unexpectedly applied: {action}")

    def test_snapshot_truncates_long_messages_and_serves_full_reads(self) -> None:
        long_content = "x" * backend.USER_MESSAGE_LIMIT
        backend.send_message(first_message(long_content))
        snapshot = backend.workspace_snapshot()
        # Workspace seeding journals the dream cycle into the feed, so pick
        # the user message rather than assuming it is the only row.
        message = next(m for m in snapshot["messages"] if m["role"] == "user")
        self.assertTrue(message["truncated"])
        self.assertEqual(len(message["content"]), backend.SNAPSHOT_MESSAGE_BYTES)
        # A full feed of maximum-size rows stays under the 1 MiB proxy cap.
        worst_case = backend.FEED_MESSAGE_LIMIT * backend.SNAPSHOT_MESSAGE_BYTES
        self.assertLess(worst_case, 1024 * 1024 // 2)
        full = backend.read_message(str(message["id"]))
        self.assertEqual(full["content"], long_content)
        self.assertFalse(full["truncated"])
        with self.assertRaises(backend.AppError):
            backend.read_message("999999")
        with self.assertRaises(backend.AppError):
            backend.read_message("not-a-number")
        # Full reads are capped too: a huge agent reply must not exceed the
        # 1 MiB proxy response limit even at worst-case JSON escaping.
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute(
                "INSERT INTO messages (role, content, created_at) VALUES ('agent', %s, %s) RETURNING id",
                ("y" * (backend.FULL_MESSAGE_BYTES + 100), "2026-01-01T00:00:00Z"),
            )
            huge_id = cur.fetchone()[0]
        huge = backend.read_message(str(huge_id))
        self.assertTrue(huge["truncated"])
        self.assertEqual(len(huge["content"]), backend.FULL_MESSAGE_BYTES)

    def test_dispatch_is_serialized_so_later_turns_see_earlier_results(self) -> None:
        backend.send_message(first_message("first turn"))
        backend.send_message({"content": "second turn"})
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        runs = self._runs()
        self.assertEqual([run["status"] for run in runs], ["active", "pending"])
        # While the first run is active, the second stays undispatched.
        backend._dispatch_pending_runs()
        self.assertEqual([run["status"] for run in self._runs()], ["active", "pending"])
        self.agent_action({"action": "create_artifact", "artifact_id": "made_in_turn_one", "title": "Turn One"})
        self.host_responses["GET /v1/tasks/task_1"] = {"task_id": "task_1", "status": "completed", "output_message": "made it"}
        backend._reap_active_runs()
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_2", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        _, _, body = self.host_calls[-1]
        # The second turn's digest reflects the first turn's applied actions.
        self.assertIn("made_in_turn_one", body["input_message"])
        self.assertIn("second turn", body["input_message"])
        self.assertNotIn("agent_runtime", body)
        self.assertNotIn("model", body)
        self.assertNotIn("effort", body)

    def test_discard_during_dispatch_wins_and_cancels_the_task(self) -> None:
        backend.send_message(first_message("racy"))
        run_id = self._runs()[0]["id"]

        def create_task_and_lose_race() -> dict[str, Any]:
            # The operator discards the queued turn while the host task
            # creation round trip is still in flight.
            backend.discard_pending_run(str(run_id))
            return {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}

        self.host_responses["POST /v1/tasks"] = create_task_and_lose_race
        self.host_responses["POST /v1/tasks/task_1/cancel"] = {"task_id": "task_1", "status": "cancelled"}
        backend._dispatch_pending_runs()
        run = self._runs()[0]
        self.assertEqual(run["status"], "done")
        self.assertIsNone(run["task_id"])
        self.assertIn(("POST", "/v1/tasks/task_1/cancel", {}), self.host_calls)

    def test_discard_unblocks_a_pending_run(self) -> None:
        backend.send_message(first_message("stuck"))
        run_id = self._runs()[0]["id"]
        result = backend.discard_pending_run(str(run_id))
        self.assertTrue(result["discarded"])
        self.assertEqual(self._runs()[0]["status"], "done")
        self.assertTrue(any("Discarded a queued turn" in content for _, content in self._messages()))
        with self.assertRaises(backend.AppError):
            backend.discard_pending_run(str(run_id))  # already done

    def test_first_message_requires_complete_settings_then_creates_pending_run(self) -> None:
        with self.assertRaises(backend.AppError) as caught:
            backend.send_message({"content": "hello"})
        self.assertEqual(caught.exception.status, HTTPStatus.BAD_REQUEST)

        with self.assertRaises(backend.AppError) as incomplete:
            backend.send_message({"content": "hello", "agent_runtime": "codex"})
        self.assertIn("model, and effort", incomplete.exception.message)

        backend.send_message(first_message("hello"))
        # Workspace creation seeds the dream cycle (an event row) before the
        # first user message lands.
        self.assertEqual([m for m in self._messages() if m[0] == "user"], [("user", "hello")])
        runs = self._runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["kind"], runs[0]["status"]), ("chat", "pending"))
        snapshot = backend.workspace_snapshot()
        self.assertEqual(snapshot["workspace"]["agent_runtime"], "codex")
        self.assertEqual(snapshot["workspace"]["model"], "gpt-5.6-terra")
        self.assertEqual(snapshot["workspace"]["effort"], "high")
        self.assertEqual(len(snapshot["busy"]), 1)

    def test_dispatch_creates_host_task_with_composed_input(self) -> None:
        backend.send_message(first_message("hello there"))
        self.host_responses["POST /v1/tasks"] = {
            "task_id": "task_9",
            "thread_id": "ws-1",
            "agent_runtime": "codex",
            "status": "queued",
        }
        backend._dispatch_pending_runs()
        runs = self._runs()
        self.assertEqual(runs[0]["status"], "active")
        self.assertEqual(runs[0]["task_id"], "task_9")
        method, path, body = self.host_calls[-1]
        self.assertEqual((method, path), ("POST", "/v1/tasks"))
        self.assertEqual(body["thread_id"], "ws-1")
        self.assertEqual(body["agent_runtime"], "codex")
        self.assertEqual(body["model"], "gpt-5.6-terra")
        self.assertEqual(body["effort"], "high")
        self.assertTrue(body["input_message"].startswith("== Builder setup =="))
        self.assertIn("== Workspace state ==", body["input_message"])
        self.assertIn("== Message from the human ==\nhello there", body["input_message"])
        self.assertLessEqual(len(body["input_message"]), backend.HOST_INPUT_LIMIT)

    def test_dispatch_failure_records_error_and_throttles(self) -> None:
        backend.send_message(first_message("hello"))
        # No POST /v1/tasks stub: dispatch fails and records the error.
        backend._dispatch_pending_runs()
        runs = self._runs()
        self.assertEqual(runs[0]["status"], "pending")
        snapshot = backend.workspace_snapshot()
        self.assertIn("unstubbed host call", snapshot["busy"][0]["last_error"])
        calls_before = len(self.host_calls)
        backend._dispatch_pending_runs()  # throttled; no new host call
        self.assertEqual(len(self.host_calls), calls_before)

    def test_agent_actions_apply_and_rejections_journal_and_return(self) -> None:
        backend.send_message(first_message("build me a tracker"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()

        self.agent_action({"action": "set_goal", "goal": "Track the launch"})
        applied = self.agent_action(
            {
                "action": "create_artifact",
                "artifact_id": "tracker",
                "title": "Launch Tracker",
                "data": {"items": []},
                "view": [{"type": "heading", "text": "Launch Tracker"}],
            }
        )
        self.assertEqual(applied, {"applied": True, "action": "create_artifact"})
        self.agent_action(
            {"action": "create_schedule", "schedule_id": "check", "title": "Check status", "prompt": "Check it", "every_minutes": 30}
        )
        # A rejected action comes back synchronously with the exact reason and
        # is journaled to the feed; nothing about the turn is interrupted.
        status, message = self.agent_action_error({"action": "delete_artifact", "artifact_id": "missing"})
        self.assertEqual(status, int(HTTPStatus.UNPROCESSABLE_ENTITY))
        self.assertIn("does not exist", message)

        self.host_responses["GET /v1/tasks/task_1"] = {"task_id": "task_1", "status": "completed", "output_message": "Here is your tracker."}
        backend._reap_active_runs()

        roles = self._messages()
        self.assertIn(("agent", "Here is your tracker."), roles)
        contents = [content for _, content in roles]
        self.assertTrue(any("Created artifact \"Launch Tracker\"" in content for content in contents))
        self.assertTrue(any("Scheduled \"Check status\" every 30 minutes" in content for content in contents))
        self.assertTrue(any(role == "error" and "does not exist" in content for role, content in roles))

        snapshot = backend.workspace_snapshot()
        self.assertEqual(snapshot["workspace"]["goal"], "Track the launch")
        self.assertEqual(len(snapshot["artifacts"]), 1)
        # The seeded dream cycle plus the schedule this turn created.
        self.assertEqual(
            {schedule["schedule_id"] for schedule in snapshot["schedules"]},
            {mp.DREAM_SCHEDULE_ID, "check"},
        )
        self.assertEqual(snapshot["busy"], [])

        artifact = backend.read_artifact("tracker")
        self.assertEqual(artifact["view"], [{"type": "heading", "text": "Launch Tracker"}])

    def test_agent_surface_only_serves_the_active_turn(self) -> None:
        # Before any turn is active there is nothing to attribute to.
        status, message = self.agent_action_error({"action": "set_goal", "goal": "x"})
        self.assertEqual(status, int(HTTPStatus.FORBIDDEN))

        backend.send_message(first_message("go"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        # A stale pre-settings-change thread cannot act.
        try:
            self.agent_call("GET", "/agent/workspace", thread_id="ws-99")
        except backend.AppError as exc:
            self.assertEqual(int(exc.status), int(HTTPStatus.FORBIDDEN))
            self.assertIn("current workspace session", exc.message)
        else:
            raise AssertionError("stale thread unexpectedly served")
        # The active task itself is served.
        self.agent_action({"action": "set_goal", "goal": "x"})
        # Once the turn is reaped, the same task fails closed again.
        self.host_responses["GET /v1/tasks/task_1"] = {"task_id": "task_1", "status": "completed", "output_message": "ok"}
        backend._reap_active_runs()
        status, _ = self.agent_action_error({"action": "set_goal", "goal": "y"})
        self.assertEqual(status, int(HTTPStatus.FORBIDDEN))

    def test_agent_reads_serve_artifacts_and_workspace_state(self) -> None:
        backend.send_message(first_message("read things"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        self.agent_action(
            {"action": "create_artifact", "artifact_id": "notes", "title": "Notes", "data": {"k": "v"}}
        )
        self.agent_action({"action": "remember", "memory_id": "boss_tz", "content": "UTC+2"})

        artifact = self.agent_call("GET", "/agent/artifacts/notes")["artifact"]
        self.assertEqual(artifact["data"], {"k": "v"})
        self.assertEqual(artifact["title"], "Notes")
        try:
            self.agent_call("GET", "/agent/artifacts/missing")
        except backend.AppError as exc:
            self.assertEqual(int(exc.status), int(HTTPStatus.NOT_FOUND))
        else:
            raise AssertionError("missing artifact unexpectedly served")

        state = self.agent_call("GET", "/agent/workspace")
        self.assertEqual([a["artifact_id"] for a in state["artifacts"]], ["notes"])
        self.assertEqual([m["memory_id"] for m in state["memories"]], ["boss_tz"])
        self.assertIn(mp.DREAM_SCHEDULE_ID, [s["schedule_id"] for s in state["schedules"]])

    def test_agent_action_budget_is_per_turn(self) -> None:
        backend.send_message(first_message("spam"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        for index in range(backend.MAX_ACTIONS_PER_TURN):
            self.agent_action({"action": "set_goal", "goal": f"goal {index}"})
        status, message = self.agent_action_error({"action": "set_goal", "goal": "one too many"})
        self.assertEqual(status, int(HTTPStatus.TOO_MANY_REQUESTS))
        self.assertIn("action budget exhausted", message)
        status, _ = self.agent_action_error({"action": "set_goal", "goal": "still too many"})
        self.assertEqual(status, int(HTTPStatus.TOO_MANY_REQUESTS))
        exhausted = [
            content
            for role, content in self._messages()
            if role == "error" and "action budget exhausted" in content
        ]
        self.assertEqual(len(exhausted), 1)
        # Reads are not counted against the budget.
        self.agent_call("GET", "/agent/workspace")
        # The next turn starts with a fresh budget.
        self.host_responses["GET /v1/tasks/task_1"] = {"task_id": "task_1", "status": "completed", "output_message": "ok"}
        backend._reap_active_runs()
        self._run_turn("again", actions=[{"action": "set_goal", "goal": "fresh"}], task_id="task_2")
        snapshot = backend.workspace_snapshot()
        self.assertEqual(snapshot["workspace"]["goal"], "fresh")

    def test_rejected_domain_action_write_rolls_back(self) -> None:
        backend.send_message(first_message("go"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()

        def write_then_reject(cur: Any, action: dict[str, Any], now: str) -> str | None:
            backend._insert_event(cur, "leaked partial write", {"action": "leaky"}, now)
            cur.execute(
                "INSERT INTO memories (memory_id, content, created_at, updated_at) VALUES (%s, %s, %s, %s)",
                ("leak", "partial", now, now),
            )
            return "rejected after writing"

        leaky = DomainAction(validate=lambda action: None, apply=write_then_reject)
        with patch.dict(engine._DOMAIN_ACTIONS, {"leaky": leaky}):
            status, message = self.agent_action_error({"action": "leaky"})
        self.assertEqual(status, int(HTTPStatus.UNPROCESSABLE_ENTITY))
        self.assertIn("rejected after writing", message)
        # Everything the hook wrote before rejecting rolled back; only the
        # rejection row committed.
        contents = [content for _, content in self._messages()]
        self.assertFalse(any("leaked partial write" in content for content in contents))
        self.assertTrue(any("Action rejected: rejected after writing" in content for content in contents))
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute("SELECT 1 FROM memories WHERE memory_id = 'leak'")
            self.assertIsNone(cur.fetchone())

    def test_action_racing_deactivation_fails_closed(self) -> None:
        backend.send_message(first_message("go"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        # Simulate the race: the route-level attribution check for task_1 has
        # already passed when deactivation commits. The write transaction
        # re-verifies under the workspace row lock and must fail closed.
        backend.deactivate_workspace(None)
        with self.assertRaises(backend.AppError) as raised:
            backend.apply_agent_action(
                {"action": "create_schedule", "schedule_id": "stale", "title": "Stale", "prompt": "p", "every_minutes": 30},
                "task_1",
            )
        self.assertEqual(int(raised.exception.status), int(HTTPStatus.FORBIDDEN))
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute("SELECT 1 FROM schedules WHERE schedule_id = 'stale'")
            self.assertIsNone(cur.fetchone())

    def test_deactivated_workspace_fires_no_schedules(self) -> None:
        backend.send_message(first_message("seed"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        self.agent_action(
            {"action": "create_schedule", "schedule_id": "brief", "title": "Brief", "prompt": "Summarize", "every_minutes": 30}
        )
        self.host_responses["GET /v1/tasks/task_1"] = {"task_id": "task_1", "status": "completed", "output_message": "ok"}
        backend._reap_active_runs()
        backend.deactivate_workspace(None)
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            # Defense in depth: even if an enabled due schedule exists on a
            # deactivated workspace, the firer must not act on it.
            cur.execute(
                "UPDATE schedules SET enabled = TRUE, next_run_at = '2020-01-01T00:00:00Z' WHERE schedule_id = 'brief'"
            )
        backend._fire_due_schedules()
        self.assertEqual([run for run in self._runs() if run["kind"] == "schedule"], [])
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute("SELECT next_run_at FROM schedules WHERE schedule_id = 'brief'")
            # The schedule row is untouched: nothing fires and nothing
            # advances until the workspace is active again.
            self.assertEqual(cur.fetchone()[0], "2020-01-01T00:00:00Z")

    def test_due_schedule_fires_once_and_advances(self) -> None:
        backend.send_message(first_message("seed"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        self.agent_action(
            {"action": "create_schedule", "schedule_id": "brief", "title": "Brief", "prompt": "Summarize", "every_minutes": 30}
        )
        self.host_responses["GET /v1/tasks/task_1"] = {"task_id": "task_1", "status": "completed", "output_message": "scheduled"}
        backend._reap_active_runs()

        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            # Scope to the created schedule: the seeded dream cycle must not fire.
            cur.execute("UPDATE schedules SET next_run_at = '2020-01-01T00:00:00Z' WHERE schedule_id = 'brief'")
        backend._fire_due_schedules()
        runs = self._runs()
        fired = [run for run in runs if run["kind"] == "schedule"]
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["schedule_id"], "brief")
        snapshot = backend.workspace_snapshot()
        brief = next(s for s in snapshot["schedules"] if s["schedule_id"] == "brief")
        self.assertGreater(brief["next_run_at"], backend._utc_now()[:10])

        # While the fired chain is active, the next due tick skips (overlap).
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute("UPDATE schedules SET next_run_at = '2020-01-01T00:00:00Z' WHERE schedule_id = 'brief'")
        backend._fire_due_schedules()
        fired = [run for run in self._runs() if run["kind"] == "schedule"]
        self.assertEqual(len(fired), 1)
        self.assertTrue(any("Skipped scheduled run" in content for _, content in self._messages()))

        self.host_responses["POST /v1/tasks"] = {"task_id": "task_5", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        _, _, body = self.host_calls[-1]
        self.assertIn("== Scheduled run ==", body["input_message"])
        self.assertIn("Summarize", body["input_message"])

    def test_one_shot_fires_and_disables(self) -> None:
        backend.send_message(first_message("seed"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        self.agent_action(
            {"action": "create_schedule", "schedule_id": "once", "title": "Once", "prompt": "Do it", "at": "2020-01-01T00:00:00Z"}
        )
        self.host_responses["GET /v1/tasks/task_1"] = {"task_id": "task_1", "status": "completed", "output_message": "scheduled"}
        backend._reap_active_runs()
        backend._fire_due_schedules()
        snapshot = backend.workspace_snapshot()
        schedule = next(s for s in snapshot["schedules"] if s["schedule_id"] == "once")
        self.assertFalse(schedule["enabled"])
        self.assertIsNone(schedule["next_run_at"])
        with self.assertRaises(backend.AppError):
            backend.set_schedule_enabled("once", True)

    def test_new_time_reenables_a_completed_one_shot(self) -> None:
        backend.send_message(first_message("seed"))
        self.host_responses["POST /v1/tasks"] = {
            "task_id": "task_1",
            "thread_id": "ws-1",
            "agent_runtime": "codex",
            "status": "queued",
        }
        backend._dispatch_pending_runs()
        self.agent_action(
            {
                "action": "create_schedule",
                "schedule_id": "once",
                "title": "Once",
                "prompt": "Do it",
                "at": "2020-01-01T00:00:00Z",
            }
        )
        backend._fire_due_schedules()
        completed = next(s for s in backend.workspace_snapshot()["schedules"] if s["schedule_id"] == "once")
        self.assertFalse(completed["enabled"])

        self.agent_action(
            {"action": "update_schedule", "schedule_id": "once", "at": "2030-01-01T00:00:00Z"}
        )
        rescheduled = next(s for s in backend.workspace_snapshot()["schedules"] if s["schedule_id"] == "once")
        self.assertTrue(rescheduled["enabled"])
        self.assertEqual(rescheduled["next_run_at"], "2030-01-01T00:00:00Z")

    def test_chat_queue_is_capped(self) -> None:
        backend.send_message(first_message("first"))
        for index in range(backend.MAX_QUEUED_CHAT_TURNS - 1):
            backend.send_message({"content": f"queued {index}"})
        with self.assertRaises(backend.AppError) as caught:
            backend.send_message({"content": "one too many"})
        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("queue is full", caught.exception.message)

    def test_agent_settings_require_an_idle_workspace(self) -> None:
        backend.send_message(first_message("first"))
        unchanged = backend.update_agent_settings(CODEX_SETTINGS)
        self.assertFalse(unchanged["changed"])
        self.assertEqual(unchanged["thread_seq"], 1)
        # A pending (undispatched) turn blocks a settings change.
        with self.assertRaises(backend.AppError) as caught:
            backend.update_agent_settings(CLAUDE_SETTINGS)
        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        # An active turn blocks a settings change too.
        with self.assertRaises(backend.AppError) as caught:
            backend.update_agent_settings(CLAUDE_SETTINGS)
        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)
        self.host_responses["GET /v1/tasks/task_1"] = {"task_id": "task_1", "status": "completed", "output_message": "ok"}
        backend._reap_active_runs()
        result = backend.update_agent_settings(CLAUDE_SETTINGS)
        self.assertEqual(result["thread_seq"], 2)
        self.assertTrue(result["changed"])

    def test_agent_settings_validate_the_host_option_matrix(self) -> None:
        backend.send_message(first_message("first"))
        backend.discard_pending_run(str(self._runs()[0]["id"]))
        with self.assertRaises(backend.AppError) as invalid:
            backend.update_agent_settings(
                {"agent_runtime": "codex", "model": "not-a-model", "effort": "high"}
            )
        self.assertEqual(invalid.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("model must be one of", invalid.exception.message)

    def test_agent_settings_rotate_thread_with_recent_context(self) -> None:
        backend.send_message(first_message("first"))
        backend.discard_pending_run(str(self._runs()[0]["id"]))
        result = backend.update_agent_settings(CLAUDE_SETTINGS)
        self.assertEqual(result["thread_seq"], 2)
        self.assertEqual(result["agent_runtime"], "claude_code")
        self.assertEqual(result["model"], "opus")
        self.assertEqual(result["effort"], "max")
        backend.send_message({"content": "after switch"})
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-2", "agent_runtime": "claude_code", "status": "queued"}
        backend._dispatch_pending_runs()
        _, _, body = self.host_calls[-1]
        self.assertEqual(body["thread_id"], "ws-2")
        self.assertEqual(body["agent_runtime"], "claude_code")
        self.assertEqual(body["model"], "opus")
        self.assertEqual(body["effort"], "max")
        self.assertIn("== Recent conversation ==\nHuman: first", body["input_message"])
        self.assertIn("== Message from the human ==\nafter switch", body["input_message"])

    def test_message_settings_after_start_are_rejected(self) -> None:
        backend.send_message(first_message("first"))
        with self.assertRaises(backend.AppError) as caught:
            backend.send_message(first_message("second", CLAUDE_SETTINGS))
        self.assertEqual(caught.exception.status, HTTPStatus.BAD_REQUEST)

    def test_ui_deletes_require_existing_rows(self) -> None:
        with self.assertRaises(backend.AppError):
            backend.delete_artifact_from_ui("missing")
        with self.assertRaises(backend.AppError):
            backend.delete_schedule_from_ui("missing")
        with self.assertRaises(backend.AppError):
            backend.stop_task("task_404")

    def test_rejection_meta_stays_bounded(self) -> None:
        backend.send_message(first_message("try"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        status, _ = self.agent_action_error({"action": "z" * 100_000})
        self.assertEqual(status, int(HTTPStatus.UNPROCESSABLE_ENTITY))
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute("SELECT meta FROM messages WHERE role = 'error' ORDER BY id DESC LIMIT 1")
            meta = json.loads(cur.fetchone()[0])
        self.assertLessEqual(len(meta["action"]), 40)

    def test_non_object_action_bodies_are_rejected(self) -> None:
        backend.send_message(first_message("try"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        for body in (None, "set_goal", ["set_goal"], {"goal": "x"}):
            status, message = self.agent_action_error(body)
            self.assertEqual(status, int(HTTPStatus.UNPROCESSABLE_ENTITY))
            self.assertIn("action", message)

    def test_oversized_task_response_finishes_the_run(self) -> None:
        backend.send_message(first_message("huge"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        self.host_responses["GET /v1/tasks/task_1"] = backend.AppError(
            HTTPStatus.BAD_GATEWAY, "host admin response too large"
        )
        backend._reap_active_runs()
        runs = self._runs()
        self.assertEqual(runs[0]["status"], "done")
        self.assertTrue(any(role == "error" and "too large" in content for role, content in self._messages()))
        # Only the chat reply was lost — mid-turn actions were applied live —
        # and the queue is unblocked for the next turn.
        backend.send_message({"content": "next"})
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_2", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        _, _, body = self.host_calls[-1]
        self.assertIn("== Message from the human ==", body["input_message"])

    def test_failed_task_reports_error(self) -> None:
        backend.send_message(first_message("try"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        self.host_responses["GET /v1/tasks/task_1"] = {"task_id": "task_1", "status": "failed", "error_message": "runtime exploded"}
        backend._reap_active_runs()
        self.assertTrue(any(role == "error" and "runtime exploded" in content for role, content in self._messages()))
        self.assertEqual(self._runs()[0]["status"], "done")

    def _run_turn(
        self,
        content: str,
        actions: list[dict[str, Any]] | None = None,
        reply: str = "done",
        task_id: str = "task_1",
    ) -> None:
        """Send a chat message, dispatch it, apply the given actions through
        the agent surface mid-turn, and reap the plain-chat reply."""
        body: dict[str, Any] = {"content": content}
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute("SELECT agent_runtime FROM workspace WHERE singleton = TRUE")
            row = cur.fetchone()
        if not row or not row[0]:
            body.update(CODEX_SETTINGS)
        backend.send_message(body)
        self.host_responses["POST /v1/tasks"] = {"task_id": task_id, "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        for action in actions or []:
            self.agent_action(action)
        self.host_responses[f"GET /v1/tasks/{task_id}"] = {"task_id": task_id, "status": "completed", "output_message": reply}
        backend._reap_active_runs()

    def test_activation_creates_the_workspace_without_queueing_a_turn(self) -> None:
        # The activation gate is a plain button: settings only, no message.
        result = backend.activate_workspace(dict(CODEX_SETTINGS))
        self.assertEqual(result, {"activated": True})
        snapshot = backend.workspace_snapshot()
        self.assertEqual(snapshot["workspace"]["agent_runtime"], "codex")
        # The seed ran (routines disclosed on the gate) but no agent turn did.
        self.assertIn(mp.DREAM_SCHEDULE_ID, [s["schedule_id"] for s in snapshot["schedules"]])
        self.assertEqual(self._runs(), [])
        with self.assertRaises(backend.AppError) as error:
            backend.activate_workspace(dict(CODEX_SETTINGS))
        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
        with self.assertRaises(backend.AppError):
            backend.activate_workspace({"agent_runtime": "codex"})

    def test_deactivation_stops_work_and_reactivation_keeps_schedules_paused(self) -> None:
        backend.activate_workspace(dict(CODEX_SETTINGS))
        backend.send_message({"content": "first"})
        self.host_responses["POST /v1/tasks"] = {
            "task_id": "task_1",
            "thread_id": "ws-1",
            "agent_runtime": "codex",
            "status": "queued",
        }
        backend._dispatch_pending_runs()
        backend.send_message({"content": "second"})
        pending_run = self._runs()[1]
        backend._DISPATCH_ATTEMPTS[pending_run["id"]] = 1.0

        # A transient host outage does not reopen the app or lose the desired
        # stop: the active row remains tracked for the worker to retry.
        self.host_responses["GET /v1/tasks/task_1"] = OSError("host unavailable")
        result = backend.route_ui_request("POST", "/deactivate", {})
        self.assertEqual(result, {"activated": False, "stopping_tasks": 1})
        snapshot = backend.workspace_snapshot()
        self.assertIsNone(snapshot["workspace"]["agent_runtime"])
        self.assertIsNone(snapshot["workspace"]["model"])
        self.assertIsNone(snapshot["workspace"]["effort"])
        self.assertEqual(snapshot["workspace"]["thread_seq"], 2)
        self.assertTrue(snapshot["messages"], "workspace history is preserved")
        self.assertTrue(all(not schedule["enabled"] for schedule in snapshot["schedules"]))
        with self.assertRaises(backend.AppError) as inactive_schedule:
            backend.set_schedule_enabled(mp.DREAM_SCHEDULE_ID, True)
        self.assertEqual(inactive_schedule.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("reactivate", inactive_schedule.exception.message)
        self.assertTrue(
            all(not schedule["enabled"] for schedule in backend.workspace_snapshot()["schedules"])
        )
        self.assertNotIn(pending_run["id"], backend._DISPATCH_ATTEMPTS)
        self.assertEqual([run["status"] for run in self._runs()], ["active", "done"])
        with self.assertRaises(backend.AppError) as stale_thread:
            self.agent_call("GET", "/agent/workspace", thread_id="ws-1")
        self.assertEqual(stale_thread.exception.status, HTTPStatus.FORBIDDEN)
        with self.assertRaises(backend.AppError) as still_stopping:
            backend.activate_workspace(dict(CODEX_SETTINGS))
        self.assertEqual(still_stopping.exception.status, HTTPStatus.CONFLICT)
        with self.assertRaises(backend.AppError) as message_reactivation:
            backend.send_message(first_message("do not restart"))
        self.assertEqual(message_reactivation.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("still stopping", message_reactivation.exception.message)
        self.assertIsNone(backend.workspace_snapshot()["workspace"]["agent_runtime"])

        self.host_responses["GET /v1/tasks/task_1"] = {
            "task_id": "task_1",
            "status": "queued",
        }
        self.host_responses["POST /v1/tasks/task_1/cancel"] = {
            "task_id": "task_1",
            "status": "cancelled",
        }
        backend._reap_active_runs()
        self.assertIn(("POST", "/v1/tasks/task_1/cancel", {}), self.host_calls)
        self.host_responses["GET /v1/tasks/task_1"] = {
            "task_id": "task_1",
            "status": "cancelled",
        }
        backend._reap_active_runs()
        self.assertEqual([run["status"] for run in self._runs()], ["done", "done"])

        self.assertEqual(backend.activate_workspace(dict(CODEX_SETTINGS)), {"activated": True})
        reactivated = backend.workspace_snapshot()
        self.assertEqual(reactivated["workspace"]["thread_seq"], 2)
        self.assertTrue(all(not schedule["enabled"] for schedule in reactivated["schedules"]))

    def test_first_message_seeds_the_dream_cycle(self) -> None:
        backend.send_message(first_message("hello"))
        snapshot = backend.workspace_snapshot()
        dream = next(s for s in snapshot["schedules"] if s["schedule_id"] == mp.DREAM_SCHEDULE_ID)
        self.assertEqual(dream["every_minutes"], 24 * 60)
        # Enabled from activation: the activation screen disclosed the cycle.
        self.assertTrue(dream["enabled"])
        self.assertTrue(dream["next_run_at"].endswith(f"T{mp.DREAM_HOUR_UTC:02d}:00:00Z"))
        self.assertTrue(any("dream cycle" in content for role, content in self._messages() if role == "event"))
        # It is an ordinary schedule: the operator can delete it, and an agent
        # settings change does not resurrect it.
        backend.discard_pending_run(str(self._runs()[0]["id"]))
        backend.delete_schedule_from_ui(mp.DREAM_SCHEDULE_ID)
        backend.update_agent_settings(CLAUDE_SETTINGS)
        backend.send_message({"content": "again"})
        snapshot = backend.workspace_snapshot()
        self.assertNotIn(mp.DREAM_SCHEDULE_ID, [s["schedule_id"] for s in snapshot["schedules"]])

    def test_setup_brief_rides_until_a_goal_is_set(self) -> None:
        self._run_turn("hi", actions=[{"action": "set_goal", "goal": "Ship the newsletter"}])
        first_input = next(body for method, path, body in self.host_calls if path == "/v1/tasks")["input_message"]
        self.assertIn("== Builder setup ==", first_input)
        backend.send_message({"content": "next"})
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_2", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        _, _, body = self.host_calls[-1]
        self.assertNotIn("== Builder setup ==", body["input_message"])
        self.assertIn("Goal: Ship the newsletter", body["input_message"])

    def test_memory_actions_apply_and_the_operator_can_edit(self) -> None:
        self._run_turn(
            "remember things",
            actions=[
                {"action": "set_measurement", "measurement": "weekly installs"},
                {"action": "remember", "memory_id": "boss_tz", "content": "The human works from UTC+2."},
                {"action": "remember", "memory_id": "style", "content": "Prefers short updates."},
                {"action": "forget", "memory_id": "style"},
            ],
        )
        snapshot = backend.workspace_snapshot()
        self.assertEqual(snapshot["workspace"]["measurement"], "weekly installs")
        self.assertEqual([m["memory_id"] for m in snapshot["memories"]], ["boss_tz"])
        # The next composed input carries the memory.
        backend.send_message({"content": "next"})
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_2", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        _, _, body = self.host_calls[-1]
        self.assertIn("boss_tz: The human works from UTC+2.", body["input_message"])
        self.assertIn("Measurement: weekly installs", body["input_message"])
        # Operator edit and forget, both journaled.
        backend.edit_memory_from_ui("boss_tz", {"content": "The human moved to UTC-5."})
        snapshot = backend.workspace_snapshot()
        self.assertEqual(snapshot["memories"][0]["content"], "The human moved to UTC-5.")
        backend.delete_memory_from_ui("boss_tz")
        self.assertEqual(backend.workspace_snapshot()["memories"], [])
        with self.assertRaises(backend.AppError):
            backend.edit_memory_from_ui("boss_tz", {"content": "gone"})

    def test_memory_cap_is_enforced(self) -> None:
        seeds = [{"action": "remember", "memory_id": f"m{i}", "content": "x"} for i in range(16)]
        self._run_turn("seed", actions=seeds, task_id="task_1")
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            for i in range(16, backend.MAX_MEMORIES):
                cur.execute(
                    "INSERT INTO memories (memory_id, content, created_at, updated_at) VALUES (%s, 'x', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
                    (f"m{i}",),
                )
        backend.send_message({"content": "overflow"})
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_2", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        status, message = self.agent_action_error(
            {"action": "remember", "memory_id": "one_too_many", "content": "x"}
        )
        self.assertEqual(status, int(HTTPStatus.UNPROCESSABLE_ENTITY))
        self.assertIn("memory limit reached", message)
        self.assertTrue(any("memory limit reached" in content for role, content in self._messages() if role == "error"))
        # Replacing an existing memory still works at the cap, in the same turn.
        self.agent_action({"action": "remember", "memory_id": "m1", "content": "updated"})
        self.host_responses["GET /v1/tasks/task_2"] = {"task_id": "task_2", "status": "completed", "output_message": "ok"}
        backend._reap_active_runs()
        snapshot = backend.workspace_snapshot()
        self.assertIn("updated", [m["content"] for m in snapshot["memories"]])

    def test_tools_inventory_applies(self) -> None:
        self._run_turn(
            "tools",
            actions=[
                {"action": "upsert_tool", "tool_id": "gmail", "title": "Gmail", "priority": "must_have", "status": "implemented", "note": "enable it in host tools"},
                {"action": "upsert_tool", "tool_id": "crm", "title": "CRM connector", "priority": "good_to_have", "status": "not_implemented"},
            ],
        )
        snapshot = backend.workspace_snapshot()
        self.assertEqual([t["tool_id"] for t in snapshot["tools"]], ["gmail", "crm"])  # must_have first
        self._run_turn(
            "enabled now",
            actions=[{"action": "upsert_tool", "tool_id": "gmail", "title": "Gmail", "priority": "must_have", "status": "enabled"}],
            task_id="task_2",
        )
        snapshot = backend.workspace_snapshot()
        self.assertEqual(next(t["status"] for t in snapshot["tools"] if t["tool_id"] == "gmail"), "enabled")
        backend.delete_tool_from_ui("crm")
        self.assertEqual([t["tool_id"] for t in backend.workspace_snapshot()["tools"]], ["gmail"])

    def test_artifact_interaction_queues_a_structured_human_event(self) -> None:
        self._run_turn(
            "build controls",
            actions=[
                {
                    "action": "create_artifact",
                    "artifact_id": "controls",
                    "title": "Controls",
                    "view": [
                        {"type": "toggle", "control_id": "auto_run", "label": "Auto-run", "value": False},
                        {"type": "field", "control_id": "owner_note", "label": "Owner note", "value": "Draft"},
                    ],
                }
            ],
        )
        result = backend.route_ui_request(
            "POST",
            "/interactions",
            {"artifact_id": "controls", "control_id": "owner_note", "value": "Ship Friday"},
        )
        self.assertFalse(result["steered"])
        snapshot = backend.workspace_snapshot()
        interaction = next(message for message in snapshot["messages"] if message["meta"] and message["meta"].get("action") == "artifact_interaction")
        self.assertEqual(interaction["meta"]["artifact_title"], "Controls")
        self.assertEqual(interaction["meta"]["value"], "Ship Friday")
        self.assertEqual(
            json.loads(interaction["content"]),
            {
                "type": "artifact_interaction",
                "artifact_id": "controls",
                "control_id": "owner_note",
                "control_type": "field",
                "value": "Ship Friday",
            },
        )
        self.host_responses["POST /v1/tasks"] = {
            "task_id": "task_2",
            "thread_id": "ws-1",
            "agent_runtime": "codex",
            "status": "queued",
        }
        backend._dispatch_pending_runs()
        _, _, task_body = self.host_calls[-1]
        self.assertIn('"type":"artifact_interaction"', task_body["input_message"])
        self.assertIn('"value":"Ship Friday"', task_body["input_message"])

    def test_artifact_interaction_validates_the_current_stored_view(self) -> None:
        self._run_turn(
            "build controls",
            actions=[
                {
                    "action": "create_artifact",
                    "artifact_id": "controls",
                    "title": "Controls",
                    "view": [
                        {"type": "button", "control_id": "approve", "label": "Approve"},
                        {"type": "toggle", "control_id": "auto_run", "label": "Auto-run", "value": False},
                        {"type": "field", "control_id": "owner_note", "label": "Owner note", "value": ""},
                    ],
                }
            ],
        )
        cases = [
            ({"artifact_id": "controls", "control_id": "missing", "value": True}, "current artifact view"),
            ({"artifact_id": "controls", "control_id": "approve", "value": "approve"}, "button value must be true"),
            ({"artifact_id": "controls", "control_id": "auto_run", "value": 1}, "toggle value must be true or false"),
            (
                {"artifact_id": "controls", "control_id": "owner_note", "value": "x" * (backend.FIELD_VALUE_LIMIT + 1)},
                "field value must be at most",
            ),
        ]
        for request, message in cases:
            with self.subTest(request=request), self.assertRaises(backend.AppError) as error:
                backend.submit_artifact_interaction(request)
            self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
            self.assertIn(message, error.exception.message)
        self.assertEqual([run for run in self._runs() if run["status"] != "done"], [])

    def test_artifact_interaction_steers_the_running_turn(self) -> None:
        backend.send_message(first_message("start"))
        self.host_responses["POST /v1/tasks"] = {
            "task_id": "task_1",
            "thread_id": "ws-1",
            "agent_runtime": "codex",
            "status": "queued",
        }
        backend._dispatch_pending_runs()
        self.agent_action(
            {
                "action": "create_artifact",
                "artifact_id": "controls",
                "title": "Controls",
                "view": [{"type": "button", "control_id": "approve", "label": "Approve"}],
            }
        )
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute("UPDATE runs SET host_status = 'running' WHERE task_id = 'task_1'")
        self.host_responses["POST /v1/tasks/task_1/steer"] = {"task_id": "task_1", "status": "running"}
        result = backend.submit_artifact_interaction(
            {"artifact_id": "controls", "control_id": "approve", "value": True}
        )
        self.assertTrue(result["steered"])
        steer = next(body for method, path, body in self.host_calls if path == "/v1/tasks/task_1/steer")
        self.assertEqual(
            json.loads(steer["steer_message"]),
            {
                "type": "artifact_interaction",
                "artifact_id": "controls",
                "control_id": "approve",
                "control_type": "button",
                "value": True,
            },
        )

    def test_message_while_running_is_steered(self) -> None:
        backend.send_message(first_message("start"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute("UPDATE runs SET host_status = 'running' WHERE task_id = 'task_1'")
        self.host_responses["POST /v1/tasks/task_1/steer"] = {"task_id": "task_1", "status": "running"}
        result = backend.send_message({"content": "also do this"})
        self.assertTrue(result["steered"])
        self.assertIn(("POST", "/v1/tasks/task_1/steer", {"steer_message": "also do this"}), self.host_calls)
        runs = self._runs()
        self.assertEqual([run["status"] for run in runs], ["active", "done"])
        self.assertTrue(any("Steered the message" in content for role, content in self._messages() if role == "event"))

    def test_pending_chat_backlog_prevents_later_message_from_steering_ahead(self) -> None:
        backend.send_message(first_message("start"))
        self.host_responses["POST /v1/tasks"] = {
            "task_id": "task_1",
            "thread_id": "ws-1",
            "agent_runtime": "codex",
            "status": "queued",
        }
        backend._dispatch_pending_runs()
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute("UPDATE runs SET host_status = 'running' WHERE task_id = 'task_1'")
        self.host_responses["POST /v1/tasks/task_1/steer"] = backend.AppError(
            HTTPStatus.CONFLICT, "steer raced task completion"
        )
        self.assertFalse(backend.send_message({"content": "older queued message"})["steered"])

        self.host_responses["POST /v1/tasks/task_1/steer"] = {
            "task_id": "task_1",
            "status": "running",
        }
        self.assertFalse(backend.send_message({"content": "newer message"})["steered"])
        steer_calls = [call for call in self.host_calls if call[1] == "/v1/tasks/task_1/steer"]
        self.assertEqual(len(steer_calls), 1)
        self.assertEqual([run["status"] for run in self._runs()], ["active", "pending", "pending"])

    def test_unknown_action_rejection_is_bounded_in_the_feed(self) -> None:
        backend.send_message(first_message("start"))
        self.host_responses["POST /v1/tasks"] = {
            "task_id": "task_1",
            "thread_id": "ws-1",
            "agent_runtime": "codex",
            "status": "queued",
        }
        backend._dispatch_pending_runs()
        action_name = "\U0001f680" * 10_000
        status, error = self.agent_action_error({"action": action_name})
        self.assertEqual(status, HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertLess(len(error.encode()), 1_000)
        rejection = next(content for role, content in self._messages() if role == "error")
        self.assertLess(len(rejection.encode()), 1_100)
        message = next(item for item in backend.workspace_snapshot()["messages"] if item["role"] == "error")
        self.assertLessEqual(len(message["meta"]["action"].encode()), backend.ACTION_LABEL_BYTES)

    def test_failed_steer_falls_back_to_a_queued_turn(self) -> None:
        backend.send_message(first_message("start"))
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_1", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_mission_pursuit")
            cur.execute("UPDATE runs SET host_status = 'running' WHERE task_id = 'task_1'")
        self.host_responses["POST /v1/tasks/task_1/steer"] = backend.AppError(
            HTTPStatus.CONFLICT, "only running tasks can be steered"
        )
        result = backend.send_message({"content": "missed the window"})
        self.assertFalse(result["steered"])
        self.assertEqual([run["status"] for run in self._runs()], ["active", "pending"])

if __name__ == "__main__":
    unittest.main()
