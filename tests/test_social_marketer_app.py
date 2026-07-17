"""Social Marketer app backend tests.

The pure-logic tests cover the domain action protocol: upsert_post and
set_post_status shape and byte-cap validation, routed through the shared
engine's validator. The database-backed tests run the app migrations on the
scratch cluster and exercise the posts table through the domain action apply
path, the operator composer routes, and the agent read route.
"""

from __future__ import annotations

from http import HTTPStatus
import importlib.util
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

import pg_harness

from host.apps.workspace_kit import engine
from host.runtime import app_migrate, app_platform, db, migrate

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_backend() -> Any:
    spec = importlib.util.spec_from_file_location(
        "social_marketer_backend", REPO_ROOT / "host" / "apps" / "social_marketer" / "backend.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sm = _load_backend()
engine.configure(sm.CONFIG)


class ManifestTests(unittest.TestCase):
    def test_manifest_derives_host_owned_names_and_slot(self) -> None:
        app = app_platform.app_by_id("social_marketer")
        assert app is not None
        self.assertEqual(app.allocation.port_offset, 3)
        self.assertEqual(app.db_schema, "app_social_marketer")
        self.assertEqual(app.db_role, "trustyclaw-app-social_marketer")
        self.assertEqual(app.port, app_platform.APP_PORT_BASE + 3)
        self.assertTrue(app.agent_api)
        self.assertIn("resident agent of Social Marketer", app.agent_instructions)
        # The agent contract must state the hard platform rules.
        self.assertIn("host-approved", app.agent_instructions)
        self.assertIn("TEXT ONLY", app.agent_instructions)
        self.assertNotIn("Instagram", app.agent_instructions)


class UpsertPostShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        engine.configure(sm.CONFIG)

    def _err(self, action: dict[str, Any]) -> str | None:
        return engine.validate_action_shape(action)

    def test_valid_upsert_post(self) -> None:
        self.assertIsNone(self._err({"action": "upsert_post", "id": "launch-x-1", "platform": "x", "body": "hello"}))
        self.assertIsNone(
            self._err(
                {
                    "action": "upsert_post",
                    "id": "launch-x-1",
                    "platform": "linkedin",
                    "body": "hello",
                    "scheduled_for": "2026-07-20T14:00:00Z",
                }
            )
        )
        self.assertIn(
            "platform must be one of",
            self._err({"action": "upsert_post", "id": "p1", "platform": "instagram", "body": "hi"}) or "",
        )

    def test_platform_enum(self) -> None:
        error = self._err({"action": "upsert_post", "id": "p1", "platform": "tiktok", "body": "x"})
        self.assertIn("platform must be one of", error or "")

    def test_body_byte_cap_is_measured_in_encoded_bytes(self) -> None:
        # "é" encodes to two UTF-8 bytes, so 2001 characters exceed X's
        # 4000-byte storage bound.
        error = self._err({"action": "upsert_post", "id": "p1", "platform": "x", "body": "é" * 2001})
        self.assertIn("4000 encoded bytes", error or "")
        self.assertIsNone(self._err({"action": "upsert_post", "id": "p1", "platform": "x", "body": "a" * 4000}))
        self.assertIn("4000 encoded bytes", self._err({"action": "upsert_post", "id": "p1", "platform": "x", "body": "a" * 4001}) or "")

    def test_empty_body_rejected(self) -> None:
        self.assertIn("non-empty", self._err({"action": "upsert_post", "id": "p1", "platform": "x", "body": "   "}) or "")

    def test_bad_id_slug(self) -> None:
        self.assertIn("id must match", self._err({"action": "upsert_post", "id": "Bad Id", "platform": "x", "body": "x"}) or "")

    def test_missing_and_unsupported_fields(self) -> None:
        self.assertIn("missing required field", self._err({"action": "upsert_post", "id": "p1", "platform": "x"}) or "")
        self.assertIn(
            "unsupported field",
            self._err({"action": "upsert_post", "id": "p1", "platform": "x", "body": "x", "status": "posted"}) or "",
        )

    def test_bad_scheduled_for(self) -> None:
        self.assertIn(
            "UTC timestamp",
            self._err({"action": "upsert_post", "id": "p1", "platform": "x", "body": "x", "scheduled_for": "tomorrow"}) or "",
        )

    def test_existing_draft_is_locked_before_update(self) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = ("draft",)
        self.assertIsNone(
            sm._apply_upsert_post(
                cur,
                {"id": "p1", "platform": "x", "body": "updated"},
                "2026-07-15T00:00:00Z",
            )
        )
        self.assertIn("FOR UPDATE", cur.execute.call_args_list[0].args[0])


class SetPostStatusShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        engine.configure(sm.CONFIG)

    def _err(self, action: dict[str, Any]) -> str | None:
        return engine.validate_action_shape(action)

    def test_valid(self) -> None:
        self.assertIsNone(self._err({"action": "set_post_status", "id": "p1", "status": "approved"}))
        self.assertIsNone(self._err({"action": "set_post_status", "id": "p1", "status": "posted", "external_ref": "12345"}))

    def test_status_enum(self) -> None:
        self.assertIn("status must be one of", self._err({"action": "set_post_status", "id": "p1", "status": "live"}) or "")

    def test_external_ref_byte_cap(self) -> None:
        self.assertIn(
            "200 encoded bytes",
            self._err({"action": "set_post_status", "id": "p1", "status": "posted", "external_ref": "é" * 101}) or "",
        )
        self.assertIn(
            "non-empty",
            self._err({"action": "set_post_status", "id": "p1", "status": "posted", "external_ref": "  "}) or "",
        )

    def test_unknown_action_lists_domain_actions(self) -> None:
        error = engine.validate_action_shape({"action": "publish_now"})
        self.assertIn("unknown action", error or "")
        self.assertIn("upsert_post", error or "")
        self.assertIn("set_post_status", error or "")


class SocialMarketerDbTests(unittest.TestCase):
    DB_NAME = "trustyclaw_social_marketer_test"
    _initialized = False

    def setUp(self) -> None:
        engine.configure(sm.CONFIG)
        pg_harness.ensure_database()
        if not SocialMarketerDbTests._initialized:
            pg_harness.create_database(self.DB_NAME)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_DB_NAME": self.DB_NAME})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(db.close_pool)
        if not SocialMarketerDbTests._initialized:
            migrate.up(quiet=True)
            with db.transaction() as cur:
                cur.execute(
                    """
                    DO $$
                    BEGIN
                      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'trustyclaw-app-social_marketer') THEN
                        CREATE ROLE "trustyclaw-app-social_marketer" LOGIN;
                      END IF;
                    END
                    $$;
                    """
                )
                cur.execute(
                    'CREATE SCHEMA IF NOT EXISTS app_social_marketer AUTHORIZATION "trustyclaw-app-social_marketer"'
                )
            app = app_platform.app_by_id("social_marketer")
            assert app is not None
            for version in app_migrate.pending(app.id):
                app_migrate.apply_sql(app.id, version, connection_user=app.db_role)
                app_migrate.record(app.id, version)
            SocialMarketerDbTests._initialized = True
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_social_marketer")
            cur.execute(
                "TRUNCATE workspace, messages, runs, schedules, artifacts, memories, tools, posts"
                " RESTART IDENTITY CASCADE"
            )

    def _apply(self, action: dict[str, Any]) -> str | None:
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_social_marketer")
            return engine._apply_action(cur, action, "2026-07-15T00:00:00Z")

    def _posts(self) -> list[dict[str, Any]]:
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_social_marketer")
            cur.execute("SELECT id, platform, body, status, scheduled_for, external_ref FROM posts ORDER BY id")
            return [
                {"id": r[0], "platform": r[1], "body": r[2], "status": r[3], "scheduled_for": r[4], "external_ref": r[5]}
                for r in cur.fetchall()
            ]

    def _events(self) -> list[str]:
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_social_marketer")
            cur.execute("SELECT content FROM messages WHERE role = 'event' ORDER BY id")
            return [r[0] for r in cur.fetchall()]

    def test_upsert_then_set_status_lifecycle(self) -> None:
        self.assertIsNone(self._apply({"action": "upsert_post", "id": "p1", "platform": "x", "body": "first"}))
        rows = self._posts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], {"id": "p1", "platform": "x", "body": "first", "status": "draft", "scheduled_for": None, "external_ref": None})

        # Approval binds the exact content; approved records are immutable.
        self.assertIsNone(self._apply({"action": "set_post_status", "id": "p1", "status": "approved"}))
        error = self._apply(
            {"action": "upsert_post", "id": "p1", "platform": "linkedin", "body": "second", "scheduled_for": "2026-07-20T14:00:00Z"}
        )
        self.assertIn("only drafts can be edited", error or "")
        row = self._posts()[0]
        self.assertEqual(row["platform"], "x")
        self.assertEqual(row["body"], "first")
        self.assertEqual(row["status"], "approved")
        self.assertIsNone(row["scheduled_for"])

        # set_post_status records the external ref on publish.
        self.assertIsNone(self._apply({"action": "set_post_status", "id": "p1", "status": "posted", "external_ref": "urn:li:9"}))
        row = self._posts()[0]
        self.assertEqual(row["status"], "posted")
        self.assertEqual(row["external_ref"], "urn:li:9")
        error = self._apply(
            {"action": "set_post_status", "id": "p1", "status": "posted", "external_ref": "changed"}
        )
        self.assertIn("posted records are immutable", error or "")
        self.assertEqual(self._posts()[0]["external_ref"], "urn:li:9")
        error = self._apply({"action": "set_post_status", "id": "p1", "status": "draft"})
        self.assertIn("posted records are immutable", error or "")
        self.assertEqual(self._posts()[0]["status"], "posted")

    def test_set_status_requires_existing_post(self) -> None:
        error = self._apply({"action": "set_post_status", "id": "ghost", "status": "posted"})
        self.assertIn("does not exist", error or "")

    def test_post_count_cap(self) -> None:
        with patch.object(sm, "MAX_POSTS", 2):
            self.assertIsNone(self._apply({"action": "upsert_post", "id": "p1", "platform": "x", "body": "a"}))
            self.assertIsNone(self._apply({"action": "upsert_post", "id": "p2", "platform": "x", "body": "b"}))
            error = self._apply({"action": "upsert_post", "id": "p3", "platform": "x", "body": "c"})
            self.assertIn("post limit reached (2)", error or "")
            # Replacing an existing post at the cap still works.
            self.assertIsNone(self._apply({"action": "upsert_post", "id": "p1", "platform": "x", "body": "a2"}))

    def test_operator_routes_read_write_delete(self) -> None:
        created = engine.route_ui_request("POST", "/api/posts", {"platform": "x", "body": "operator draft", "scheduled_for": ""})
        post_id = created["post_id"]
        self.assertTrue(post_id.startswith("post-"))

        listed = engine.route_ui_request("GET", "/api/posts", None)
        self.assertEqual(len(listed["posts"]), 1)
        self.assertEqual(listed["posts"][0]["status"], "draft")
        self.assertEqual(listed["posts"][0]["platform"], "x")

        one = engine.route_ui_request("GET", f"/api/posts/{post_id}", None)
        self.assertEqual(one["post"]["body"], "operator draft")

        deleted = engine.route_ui_request("DELETE", f"/api/posts/{post_id}", None)
        self.assertEqual(deleted["deleted"], post_id)
        self.assertEqual(engine.route_ui_request("GET", "/api/posts", None)["posts"], [])

    def test_operator_upsert_rejects_bad_platform(self) -> None:
        with self.assertRaises(engine.AppError) as ctx:
            engine.route_ui_request("POST", "/api/posts", {"platform": "myspace", "body": "x"})
        self.assertEqual(ctx.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_operator_cannot_rewrite_posted_content(self) -> None:
        self.assertIsNone(self._apply({"action": "upsert_post", "id": "keep", "platform": "x", "body": "orig"}))
        self.assertIsNone(self._apply({"action": "set_post_status", "id": "keep", "status": "posted", "external_ref": "9"}))
        with self.assertRaises(engine.AppError) as error:
            engine.route_ui_request("POST", "/api/posts", {"id": "keep", "platform": "x", "body": "edited by operator"})
        self.assertEqual(error.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertIn("only drafts can be edited", error.exception.message)
        row = self._posts()[0]
        self.assertEqual(row["body"], "orig")
        self.assertEqual(row["status"], "posted")
        self.assertEqual(row["external_ref"], "9")

        with self.assertRaises(engine.AppError) as delete_error:
            engine.route_ui_request("DELETE", "/api/posts/keep", None)
        self.assertEqual(delete_error.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("only drafts can be deleted", delete_error.exception.message)
        self.assertEqual(self._posts()[0]["id"], "keep")

    def test_list_clips_body_preview_and_single_read_is_full(self) -> None:
        long_body = "y" * 1000
        self.assertIsNone(self._apply({"action": "upsert_post", "id": "long", "platform": "x", "body": long_body}))
        listed = engine.route_ui_request("GET", "/api/posts", None)["posts"][0]
        self.assertTrue(listed["truncated"])
        self.assertLess(len(listed["body"]), len(long_body))
        self.assertEqual(listed["body_bytes"], 1000)
        full = engine.route_ui_request("GET", "/api/posts/long", None)["post"]
        self.assertFalse(full["truncated"])
        self.assertEqual(full["body"], long_body)

    def test_agent_posts_route_reads_shared_table(self) -> None:
        self.assertIsNone(self._apply({"action": "upsert_post", "id": "shared", "platform": "linkedin", "body": "launch note"}))
        result = sm._domain_agent_routes("GET", ["agent", "posts"], None, "task-1")
        assert result is not None
        self.assertEqual([post["id"] for post in result["posts"]], ["shared"])

    def test_agent_single_post_read_returns_full_body(self) -> None:
        # The agent must see the exact body before publishing; the list is
        # preview-clipped, so /agent/posts/<id> returns the full post.
        long_body = "z" * 1000
        self.assertIsNone(self._apply({"action": "upsert_post", "id": "long", "platform": "x", "body": long_body}))
        listed = sm._domain_agent_routes("GET", ["agent", "posts"], None, "task-1")
        assert listed is not None
        self.assertTrue(listed["posts"][0]["truncated"])
        full = sm._domain_agent_routes("GET", ["agent", "posts", "long"], None, "task-1")
        assert full is not None
        self.assertFalse(full["post"]["truncated"])
        self.assertEqual(full["post"]["body"], long_body)

    def test_seed_inventory_and_schedules(self) -> None:
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_social_marketer")
            cur.execute(
                "INSERT INTO workspace (singleton, created_at, updated_at) VALUES (TRUE, %s, %s)",
                ("2026-07-15T00:00:00Z", "2026-07-15T00:00:00Z"),
            )
            sm.seed(cur, "2026-07-15T00:00:00Z")
            cur.execute("SELECT tool_id, priority, status FROM tools ORDER BY tool_id")
            tools = cur.fetchall()
            cur.execute("SELECT goal, measurement FROM workspace WHERE singleton = TRUE")
            workspace = cur.fetchone()
            cur.execute("SELECT schedule_id, every_minutes FROM schedules WHERE enabled = TRUE ORDER BY schedule_id")
            schedules = cur.fetchall()
        tool_map = {t[0]: (t[1], t[2]) for t in tools}
        self.assertEqual(tool_map["twitter"], ("must_have", "implemented"))
        self.assertEqual(tool_map["brave_search"], ("must_have", "implemented"))
        self.assertEqual(set(tool_map), {"twitter", "linkedin", "brave_search", "linkedin_discovery"})
        self.assertEqual(workspace, (sm.DEFAULT_GOAL, sm.DEFAULT_MEASUREMENT))
        self.assertEqual(dict(schedules), {"campaign_planning": sm.WEEKLY_MINUTES, "engagement_check": sm.DAILY_MINUTES})


if __name__ == "__main__":
    unittest.main()
