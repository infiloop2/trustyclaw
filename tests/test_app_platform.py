from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from host.runtime import app_platform
from host.constants import APP_PORT_BASE


RELEASED_APP_SLOTS = {
    "agent_chat": 0,
}


class AppPlatformTests(unittest.TestCase):
    def test_installed_agent_chat_manifest_derives_host_owned_names(self) -> None:
        apps = app_platform.installed_apps()

        agent_chat = next(app for app in apps if app.id == "agent_chat")

        self.assertEqual(agent_chat.linux_user, "trustyclaw-app-agent_chat")
        self.assertEqual(agent_chat.db_schema, "app_agent_chat")
        self.assertEqual(agent_chat.db_role, "trustyclaw-app-agent_chat")
        self.assertEqual(agent_chat.service_name, "trustyclaw-app-agent_chat.service")
        self.assertEqual(agent_chat.port, APP_PORT_BASE)
        self.assertEqual(agent_chat.public()["ui"]["iframe_src"], "/v1/apps/agent_chat/ui/index.html")
        self.assertEqual(set(agent_chat.public()), {"id", "title", "backend", "ui"})
        self.assertEqual(set(agent_chat.public()["backend"]), {"api_route"})
        self.assertEqual(set(agent_chat.public()["ui"]), {"iframe_src", "sandbox"})
        allocation = agent_chat.allocation
        self.assertIsNotNone(allocation)
        assert allocation is not None
        self.assertEqual(allocation.uid, 48000)
        self.assertEqual(allocation.gid, 48000)
        self.assertEqual(allocation.port_offset, 0)

    def test_installed_apps_have_unique_host_owned_names(self) -> None:
        apps = app_platform.installed_apps()

        self.assertGreaterEqual(len(apps), 1)
        self.assertEqual(len({app.id for app in apps}), len(apps))
        self.assertEqual(len({app.linux_user for app in apps}), len(apps))
        self.assertEqual(len({app.db_schema for app in apps}), len(apps))
        self.assertEqual(len({app.db_role for app in apps}), len(apps))
        self.assertEqual(len({app.service_name for app in apps}), len(apps))
        self.assertEqual(len({app.port for app in apps}), len(apps))

    def test_app_identity_and_allocations_come_from_each_package(self) -> None:
        apps = app_platform.installed_apps()

        for app in apps:
            with self.subTest(app_id=app.id):
                self.assertEqual(app.id, app.package_dir.name)
                self.assertEqual(app.allocation.uid, app_platform.APP_UID_BASE + app.allocation.port_offset)
                self.assertEqual(app.allocation.gid, app.allocation.uid)
                self.assertEqual(app.port, APP_PORT_BASE + app.allocation.port_offset)

    def test_released_apps_and_slots_do_not_change(self) -> None:
        apps = {app.id: app for app in app_platform.installed_apps()}

        self.assertEqual(set(apps), set(RELEASED_APP_SLOTS))
        for app_id, host_slot in RELEASED_APP_SLOTS.items():
            with self.subTest(app_id=app_id):
                app = apps[app_id]
                self.assertEqual(app.allocation.port_offset, host_slot)
                self.assertEqual(app.allocation.uid, app_platform.APP_UID_BASE + host_slot)
                self.assertEqual(app.allocation.gid, app_platform.APP_UID_BASE + host_slot)
                self.assertEqual(app.port, APP_PORT_BASE + host_slot)

    def test_agent_chat_port_does_not_depend_on_manifest_scan_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "aaa_app", host_slot=1)
            self._write_minimal_app(root, "agent_chat", host_slot=0)

            apps = app_platform.installed_apps(root)
            ports = {app.id: app.port for app in apps}

            self.assertLess("aaa_app", "agent_chat")
            self.assertEqual(ports["agent_chat"], APP_PORT_BASE)
            self.assertNotEqual(ports["aaa_app"], APP_PORT_BASE)

    def test_installed_apps_rejects_more_than_one_hundred_apps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(app_platform.MAX_INSTALLED_APPS + 1):
                self._write_minimal_app(root, f"app_{index:03d}")

            with self.assertRaisesRegex(app_platform.AppError, "maximum is 100"):
                app_platform.installed_apps(root)

    def test_package_directory_rejects_hyphenated_app_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad-app")

            with self.assertRaisesRegex(app_platform.AppError, "package directory must match"):
                app_platform.installed_apps(root)

    def test_package_directory_requires_a_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bad_app").mkdir()

            with self.assertRaisesRegex(app_platform.AppError, "must contain a regular manifest.json"):
                app_platform.installed_apps(root)

    def test_app_root_rejects_non_package_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "registry.json").write_text("{}")

            with self.assertRaisesRegex(app_platform.AppError, "every entry under host/apps"):
                app_platform.installed_apps(root)

    def test_manifest_rejects_multiline_titles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad_app", title="Bad\nUNIT\nApp")

            with self.assertRaisesRegex(app_platform.AppError, "title must be a single line"):
                app_platform.installed_apps(root)

    def test_manifest_rejects_out_of_range_host_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad_app", host_slot=-1)

            with self.assertRaisesRegex(app_platform.AppError, "host_slot must be in 0-99"):
                app_platform.installed_apps(root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad_app", host_slot=100)

            with self.assertRaisesRegex(app_platform.AppError, "host_slot must be in 0-99"):
                app_platform.installed_apps(root)

    def test_installed_apps_reject_duplicate_host_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "first_app", host_slot=4)
            self._write_minimal_app(root, "second_app", host_slot=4)

            with self.assertRaisesRegex(app_platform.AppError, "duplicate generated app"):
                app_platform.installed_apps(root)

    def test_manifest_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad_app", extra={"id": "bad_app"})

            with self.assertRaisesRegex(app_platform.AppError, "unsupported id"):
                app_platform.installed_apps(root)

    def test_ui_asset_resolves_only_inside_app_ui_directory(self) -> None:
        resolved = app_platform.ui_asset("/v1/apps/agent_chat/ui/index.html")
        self.assertIsNotNone(resolved)
        app, asset, content_type = resolved
        self.assertEqual(app.id, "agent_chat")
        self.assertEqual(asset.name, "index.html")
        self.assertTrue(content_type.startswith("text/html"))

        with self.assertRaises(app_platform.AppError):
            app_platform.ui_asset("/v1/apps/agent_chat/ui/../../manifest.json")

    def test_manifest_rejects_app_chosen_paths_outside_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app_dir = root / "bad_app"
            app_dir.mkdir()
            (app_dir / "backend.py").write_text("")
            (app_dir / "migrations").mkdir()
            (app_dir / "ui").mkdir()
            (app_dir / "manifest.json").write_text(json.dumps({
                "id": "bad_app",
                "title": "Bad App",
                "backend": {"entrypoint": "../backend.py"},
                "database": {"migrations": "migrations"},
                "ui": {"path": "ui"},
            }))

            with self.assertRaises(app_platform.AppError):
                app_platform.installed_apps(root)

    def test_manifest_rejects_generated_postgres_identifiers_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app_id = "a" * 49
            self._write_minimal_app(root, app_id)

            with self.assertRaisesRegex(app_platform.AppError, "PostgreSQL 63-byte identifier limit"):
                app_platform.installed_apps(root)

    def _write_minimal_app(
        self,
        root: Path,
        app_id: str,
        *,
        host_slot: int = 0,
        title: str | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        app_dir = root / app_id
        app_dir.mkdir()
        (app_dir / "backend.py").write_text("")
        (app_dir / "migrations").mkdir()
        (app_dir / "ui").mkdir()
        manifest: dict[str, object] = {
            "host_slot": host_slot,
            "title": app_id if title is None else title,
            "backend": {"entrypoint": "backend.py"},
            "database": {"migrations": "migrations"},
            "ui": {"path": "ui"},
        }
        manifest.update(extra or {})
        (app_dir / "manifest.json").write_text(json.dumps(manifest))


if __name__ == "__main__":
    unittest.main()
