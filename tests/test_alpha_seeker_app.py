"""Alpha Seeker app backend tests.

Alpha Seeker is a thin financial-research config over the shared workspace_kit engine.
The generic machinery (action protocol, run worker, view validation) is covered
by tests/test_workspace_kit.py and tests/test_mission_pursuit_app.py; these tests
cover what is specific to Alpha Seeker: its manifest and host-derived allocation, its
financial-research setup brief, its read-only tool contract, and the first-open
seed that records the three read-only market tools and the daily pre-market
brief schedule.
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
ALPHA_DIR = REPO_ROOT / "host" / "apps" / "alpha_seeker"


def _load_alpha_seeker_backend() -> Any:
    spec = importlib.util.spec_from_file_location("alpha_seeker_backend", ALPHA_DIR / "backend.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


alpha = _load_alpha_seeker_backend()

CLAUDE_SETTINGS = {"agent_runtime": "claude_code", "model": "opus", "effort": "high"}


class AlphaManifestTests(unittest.TestCase):
    def test_installed_alpha_owns_slot_two_and_the_agent_protocol(self) -> None:
        app = app_platform.app_by_id("alpha_seeker")
        self.assertIsNotNone(app)
        assert app is not None
        self.assertEqual(app.id, "alpha_seeker")
        self.assertEqual(app.db_schema, "app_alpha_seeker")
        self.assertEqual(app.linux_user, "trustyclaw-app-alpha_seeker")
        self.assertEqual(app.allocation.port_offset, 2)
        self.assertEqual(app.allocation.uid, app_platform.APP_UID_BASE + 2)
        self.assertEqual(app.allocation.gid, app_platform.APP_UID_BASE + 2)
        self.assertTrue(app.agent_api)
        self.assertIn("You are the resident agent of Alpha Seeker", app.agent_instructions)

    def test_agent_md_states_the_read_only_trading_boundary(self) -> None:
        agent_md = (ALPHA_DIR / "agent.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(agent_md.encode("utf-8")), 16 * 1024)
        lowered = agent_md.lower()
        self.assertIn("read-only", lowered)
        self.assertIn("no order-placement tool exists", lowered)
        self.assertIn("propose", lowered)
        self.assertIn("utc timestamp", lowered)
        # The exact tool actions the workspace wires must be named for the agent.
        for action in (
            "ibkr_get_positions",
            "ibkr_get_account_summary",
            "ibkr_get_trades",
            "polymarket_list_markets",
            "polymarket_search",
            "polymarket_get_market",
            "polymarket_get_order_book",
            "polymarket_price_history",
            "brave_search_search_web",
        ):
            self.assertIn(action, agent_md)


class AlphaConfigTests(unittest.TestCase):
    def test_setup_brief_uses_app_defaults_without_an_intake_interview(self) -> None:
        brief = alpha.CONFIG.setup_brief
        self.assertIsNot(brief, engine.GENERIC_SETUP_BRIEF)
        lowered = brief.lower()
        self.assertIn("mandate", lowered)
        self.assertIn("existing research mandate", lowered)
        self.assertNotIn("set_goal", lowered)
        self.assertNotIn("set_measurement", lowered)

    def test_seeded_tools_are_the_three_read_only_market_tools(self) -> None:
        seeded = {tool_id: (priority, status) for tool_id, _title, priority, status, _note in alpha.SEEDED_TOOLS}
        self.assertEqual(set(seeded), {"ibkr", "polymarket", "brave_search"})
        for tool_id, (priority, status) in seeded.items():
            with self.subTest(tool_id=tool_id):
                self.assertEqual(priority, "good_to_have" if tool_id == "brave_search" else "must_have")
                self.assertEqual(status, "implemented")

    def test_pre_market_brief_prompt_refreshes_positions_and_appends_research(self) -> None:
        prompt = alpha.BRIEF_PROMPT
        self.assertIn("ibkr_get_positions", prompt)
        self.assertIn("polymarket", prompt)
        self.assertIn("research", prompt)
        self.assertIn("Do not place", prompt)


class AlphaSeedDbTests(unittest.TestCase):
    DB_NAME = "trustyclaw_alpha_test"
    _initialized = False

    def setUp(self) -> None:
        engine.configure(alpha.CONFIG)
        pg_harness.ensure_database()
        if not AlphaSeedDbTests._initialized:
            pg_harness.create_database(self.DB_NAME)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_DB_NAME": self.DB_NAME})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(db.close_pool)
        if not AlphaSeedDbTests._initialized:
            migrate.up(quiet=True)
            with db.transaction() as cur:
                cur.execute(
                    """
                    DO $$
                    BEGIN
                      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'trustyclaw-app-alpha_seeker') THEN
                        CREATE ROLE "trustyclaw-app-alpha_seeker" LOGIN;
                      END IF;
                    END
                    $$;
                    """
                )
                cur.execute('CREATE SCHEMA IF NOT EXISTS app_alpha_seeker AUTHORIZATION "trustyclaw-app-alpha_seeker"')
            app = app_platform.app_by_id("alpha_seeker")
            assert app is not None
            for version in app_migrate.pending(app.id):
                app_migrate.apply_sql(app.id, version, connection_user=app.db_role)
                app_migrate.record(app.id, version)
            AlphaSeedDbTests._initialized = True
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_alpha_seeker")
            cur.execute(
                "TRUNCATE workspace, messages, runs, schedules, artifacts, memories, tools"
                " RESTART IDENTITY CASCADE"
            )
        # send_message only queues work; stub the host API so a stray call fails
        # loudly rather than hitting a socket.
        self.api_patch = patch.object(
            engine, "call_admin_api", side_effect=AssertionError("no host call expected during seeding")
        )
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)

    def _rows(self, sql: str) -> list[tuple[Any, ...]]:
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_alpha_seeker")
            cur.execute(sql)
            return cur.fetchall()

    def test_first_message_seeds_read_only_tools_and_the_pre_market_brief(self) -> None:
        engine.send_message({"content": "hello", **CLAUDE_SETTINGS})

        workspace = self._rows("SELECT goal, measurement FROM workspace WHERE singleton = TRUE")[0]
        self.assertEqual(workspace, (alpha.DEFAULT_GOAL, alpha.DEFAULT_MEASUREMENT))

        tools = {row[0]: (row[1], row[2], row[3]) for row in self._rows(
            "SELECT tool_id, priority, status, note FROM tools"
        )}
        self.assertEqual(set(tools), {"ibkr", "polymarket", "brave_search"})
        for tool_id, (priority, status, note) in tools.items():
            with self.subTest(tool_id=tool_id):
                self.assertEqual(priority, "good_to_have" if tool_id == "brave_search" else "must_have")
                self.assertEqual(status, "implemented")
                self.assertTrue(note)

        schedules = self._rows("SELECT schedule_id, every_minutes, enabled FROM schedules")
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0][0], "pre_market_brief")
        self.assertEqual(schedules[0][1], 24 * 60)
        # Enabled from activation: the activation screen disclosed the brief
        # before the operator opted in.
        self.assertTrue(schedules[0][2])

        # The seed journals its work into the operator feed as ordinary events.
        events = [row[0] for row in self._rows("SELECT content FROM messages WHERE role = 'event'")]
        self.assertTrue(any("read-only market tools" in text for text in events))
        self.assertTrue(any("pre-market brief" in text for text in events))

    def test_seed_runs_once_and_is_not_reapplied_on_later_messages(self) -> None:
        engine.send_message({"content": "hello", **CLAUDE_SETTINGS})
        engine.send_message({"content": "second turn"})

        seed_events = self._rows("SELECT content FROM messages WHERE role = 'event' AND content LIKE '%pre-market brief%'")
        self.assertEqual(len(seed_events), 1)
        self.assertEqual(len(self._rows("SELECT schedule_id FROM schedules")), 1)


if __name__ == "__main__":
    unittest.main()
