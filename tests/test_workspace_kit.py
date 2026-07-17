"""workspace_kit engine tests.

These cover the generic machinery independent of any one app: the declarative
view-block validator, the generic agent-action shape validators, the fact that
``workspace_kit`` is an importable package and not an installed app, and the
contract that every installed workspace_kit app carries a first migration
creating the shared base schema.
"""

from __future__ import annotations

from pathlib import Path
import json
from typing import Any
import unittest

from host.apps.workspace_kit import engine, views
from host.runtime import app_platform

REPO_ROOT = Path(__file__).resolve().parents[1]
APPS_ROOT = REPO_ROOT / "host" / "apps"
KIT_ROOT = APPS_ROOT / "workspace_kit"


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
        self.assertIsNone(views.validate_view(view))
        # The engine re-exports the same validator it uses for agent actions.
        self.assertIs(engine.validate_view, views.validate_view)

    def test_unknown_block_type_names_allowed_types(self) -> None:
        error = views.validate_view([{"type": "iframe", "src": "https://evil"}])
        self.assertIn("unknown block type", error or "")
        self.assertIn("chart", error or "")

    def test_block_count_cap(self) -> None:
        error = views.validate_view([{"type": "divider"}] * (views.MAX_VIEW_BLOCKS + 1))
        self.assertIn("at most", error or "")

    def test_serialized_view_cap(self) -> None:
        view = [{"type": "text", "text": "x" * 4000}] * 5
        self.assertIn("characters when serialized", views.validate_view(view) or "")

    def test_chart_rejects_bad_points(self) -> None:
        self.assertIn(
            "2 to 60 points",
            views.validate_view([{"type": "chart", "kind": "bar", "points": [{"label": "a", "value": 1}]}]) or "",
        )
        self.assertIn(
            "finite numbers",
            views.validate_view(
                [{"type": "chart", "kind": "line", "points": [{"label": "a", "value": 1}, {"label": "b", "value": True}]}]
            )
            or "",
        )
        self.assertIn(
            "kind must be",
            views.validate_view([{"type": "chart", "kind": "pie", "points": [{"label": "a", "value": 1}, {"label": "b", "value": 2}]}]) or "",
        )

    def test_table_row_shape_must_match_columns(self) -> None:
        error = views.validate_view([{"type": "table", "columns": ["A"], "rows": [["1", "2"]]}])
        self.assertIn("match the number of columns", error or "")

    def test_progress_bounds(self) -> None:
        self.assertIn("between 0 and 100", views.validate_view([{"type": "progress", "value": 120}]) or "")

    def test_huge_integers_are_rejected_without_overflow(self) -> None:
        huge = 10 ** 10_000
        self.assertIn(
            "between 0 and 100",
            views.validate_view([{"type": "progress", "value": huge}]) or "",
        )
        self.assertIn(
            "finite numbers",
            views.validate_view([
                {"type": "chart", "kind": "bar", "points": [
                    {"label": "a", "value": huge}, {"label": "b", "value": 1}
                ]}
            ]) or "",
        )


class BoundHelperTests(unittest.TestCase):
    def test_snapshot_meta_clips_interaction_value(self) -> None:
        meta = engine._snapshot_message_meta(json.dumps({
            "action": "artifact_interaction", "value": "é" * 1000
        }))
        assert meta is not None
        self.assertLessEqual(len(meta["value"].encode("utf-8")), engine.SNAPSHOT_META_VALUE_BYTES)
        self.assertTrue(meta["value_truncated"])

    def test_snapshot_meta_survives_corrupt_rows(self) -> None:
        self.assertIsNone(engine._snapshot_message_meta("{not json"))
        self.assertIsNone(engine._snapshot_message_meta("[1, 2]"))

    def test_snapshot_meta_bounds_the_whole_encoded_object(self) -> None:
        # String leaves are clipped per field, but non-string leaves and key
        # count also grow with domain-hook input; the whole object must fit.
        oversized = engine._snapshot_message_meta(json.dumps({
            "action": "domain_write", "points": list(range(3000))
        }))
        self.assertEqual(oversized, {"action": "domain_write"})
        anonymous = engine._snapshot_message_meta(json.dumps({
            "points": list(range(3000))
        }))
        self.assertEqual(anonymous, {"action": "unknown"})

    def test_snapshot_display_text_is_bounded_after_json_escaping(self) -> None:
        rendered = engine._snapshot_text("😀" * 1000, engine.SNAPSHOT_TITLE_BYTES)
        encoded_bytes = len(json.dumps(rendered).encode()) - 2
        self.assertLessEqual(encoded_bytes, engine.SNAPSHOT_TITLE_BYTES)
        self.assertTrue(rendered.endswith("…"))

    def test_snapshot_maximum_rows_fit_the_app_proxy_response_cap(self) -> None:
        emoji = "😀" * 1000
        message, _ = engine.clip_encoded_text(emoji, engine.SNAPSHOT_MESSAGE_BYTES)
        meta = engine._snapshot_message_meta(json.dumps({
            "value": emoji,
            **{f"field_{index}": emoji for index in range(8)},
        }))
        title = engine._snapshot_text(emoji, engine.SNAPSHOT_TITLE_BYTES)
        snapshot = {
            "workspace": {
                "goal": engine._snapshot_text(emoji, engine.SNAPSHOT_GOAL_BYTES),
                "measurement": engine._snapshot_text(emoji, engine.SNAPSHOT_GOAL_BYTES),
            },
            "messages": [
                {"id": index, "role": "agent", "content": message, "meta": meta}
                for index in range(engine.FEED_MESSAGE_LIMIT)
            ],
            "busy": [
                {"run_id": index, "last_error": engine._snapshot_text(emoji, engine.SNAPSHOT_ERROR_BYTES)}
                for index in range(engine.MAX_QUEUED_CHAT_TURNS + 1)
            ],
            "schedules": [{"schedule_id": f"s{index}", "title": title} for index in range(engine.MAX_SCHEDULES)],
            "artifacts": [{"artifact_id": f"a{index}", "title": title} for index in range(engine.MAX_ARTIFACTS)],
            "memories": [
                {"memory_id": f"m{index}", "content": engine._snapshot_text(emoji, engine.SNAPSHOT_MEMORY_BYTES)}
                for index in range(engine.MAX_MEMORIES)
            ],
            "tools": [
                {
                    "tool_id": f"t{index}",
                    "title": title,
                    "note": engine._snapshot_text(emoji, engine.SNAPSHOT_TOOL_NOTE_BYTES),
                }
                for index in range(engine.MAX_TOOLS)
            ],
        }
        self.assertLess(len(json.dumps(snapshot).encode()), 900 * 1024)

    def test_validation_error_and_domain_digest_are_bounded(self) -> None:
        error = engine._bounded_action_error("é" * 5000)
        self.assertLessEqual(len(error.encode("utf-8")), engine.ACTION_ERROR_BYTES)
        rendered = engine._render_digest_sections(
            [("Domain", ["- " + "x" * 100, "- never reached"])], budget=30
        )
        self.assertEqual(rendered, "Domain")

    def test_terminal_cache_cleanup_removes_both_indexes(self) -> None:
        engine._DISPATCH_ATTEMPTS[42] = 3
        engine._AGENT_ACTIONS_BY_TASK["task_42"] = 7
        engine._forget_run_caches(42, "task_42")
        self.assertNotIn(42, engine._DISPATCH_ATTEMPTS)
        self.assertNotIn("task_42", engine._AGENT_ACTIONS_BY_TASK)

    def test_interactive_blocks_are_typed_and_control_ids_are_unique(self) -> None:
        self.assertIn(
            "button tone",
            views.validate_view([{"type": "button", "control_id": "go", "label": "Go", "tone": "script"}]) or "",
        )
        self.assertIn(
            "control_id must match",
            views.validate_view([{"type": "button", "control_id": "Bad Slug", "label": "Go"}]) or "",
        )
        self.assertIn(
            "at most 1000",
            views.validate_view(
                [{"type": "field", "control_id": "note", "label": "Note", "value": "x" * (views.FIELD_VALUE_LIMIT + 1)}]
            )
            or "",
        )
        self.assertIn(
            "duplicate control_id",
            views.validate_view(
                [
                    {"type": "button", "control_id": "same", "label": "First"},
                    {"type": "toggle", "control_id": "same", "label": "Second", "value": False},
                ]
            )
            or "",
        )


class ActionShapeTests(unittest.TestCase):
    def test_unknown_action_lists_allowed_actions(self) -> None:
        error = engine.validate_action_shape({"action": "launch_rocket"})
        self.assertIn("unknown action", error or "")
        self.assertIn("create_artifact", error or "")

    def test_set_goal_and_measurement(self) -> None:
        self.assertIsNone(engine.validate_action_shape({"action": "set_goal", "goal": "x"}))
        self.assertIsNone(engine.validate_action_shape({"action": "set_goal", "goal": ""}))
        self.assertIn("goal must be at most", engine.validate_action_shape({"action": "set_goal", "goal": "x" * (engine.GOAL_LIMIT + 1)}) or "")
        self.assertIn("unsupported field", engine.validate_action_shape({"action": "set_goal", "goal": "x", "bonus": 1}) or "")
        self.assertIsNone(engine.validate_action_shape({"action": "set_measurement", "measurement": "installs"}))

    def test_remember_forget_and_tools(self) -> None:
        self.assertIsNone(engine.validate_action_shape({"action": "remember", "memory_id": "boss_tz", "content": "UTC+2"}))
        self.assertIsNone(engine.validate_action_shape({"action": "forget", "memory_id": "boss_tz"}))
        self.assertIn(
            "memory_id must match",
            engine.validate_action_shape({"action": "remember", "memory_id": "Bad Slug", "content": "x"}) or "",
        )
        self.assertIsNone(
            engine.validate_action_shape(
                {"action": "upsert_tool", "tool_id": "gmail", "title": "Gmail", "priority": "must_have", "status": "implemented", "note": "ask to enable"}
            )
        )
        self.assertIn(
            "priority must be one of",
            engine.validate_action_shape({"action": "upsert_tool", "tool_id": "gmail", "title": "Gmail", "priority": "critical", "status": "enabled"}) or "",
        )
        self.assertIn(
            "status must be one of",
            engine.validate_action_shape({"action": "upsert_tool", "tool_id": "gmail", "title": "Gmail", "priority": "must_have", "status": "on"}) or "",
        )

    def test_create_and_update_artifact(self) -> None:
        good = {"action": "create_artifact", "artifact_id": "notes", "title": "Notes", "data": {"a": 1}}
        self.assertIsNone(engine.validate_action_shape(good))
        self.assertIn("artifact_id must match", engine.validate_action_shape({**good, "artifact_id": "Bad Slug"}) or "")
        self.assertIn("data must be at most", engine.validate_action_shape({**good, "data": "x" * (engine.DATA_LIMIT + 1)}) or "")
        self.assertIn("finite numbers", engine.validate_action_shape({**good, "data": {"x": float("nan")}}) or "")
        nested: Any = []
        for _ in range(100_000):
            nested = [nested]
        self.assertIn("JSON-serializable", engine.validate_action_shape({**good, "data": nested}) or "")
        self.assertIn(
            "needs at least one of",
            engine.validate_action_shape({"action": "update_artifact", "artifact_id": "a"}) or "",
        )
        self.assertIsNone(engine.validate_action_shape({"action": "update_artifact", "artifact_id": "a", "view": None}))
        self.assertIn(
            "view must be a list",
            engine.validate_action_shape({"action": "create_artifact", "artifact_id": "a", "title": "t", "view": None}) or "",
        )

    def test_schedule_cadence(self) -> None:
        base = {"action": "create_schedule", "schedule_id": "brief", "title": "Brief", "prompt": "do it"}
        self.assertIsNone(engine.validate_action_shape({**base, "every_minutes": 30}))
        self.assertIsNone(engine.validate_action_shape({**base, "at": "2027-01-01T09:00:00Z"}))
        self.assertIn("needs every_minutes", engine.validate_action_shape(base) or "")
        self.assertIn("not both", engine.validate_action_shape({**base, "every_minutes": 30, "at": "2027-01-01T09:00:00Z"}) or "")
        self.assertIn("must be between", engine.validate_action_shape({**base, "every_minutes": 1}) or "")
        self.assertIn("must be an integer", engine.validate_action_shape({**base, "every_minutes": True}) or "")
        self.assertIn("UTC timestamp", engine.validate_action_shape({**base, "at": "tomorrow"}) or "")
        self.assertIn(
            "needs at least one of",
            engine.validate_action_shape({"action": "update_schedule", "schedule_id": "a"}) or "",
        )
        self.assertIn(
            "enabled must be",
            engine.validate_action_shape({"action": "update_schedule", "schedule_id": "a", "enabled": "yes"}) or "",
        )


class PackageIsNotAnInstalledApp(unittest.TestCase):
    def test_workspace_kit_has_no_manifest_and_is_not_installed(self) -> None:
        self.assertFalse((KIT_ROOT / "manifest.json").exists())
        ids = {app.id for app in app_platform.installed_apps()}
        self.assertNotIn("workspace_kit", ids)
        # The reference app that sits on the kit still validates.
        self.assertIn("mission_pursuit", ids)


class ConnectionStatusTests(unittest.TestCase):
    """The overall connections pill folds per-tool states: any must-have off
    or missing blocks the app, anything else short of all-ready degrades."""

    @staticmethod
    def tool(priority: str, state: str) -> dict[str, Any]:
        return {"tool_id": "t", "title": "T", "priority": priority, "state": state, "detail": ""}

    def test_all_ready_is_ready(self) -> None:
        tools = [self.tool("must_have", "ready"), self.tool("good_to_have", "ready")]
        self.assertEqual(engine._overall_connection_status(tools), "ready")

    def test_empty_inventory_is_ready(self) -> None:
        self.assertEqual(engine._overall_connection_status([]), "ready")

    def test_must_have_off_or_missing_blocks(self) -> None:
        for state in ("off", "missing"):
            with self.subTest(state=state):
                tools = [self.tool("must_have", state), self.tool("good_to_have", "ready")]
                self.assertEqual(engine._overall_connection_status(tools), "blocked")

    def test_missing_credentials_or_optional_gaps_degrade(self) -> None:
        self.assertEqual(
            engine._overall_connection_status([self.tool("must_have", "unconfigured")]), "degraded"
        )
        self.assertEqual(
            engine._overall_connection_status(
                [self.tool("must_have", "ready"), self.tool("good_to_have", "off")]
            ),
            "degraded",
        )

    def test_network_policy_parser_matches_the_host_response_shape(self) -> None:
        # The policy envelope keys have changed under the kit before; parse
        # the host module's real fail-closed default and a synthetic stored
        # policy so a future key rename fails here instead of silently
        # reporting every provider as disabled.
        from unittest.mock import patch

        from host.runtime import network_policy

        with patch.object(network_policy, "network_policy_record", return_value=None):
            fail_closed = network_policy.network_policy_response()
        self.assertEqual(engine.parse_network_integrations(fail_closed), {})
        payload = {
            "network_controls": {
                "network_integrations": {"openai": {"enabled": True}, "claude": {"enabled": False}}
            }
        }
        self.assertEqual(
            engine.parse_network_integrations(payload), {"openai": True, "claude": False}
        )

    def test_runtime_providers_mirror_the_orchestrator_mapping(self) -> None:
        # The engine cannot import the host orchestrator at runtime, so its
        # copy of the runtime-to-integration mapping must be pinned here: a
        # wrong integration id silently reports every provider as disabled.
        from host.runtime import orchestrator

        self.assertEqual(dict(engine.RUNTIME_PROVIDERS), orchestrator._MANAGED_PROVIDER_BY_RUNTIME)

    def test_no_enabled_provider_blocks_regardless_of_tools(self) -> None:
        providers = [
            {"provider": "openai", "agent_runtime": "codex", "enabled": False},
            {"provider": "claude", "agent_runtime": "claude_code", "enabled": False},
        ]
        self.assertEqual(
            engine._overall_connection_status([self.tool("must_have", "ready")], providers),
            "blocked",
        )
        self.assertEqual(engine._overall_connection_status([], providers), "blocked")

    def test_one_enabled_provider_defers_to_tool_states(self) -> None:
        providers = [
            {"provider": "openai", "agent_runtime": "codex", "enabled": True},
            {"provider": "claude", "agent_runtime": "claude_code", "enabled": False},
        ]
        self.assertEqual(
            engine._overall_connection_status([self.tool("must_have", "ready")], providers),
            "ready",
        )


class WorkspaceKitMigrationCopiesAreEquivalent(unittest.TestCase):
    """Every kit app's first migration creates the same base schema. There is
    deliberately no canonical schema file in the kit: the apps' migrations are
    applied history, so the suite asserts them against each other and against
    the tables the engine needs.

    Mission Pursuit predates the kit extraction: its shipped first migration is
    ``0001_mission_pursuit.sql`` (applied history on live hosts, immutable), and
    it matches the other apps' ``0001_workspace_base.sql`` in everything but its
    header comments.
    """

    BASE_TABLES = ("workspace", "messages", "runs", "schedules", "artifacts", "memories", "tools")

    def _kit_app_dirs(self) -> list[Path]:
        apps: list[Path] = []
        for child in APPS_ROOT.iterdir():
            index = child / "ui" / "index.html"
            if (
                child.is_dir()
                and (child / "manifest.json").is_file()
                and index.is_file()
                and "/workspace-kit/view_blocks.js" in index.read_text()
            ):
                apps.append(child)
        return sorted(apps)

    def test_at_least_mission_pursuit_is_checked(self) -> None:
        names = {path.name for path in self._kit_app_dirs()}
        self.assertIn("mission_pursuit", names)

    @staticmethod
    def _first_migration(app_dir: Path) -> Path:
        migrations = sorted((app_dir / "migrations").glob("*.sql"))
        assert migrations, f"{app_dir.name} has no migrations"
        return migrations[0]

    @staticmethod
    def _executable_sql(path: Path) -> str:
        lines = [
            line.rstrip()
            for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("--")
        ]
        return "\n".join(lines)

    def test_first_migrations_share_identical_executable_sql(self) -> None:
        apps = self._kit_app_dirs()
        reference_dir = apps[0]
        reference = self._executable_sql(self._first_migration(reference_dir))
        for app_dir in apps[1:]:
            with self.subTest(app=app_dir.name):
                self.assertEqual(
                    self._executable_sql(self._first_migration(app_dir)),
                    reference,
                    f"{app_dir.name}'s first migration must create the same base schema"
                    f" as {reference_dir.name}'s (comments aside)",
                )

    def test_first_migration_names_are_the_expected_applied_history(self) -> None:
        for app_dir in self._kit_app_dirs():
            expected = (
                "0001_mission_pursuit.sql"
                if app_dir.name == "mission_pursuit"
                else "0001_workspace_base.sql"
            )
            with self.subTest(app=app_dir.name):
                self.assertEqual(self._first_migration(app_dir).name, expected)

    def test_first_migration_creates_every_base_table(self) -> None:
        sql = self._executable_sql(self._first_migration(self._kit_app_dirs()[0]))
        for table in self.BASE_TABLES:
            with self.subTest(table=table):
                self.assertIn(f"CREATE TABLE IF NOT EXISTS {table} (", sql)


if __name__ == "__main__":
    unittest.main()
