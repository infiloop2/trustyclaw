"""Software Builder app backend tests.

Software Builder is a thin ``workspace_kit`` config: the generic engine machinery is
covered by ``tests.test_mission_pursuit_app`` and ``tests.test_workspace_kit``.
These tests cover only what is specific to Software Builder: its manifest slot and
derivations, its byte-identical workspace base migration, the app-authoring
setup brief, the first-open seed, and the pull-request product contract.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

import pg_harness

from host.apps.workspace_kit import engine
from host.runtime.deploy import app_migrate, migrate
from host.runtime.core import app_platform, db

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER_DIR = REPO_ROOT / "host" / "apps" / "software_builder"


def _load_software_builder_backend() -> Any:
    spec = importlib.util.spec_from_file_location("software_builder_backend", BUILDER_DIR / "backend.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load_software_builder_backend()

FIRST_MESSAGE = {"content": "fix the parser", "agent_runtime": "codex", "model": "gpt-5.6-terra", "effort": "high"}


class ManifestTests(unittest.TestCase):
    def test_builder_installs_on_slot_five_with_agent_api(self) -> None:
        app = app_platform.app_by_id("software_builder")
        assert app is not None
        self.assertEqual(app.allocation.port_offset, 5)
        self.assertTrue(app.agent_api)
        self.assertEqual(app.db_schema, "app_software_builder")
        self.assertEqual(app.db_role, "trustyclaw-app-software_builder")
        self.assertEqual(app.port, builder.CONFIG.port)

    def test_config_targets_builder_schema(self) -> None:
        self.assertEqual(builder.CONFIG.app_id, "software_builder")
        self.assertEqual(builder.CONFIG.db_schema, "app_software_builder")
        self.assertIsNone(builder.CONFIG.domain_ui_routes)
        self.assertIsNone(builder.CONFIG.domain_agent_routes)
        self.assertEqual(dict(builder.CONFIG.domain_actions), {})


class WorkspaceBaseMigrationTests(unittest.TestCase):
    def test_first_migration_is_byte_identical_to_sibling_kit_apps(self) -> None:
        # There is no canonical kit schema file; the embedded copies are the
        # applied history, and test_workspace_kit pins them to each other.
        sibling = (REPO_ROOT / "host" / "apps" / "alpha_seeker" / "migrations" / "0001_workspace_base.sql").read_bytes()
        mine = (BUILDER_DIR / "migrations" / "0001_workspace_base.sql").read_bytes()
        self.assertEqual(mine, sibling)

    def test_builder_ships_no_domain_migration(self) -> None:
        # Software Builder represents pull requests as generic artifacts. It has
        # no domain tables, so its migrations are the copied workspace base and
        # the kit-wide runtime-constraint update.
        migrations = sorted(p.name for p in (BUILDER_DIR / "migrations").glob("*.sql"))
        self.assertEqual(
            migrations,
            ["0001_workspace_base.sql", "0002_pi_hermes_runtimes.sql", "0003_remove_pi_runtime.sql"],
        )


class AgentInstructionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (BUILDER_DIR / "agent.md").read_text()

    def test_agent_md_fits_the_platform_byte_cap(self) -> None:
        self.assertLessEqual(len((BUILDER_DIR / "agent.md").read_bytes()), app_platform.MAX_AGENT_INSTRUCTIONS_BYTES)

    def test_agent_md_defines_the_pull_request_lifecycle(self) -> None:
        self.assertIn("pull request", self.text.lower())
        self.assertIn("review comment", self.text.lower())
        self.assertIn("checks", self.text.lower())
        self.assertIn("operator merges", self.text.lower())

    def test_agent_md_uses_one_artifact_per_pull_request(self) -> None:
        self.assertIn("one artifact per pull request", self.text.lower())
        self.assertIn("repository", self.text.lower())
        self.assertIn("branch", self.text.lower())


class SetupBriefTests(unittest.TestCase):
    def test_setup_brief_starts_from_a_repository_request_and_is_bounded(self) -> None:
        brief = builder.CONFIG.setup_brief
        self.assertTrue(brief.strip())
        self.assertIn("pull request", brief.lower())
        self.assertIn("repository", brief.lower())
        # It must fit alongside a full digest and message within the host input
        # limit, so keep it comfortably under the engine budget.
        self.assertLess(len(brief), engine.HOST_INPUT_LIMIT // 4)


class BuilderSeedDbTests(unittest.TestCase):
    DB_NAME = "trustyclaw_builder_test"
    _initialized = False

    def setUp(self) -> None:
        engine.configure(builder.CONFIG)
        pg_harness.ensure_database()
        if not BuilderSeedDbTests._initialized:
            pg_harness.create_database(self.DB_NAME)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_DB_NAME": self.DB_NAME})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(db.close_pool)
        if not BuilderSeedDbTests._initialized:
            migrate.up(quiet=True)
            with db.transaction() as cur:
                cur.execute(
                    """
                    DO $$
                    BEGIN
                      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'trustyclaw-app-software_builder') THEN
                        CREATE ROLE "trustyclaw-app-software_builder" LOGIN;
                      END IF;
                    END
                    $$;
                    """
                )
                cur.execute('CREATE SCHEMA IF NOT EXISTS app_software_builder AUTHORIZATION "trustyclaw-app-software_builder"')
            app = app_platform.app_by_id("software_builder")
            assert app is not None
            for version in app_migrate.pending(app.id):
                app_migrate.apply_sql(app.id, version, connection_user=app.db_role)
                app_migrate.record(app.id, version)
            BuilderSeedDbTests._initialized = True
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_software_builder")
            cur.execute(
                "TRUNCATE workspace, messages, runs, schedules, artifacts, memories, tools"
                " RESTART IDENTITY CASCADE"
            )
        engine._DISPATCH_ATTEMPTS.clear()
        engine._AGENT_ACTIONS_BY_TASK.clear()

    def test_first_open_records_brave_search_and_seeds_no_schedule(self) -> None:
        engine.send_message(FIRST_MESSAGE)
        snapshot = engine.workspace_snapshot()
        self.assertEqual(snapshot["workspace"]["goal"], builder.DEFAULT_GOAL)
        self.assertEqual(snapshot["workspace"]["measurement"], builder.DEFAULT_MEASUREMENT)
        tools = snapshot["tools"]
        self.assertEqual([tool["tool_id"] for tool in tools], ["brave_search"])
        brave = tools[0]
        self.assertEqual((brave["priority"], brave["status"]), ("good_to_have", "implemented"))
        self.assertIn("brave_search_search_web", brave["note"])
        # Software Builder is interactive: nothing is scheduled on first open.
        self.assertEqual(snapshot["schedules"], [])
        # The seed journals its own event into the feed.
        self.assertTrue(
            any(role == "event" and "Brave Search" in content for role, content in self._messages())
        )

    def _messages(self) -> list[tuple[str, str]]:
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_software_builder")
            cur.execute("SELECT role, content FROM messages ORDER BY id ASC")
            return cur.fetchall()


if __name__ == "__main__":
    unittest.main()
