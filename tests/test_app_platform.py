from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from host.runtime import app_platform
from host.constants import APP_PORT_BASE


class AppPlatformTests(unittest.TestCase):
    def test_installed_agent_chat_manifest_derives_host_owned_names(self) -> None:
        apps = app_platform.installed_apps()

        agent_chat = next(app for app in apps if app.id == "agent_chat")

        self.assertEqual(agent_chat.linux_user, "trustyclaw-app-agent_chat")
        self.assertEqual(agent_chat.db_schema, "app_agent_chat")
        self.assertEqual(agent_chat.db_role, "trustyclaw-app-agent_chat")
        self.assertEqual(agent_chat.service_name, "trustyclaw-app-agent_chat.service")
        self.assertEqual(agent_chat.port, APP_PORT_BASE)
        self.assertEqual(agent_chat.public()["backend"]["localhost_base_url"], f"http://127.0.0.1:{APP_PORT_BASE}")
        self.assertEqual(agent_chat.public()["ui"]["iframe_src"], "/v1/apps/agent_chat/ui/index.html")
        self.assertEqual(set(agent_chat.public()), {"id", "title", "backend", "database", "ui"})
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

    def test_app_registry_pins_static_allocations(self) -> None:
        expected = {
            "agent_chat": {
                "uid": 48000,
                "gid": 48000,
                "port_offset": 0,
            },
        }
        registry = app_platform.app_registry()

        self.assertEqual(set(registry), set(expected))
        for app_id, expected_allocation in expected.items():
            with self.subTest(app_id=app_id):
                allocation = registry[app_id]
                self.assertEqual(allocation.uid, expected_allocation["uid"])
                self.assertEqual(allocation.gid, expected_allocation["gid"])
                self.assertEqual(allocation.port_offset, expected_allocation["port_offset"])

    def test_agent_chat_port_does_not_depend_on_manifest_scan_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "aaa_app")
            self._write_minimal_app(root, "agent_chat")
            self._write_registry(root, {"agent_chat": {"uid": 48000, "gid": 48000, "port_offset": 0}})

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

    def test_manifest_rejects_hyphenated_app_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad_app", manifest_id="bad-app")

            with self.assertRaisesRegex(app_platform.AppError, "id must match"):
                app_platform.installed_apps(root)

    def test_manifest_rejects_multiline_titles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad_app", title="Bad\nUNIT\nApp")

            with self.assertRaisesRegex(app_platform.AppError, "title must be a single line"):
                app_platform.installed_apps(root)

    def test_registry_rejects_out_of_range_port_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad_app")
            self._write_registry(root, {"bad_app": {"uid": 48000, "gid": 48000, "port_offset": -1}})

            with self.assertRaisesRegex(app_platform.AppError, "port_offset must be in 0-99"):
                app_platform.installed_apps(root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad_app")
            self._write_registry(root, {"bad_app": {"uid": 48000, "gid": 48000, "port_offset": 100}})

            with self.assertRaisesRegex(app_platform.AppError, "port_offset must be in 0-99"):
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
        manifest_id: str | None = None,
        title: str | None = None,
    ) -> None:
        app_dir = root / app_id
        app_dir.mkdir()
        (app_dir / "backend.py").write_text("")
        (app_dir / "migrations").mkdir()
        (app_dir / "ui").mkdir()
        (app_dir / "manifest.json").write_text(json.dumps({
            "id": app_id if manifest_id is None else manifest_id,
            "title": app_id if title is None else title,
            "backend": {"entrypoint": "backend.py"},
            "database": {"migrations": "migrations"},
            "ui": {"path": "ui"},
        }))

    def _write_registry(self, root: Path, data: dict[str, dict[str, int]]) -> None:
        (root / "registry.json").write_text(json.dumps(data))


if __name__ == "__main__":
    unittest.main()
