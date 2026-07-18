"""Tests for the SQL migration runner (host.runtime.deploy.migrate).

These run against a dedicated database on the scratch cluster so migrating
down never disturbs the schema the other tests share.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pg_harness

from host.runtime.deploy import app_migrate, migrate
from host.runtime.core import app_platform, db


def _write(directory: Path, name: str, up: str, down: str = "") -> None:
    (directory / name).write_text(f"-- migrate:up\n{up}\n\n-- migrate:down\n{down}\n")


def _app_up(app_id: str) -> list[int]:
    """The production app-migration loop (bootstrap shells pending, then
    apply-sql as the app role and record as admin per version), driven
    in-process for tests. No advisory lock: tests are single-process."""
    app = app_platform.app_by_id(app_id)
    assert app is not None
    applied = []
    for version in app_migrate.pending(app_id):
        app_migrate.apply_sql(app_id, version, connection_user=app.db_role)
        app_migrate.record(app_id, version)
        applied.append(version)
    return applied


class MigrateRunnerTests(unittest.TestCase):
    DB_NAME = "trustyclaw_migrate_test"

    def setUp(self) -> None:
        pg_harness.create_database(self.DB_NAME)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_DB_NAME": self.DB_NAME})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        # Close pooled connections to this class's database before the env
        # restore, so no later test checks one out against the wrong database.
        self.addCleanup(db.close_pool)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.migrations = Path(self.temp_dir.name)

    def table_names(self) -> set[str]:
        with db.transaction() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            return {row[0] for row in cur.fetchall()}

    def test_up_applies_pending_migrations_in_order_and_records_them(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);", "DROP TABLE first;")
        _write(
            self.migrations,
            "0002_second.sql",
            "CREATE TABLE second (first_like INT); INSERT INTO second SELECT 1 FROM first;",
            "DROP TABLE second;",
        )

        applied = migrate.up(directory=self.migrations, quiet=True)

        self.assertEqual(applied, [1, 2])
        self.assertLessEqual({"first", "second", "schema_migrations"}, self.table_names())
        status = migrate.status(directory=self.migrations)
        self.assertEqual(status, [(1, "first", True), (2, "second", True)])

    def test_up_is_idempotent_and_applies_only_new_versions(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);", "DROP TABLE first;")
        self.assertEqual(migrate.up(directory=self.migrations, quiet=True), [1])
        self.assertEqual(migrate.up(directory=self.migrations, quiet=True), [])
        _write(self.migrations, "0002_second.sql", "CREATE TABLE second (id INT);", "DROP TABLE second;")
        self.assertEqual(migrate.up(directory=self.migrations, quiet=True), [2])

    def test_a_failing_migration_rolls_back_and_leaves_the_previous_version(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);", "DROP TABLE first;")
        migrate.up(directory=self.migrations, quiet=True)
        _write(self.migrations, "0002_broken.sql", "CREATE TABLE second (id INT); SELECT no_such_column;")

        with self.assertRaises(Exception):
            migrate.up(directory=self.migrations, quiet=True)

        self.assertNotIn("second", self.table_names())
        self.assertEqual(migrate.status(directory=self.migrations)[0], (1, "first", True))

    def test_down_reverts_the_newest_and_to_reverts_everything_above_the_target(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);", "DROP TABLE first;")
        _write(self.migrations, "0002_second.sql", "CREATE TABLE second (id INT);", "DROP TABLE second;")
        _write(self.migrations, "0003_third.sql", "CREATE TABLE third (id INT);", "DROP TABLE third;")
        migrate.up(directory=self.migrations, quiet=True)

        self.assertEqual(migrate.down(directory=self.migrations, quiet=True), [3])
        self.assertNotIn("third", self.table_names())
        self.assertEqual(migrate.down(target=0, directory=self.migrations, quiet=True), [2, 1])
        self.assertNotIn("first", self.table_names())
        self.assertNotIn("second", self.table_names())
        self.assertEqual(
            migrate.status(directory=self.migrations),
            [(1, "first", False), (2, "second", False), (3, "third", False)],
        )

    def test_down_refuses_a_version_with_no_file_or_empty_down_section(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);")
        migrate.up(directory=self.migrations, quiet=True)
        with self.assertRaises(migrate.MigrationError):
            migrate.down(directory=self.migrations, quiet=True)

    def test_malformed_migration_files_are_rejected(self) -> None:
        (self.migrations / "0001_missing_markers.sql").write_text("CREATE TABLE first (id INT);")
        with self.assertRaises(migrate.MigrationError):
            migrate.load_migrations(self.migrations)
        (self.migrations / "0001_missing_markers.sql").unlink()

        (self.migrations / "not_versioned.sql").write_text("-- migrate:up\nSELECT 1;\n-- migrate:down\n")
        with self.assertRaises(migrate.MigrationError):
            migrate.load_migrations(self.migrations)

    def test_repo_migrations_apply_and_roll_back_cleanly(self) -> None:
        # The real migration history must always migrate a fresh database up
        # and back down; this is the guardrail for every future migration.
        applied = migrate.up(quiet=True)
        self.assertGreaterEqual(len(applied), 1)
        self.assertIn("tasks", self.table_names())
        reverted = migrate.down(target=0, quiet=True)
        self.assertEqual(reverted, list(reversed(applied)))
        self.assertEqual(self.table_names(), {"schema_migrations"})



class RepoMigrationDataTests(unittest.TestCase):
    """Data-migration behavior of the real repo migrations."""

    DB_NAME = "trustyclaw_migrate_repo_test"

    def setUp(self) -> None:
        pg_harness.create_database(self.DB_NAME)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_DB_NAME": self.DB_NAME})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        # Close pooled connections to this class's database before the env
        # restore, so no later test checks one out against the wrong database.
        self.addCleanup(db.close_pool)
        self.repo_migrations = Path(__file__).resolve().parents[1] / "host" / "migrations"

    def test_0002_migrates_legacy_preset_policy_rows(self) -> None:
        # A host upgraded from the raw-preset era: managed provider rows plus
        # raw GitHub/PyPI/npm domain rules in the stored policy.
        migrate.up(target=1, directory=self.repo_migrations, quiet=True)
        with db.transaction() as cur:
            cur.execute("INSERT INTO network_policy (singleton, updated_at) VALUES (TRUE, '2026-01-01T00:00:00Z')")
            cur.execute("INSERT INTO managed_provider_access (provider) VALUES ('openai')")
            for domain in (
                "github.com", "api.github.com", "raw.githubusercontent.com",
                "pypi.org", "files.pythonhosted.org", "*.npmjs.org",
                "example.com",
            ):
                cur.execute("INSERT INTO allowed_domains (domain) VALUES (%s)", (domain,))
            cur.execute("INSERT INTO domain_methods (domain, position, method) VALUES ('github.com', 0, 'GET')")
            cur.execute("INSERT INTO domain_methods (domain, position, method) VALUES ('example.com', 0, 'GET')")

        migrate.up(directory=self.repo_migrations, quiet=True)

        with db.transaction() as cur:
            cur.execute("SELECT integration FROM managed_integrations ORDER BY integration")
            integrations = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT domain FROM allowed_domains")
            domains = {row[0] for row in cur.fetchall()}
            cur.execute("SELECT domain FROM domain_methods")
            method_domains = {row[0] for row in cur.fetchall()}
        # Reserved preset domains (GitHub, package) are dropped so the policy
        # validates again, but no integration is auto-activated — only the
        # carried-over openai provider row remains. The operator re-enables
        # the package integrations they want. Manual rules survive.
        self.assertEqual(integrations, ["openai"])
        self.assertEqual(domains, {"example.com"})
        self.assertEqual(method_domains, {"example.com"})

        # The migrated policy parses — an upgraded host keeps its egress
        # instead of failing closed on reserved domains.
        from host.config import parse_network_controls
        from host.runtime.core.network_policy import load_policy

        parsed = parse_network_controls(load_policy())
        self.assertTrue(parsed.integrations["openai"].enabled)
        self.assertFalse(parsed.integrations["python_packages"].enabled)
        self.assertFalse(parsed.integrations["npm_packages"].enabled)
        self.assertFalse(parsed.integrations["github"].enabled)

    def test_0008_migrates_existing_tasks_and_provider_sessions(self) -> None:
        migrate.up(target=7, directory=self.repo_migrations, quiet=True)
        with db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO tasks (
                    number, status, agent_runtime, thread_id, input_message,
                    created_at, updated_at
                ) VALUES
                    (1, 'completed', 'codex', 'codex-thread', 'done', '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z'),
                    (2, 'queued', 'claude_code', 'claude-thread', 'waiting', '2026-01-01T00:00:02Z', '2026-01-01T00:00:02Z')
                """
            )
            cur.execute(
                """
                INSERT INTO thread_sessions (
                    agent_runtime, thread_id, provider_session_id, last_used_at
                ) VALUES ('codex', 'codex-thread', 'provider-thread', '2026-01-01T00:00:01Z')
                """
            )

        self.assertEqual(migrate.up(target=8, directory=self.repo_migrations, quiet=True), [8])

        with db.transaction() as cur:
            cur.execute("SELECT thread_id FROM tasks ORDER BY number")
            self.assertEqual(
                cur.fetchall(),
                [
                    ("codex-thread",),
                    ("claude-thread",),
                ],
            )
            cur.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = 'tasks'"
                " ORDER BY ordinal_position"
            )
            self.assertEqual(
                [row[0] for row in cur.fetchall()],
                [
                    "number",
                    "status",
                    "thread_id",
                    "input_message",
                    "output_message",
                    "error_message",
                    "created_at",
                    "updated_at",
                ],
            )
            cur.execute(
                "SELECT thread_id, provider_session_id, model, effort"
                " FROM thread_sessions ORDER BY thread_id"
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    ("claude-thread", None, "opus", "high"),
                    ("codex-thread", "provider-thread", "gpt-5.6-terra", "high"),
                ],
            )

        with self.assertRaises(Exception), db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO thread_sessions (
                    agent_runtime, thread_id, provider_session_id, last_used_at, model, effort
                ) VALUES (
                    'codex', 'invalid', NULL, '2026-01-01T00:00:03Z',
                    'gpt-5.6-luna', 'ultra'
                )
                """
            )

        self.assertEqual(
            migrate.down(target=7, directory=self.repo_migrations, quiet=True),
            [8],
        )
        with db.transaction() as cur:
            cur.execute("SELECT number, agent_runtime, thread_id FROM tasks ORDER BY number")
            self.assertEqual(
                cur.fetchall(),
                [(1, "codex", "codex-thread"), (2, "claude_code", "claude-thread")],
            )
            cur.execute(
                "SELECT agent_runtime, thread_id, provider_session_id"
                " FROM thread_sessions ORDER BY thread_id"
            )
            self.assertEqual(
                cur.fetchall(),
                [("codex", "codex-thread", "provider-thread")],
            )


class AppMigrationTests(unittest.TestCase):
    DB_NAME = "trustyclaw_app_migrate_test"

    def setUp(self) -> None:
        pg_harness.create_database(self.DB_NAME)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_DB_NAME": self.DB_NAME})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        # Close pooled connections to this class's database before the env
        # restore, so no later test checks one out against the wrong database.
        self.addCleanup(db.close_pool)
        migrate.up(quiet=True)
        with db.transaction() as cur:
            cur.execute(
                """
                DO $$
                BEGIN
                  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'trustyclaw-app-agent_chat') THEN
                    CREATE ROLE "trustyclaw-app-agent_chat" LOGIN;
                  END IF;
                END
                $$;
                """
            )
            cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            cur.execute('CREATE SCHEMA app_agent_chat AUTHORIZATION "trustyclaw-app-agent_chat"')

    def test_app_migration_runs_in_app_schema_and_records_host_version(self) -> None:
        self.assertEqual(_app_up("agent_chat"), [1, 2, 3, 4])
        self.assertEqual(_app_up("agent_chat"), [])

        with db.transaction() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'app_agent_chat'")
            self.assertEqual({row[0] for row in cur.fetchall()}, {"thread_tasks", "threads"})
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'app_agent_chat' AND table_name = 'threads'
                ORDER BY ordinal_position
                """
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    ("thread_id", "text"),
                    ("archived", "boolean"),
                ],
            )
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'app_agent_chat' AND table_name = 'thread_tasks'
                ORDER BY ordinal_position
                """
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    ("task_id", "text"),
                    ("thread_id", "text"),
                ],
            )
            cur.execute("SELECT app_id, version, name FROM app_schema_migrations")
            self.assertEqual(
                cur.fetchall(),
                [
                    ("agent_chat", 1, "app_state"),
                    ("agent_chat", 2, "clear_stale_host_refs"),
                    ("agent_chat", 3, "minimal_thread_index"),
                    ("agent_chat", 4, "drop_preferences"),
                ],
            )
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = 'preferences'")
            self.assertEqual(cur.fetchall(), [])

    def test_app_migration_recovers_when_sql_commits_before_host_record(self) -> None:
        app_migrate.apply_sql("agent_chat", 1, connection_user="trustyclaw-app-agent_chat")

        self.assertEqual(_app_up("agent_chat"), [1, 2, 3, 4])

        with db.transaction() as cur:
            cur.execute("SELECT app_id, version, name FROM app_schema_migrations")
            self.assertEqual(
                cur.fetchall(),
                [
                    ("agent_chat", 1, "app_state"),
                    ("agent_chat", 2, "clear_stale_host_refs"),
                    ("agent_chat", 3, "minimal_thread_index"),
                    ("agent_chat", 4, "drop_preferences"),
                ],
            )
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'app_agent_chat'")
            self.assertEqual({row[0] for row in cur.fetchall()}, {"thread_tasks", "threads"})

    def test_agent_chat_cleanup_migration_deletes_stale_host_references(self) -> None:
        app_migrate.apply_sql("agent_chat", 1, connection_user="trustyclaw-app-agent_chat")
        app_migrate.record("agent_chat", 1)
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_agent_chat")
            cur.execute(
                """
                INSERT INTO threads (thread_id, agent_runtime, archived, created_at, updated_at)
                VALUES ('old-thread', 'codex', FALSE, '2026-06-08T00:00:00Z', '2026-06-08T00:00:00Z')
                """
            )
            cur.execute(
                """
                INSERT INTO thread_tasks (task_id, thread_id, created_at)
                VALUES ('task_1', 'old-thread', '2026-06-08T00:00:00Z')
                """
            )

        self.assertEqual(_app_up("agent_chat"), [2, 3, 4])

        with db.transaction() as cur:
            cur.execute("SELECT count(*) FROM app_agent_chat.thread_tasks")
            self.assertEqual(cur.fetchone(), (0,))
            cur.execute("SELECT count(*) FROM app_agent_chat.threads")
            self.assertEqual(cur.fetchone(), (0,))

    def test_agent_chat_migration_removes_host_owned_thread_configuration(self) -> None:
        for version in (1, 2):
            app_migrate.apply_sql(
                "agent_chat", version, connection_user="trustyclaw-app-agent_chat"
            )
            app_migrate.record("agent_chat", version)
        with db.transaction(user="trustyclaw-app-agent_chat") as cur:
            cur.execute("SET LOCAL search_path TO app_agent_chat")
            cur.execute(
                """
                INSERT INTO threads (thread_id, agent_runtime, archived, created_at, updated_at)
                VALUES
                    ('codex-thread', 'codex', FALSE, '2026-06-08T00:00:00Z', '2026-06-08T00:00:00Z'),
                    ('claude-thread', 'claude_code', FALSE, '2026-06-08T00:00:00Z', '2026-06-08T00:00:00Z')
                """
            )

        self.assertEqual(_app_up("agent_chat"), [3, 4])

        with db.transaction() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'app_agent_chat' AND table_name = 'threads'"
                " ORDER BY ordinal_position"
            )
            self.assertEqual(
                [row[0] for row in cur.fetchall()],
                ["thread_id", "archived"],
            )
            cur.execute("SELECT thread_id FROM app_agent_chat.threads ORDER BY thread_id")
            self.assertEqual(cur.fetchall(), [("claude-thread",), ("codex-thread",)])

    def test_app_migration_cannot_reset_back_to_host_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            migrations = Path(temp_dir) / "migrations"
            migrations.mkdir()
            _write(
                migrations,
                "0001_escape.sql",
                "RESET ROLE; CREATE TABLE public.host_escape_attempt (id INT);",
            )
            app = app_platform.AppManifest(
                id="agent_chat",
                title="Agent Chat",
                package_dir=Path(temp_dir),
                backend_entrypoint=Path(temp_dir) / "backend.py",
                migrations_dir=migrations,
                ui_dir=Path(temp_dir),
                port=7450,
                allocation=app_platform.AppAllocation(uid=48000, gid=48000, port_offset=0),
                agent_instructions="Test app instructions.",
            )
            with patch("host.runtime.core.app_platform.app_by_id", return_value=app):
                with self.assertRaises(Exception):
                    _app_up("agent_chat")

        with db.transaction() as cur:
            cur.execute("SELECT to_regclass('public.host_escape_attempt')")
            self.assertEqual(cur.fetchone(), (None,))
            cur.execute("SELECT app_id, version, name FROM app_schema_migrations")
            self.assertEqual(cur.fetchall(), [])


if __name__ == "__main__":
    unittest.main()
