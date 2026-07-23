from __future__ import annotations

from pathlib import Path
import json
import importlib.util
import re
import tempfile
import unittest

from host.runtime.core import app_platform
from host.runtime.deploy import migrate
from host.constants import APP_PORT_BASE


RELEASED_APP_SLOTS = {
    "agent_chat": 0,
    "mission_pursuit": 1,
    "alpha_seeker": 2,
    "social_marketer": 3,
    "virality_machine": 4,
    "software_builder": 5,
    "personal_web_app_builder": 6,
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
        self.assertIn("You are working in Agent Chat", agent_chat.agent_instructions)
        self.assertFalse(agent_chat.agent_api)
        self.assertEqual(agent_chat.release_stage, "stable")
        self.assertEqual(agent_chat.public()["ui"]["iframe_src"], "/v1/apps/agent_chat/ui/index.html")
        self.assertEqual(set(agent_chat.public()), {"id", "title", "release_stage", "backend", "ui"})
        self.assertEqual(set(agent_chat.public()["backend"]), {"api_route"})
        self.assertEqual(set(agent_chat.public()["ui"]), {"iframe_src", "sandbox"})
        allocation = agent_chat.allocation
        self.assertIsNotNone(allocation)
        assert allocation is not None
        self.assertEqual(allocation.uid, 48000)
        self.assertEqual(allocation.gid, 48000)
        self.assertEqual(allocation.port_offset, 0)

    def test_installed_mission_pursuit_manifest_owns_its_agent_protocol(self) -> None:
        apps = {app.id: app for app in app_platform.installed_apps()}
        mission = apps["mission_pursuit"]

        self.assertTrue(mission.agent_api)
        self.assertIn("You are the resident agent of Mission Pursuit", mission.agent_instructions)
        self.assertIn('"path": "/agent/actions"', mission.agent_instructions)
        self.assertIn('"type":"artifact_interaction"', mission.agent_instructions)
        self.assertIn('"type": "button"', mission.agent_instructions)

    def test_installed_app_migrations_are_well_formed(self) -> None:
        for app in app_platform.installed_apps():
            with self.subTest(app_id=app.id):
                self.assertTrue(migrate.load_migrations(app.migrations_dir))

    def test_workspace_kit_ui_has_no_agent_controlled_browser_capability_sink(self) -> None:
        forbidden = (
            "window.open", "location.href", "location.assign", "location.replace",
            "document.location", "document.write", "eval(", "new Function",
            "fetch(", "XMLHttpRequest", "WebSocket", "EventSource",
            'createElement("script")', 'createElement("iframe")',
        )
        canonical_renderer = app_platform.APP_ROOT / "workspace_kit" / "ui" / "view_blocks.js"
        canonical_source = canonical_renderer.read_text()
        self.assertIn("div.textContent", canonical_source)
        for sink in forbidden:
            self.assertNotIn(sink, canonical_source, f"shared renderer exposes browser sink {sink}")
        checked = 0
        for app in app_platform.installed_apps():
            ui = app.ui_dir
            index = (ui / "index.html").read_text()
            if "/workspace-kit/view_blocks.js" not in index:
                continue
            with self.subTest(app_id=app.id):
                source = "\n".join(path.read_text() for path in sorted(ui.glob("*.js")))
                for sink in forbidden:
                    self.assertNotIn(sink, source, f"{app.id} exposes browser sink {sink}")
                self.assertIsNone(re.search(r"<a(?:\s|>)", index, re.I))
                self.assertIn("/workspace-kit/view_blocks.css", index)
                checked += 1
        # Agent Chat and Personal Web App Builder are independent products;
        # the other released apps use the shared Workspace Kit renderer.
        self.assertEqual(checked, len(RELEASED_APP_SLOTS) - 2)

    def test_personal_web_app_builder_alone_opts_into_capability_workers(self) -> None:
        apps = {app.id: app for app in app_platform.installed_apps()}

        builder = apps["personal_web_app_builder"]
        self.assertTrue(builder.capability_worker)
        self.assertTrue(builder.agent_api)
        self.assertEqual(builder.release_stage, "stable")
        self.assertEqual(builder.allocation.port_offset, 6)
        self.assertIn("dedicated capability worker", builder.agent_instructions)
        for app_id, app in apps.items():
            if app_id != builder.id:
                self.assertFalse(app.capability_worker, app_id)

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
        self.assertEqual(
            {app_id: app.release_stage for app_id, app in apps.items()},
            {
                "agent_chat": "stable",
                "personal_web_app_builder": "stable",
                "mission_pursuit": "beta",
                "alpha_seeker": "beta",
                "social_marketer": "beta",
                "virality_machine": "beta",
                "software_builder": "beta",
            },
        )
        for app_id, host_slot in RELEASED_APP_SLOTS.items():
            with self.subTest(app_id=app_id):
                app = apps[app_id]
                self.assertEqual(app.allocation.port_offset, host_slot)
                self.assertEqual(app.allocation.uid, app_platform.APP_UID_BASE + host_slot)
                self.assertEqual(app.allocation.gid, app_platform.APP_UID_BASE + host_slot)
                self.assertEqual(app.port, APP_PORT_BASE + host_slot)

    def test_every_installed_app_has_dynamic_smoke_coverage(self) -> None:
        smoke_root = Path(__file__).parent / "apps"
        for app in app_platform.installed_apps():
            with self.subTest(app_id=app.id):
                smoke = smoke_root / app.id / "smoke.py"
                self.assertTrue(smoke.is_file(), f"missing smoke module for {app.id}")
                spec = importlib.util.spec_from_file_location(f"smoke_{app.id}", smoke)
                self.assertIsNotNone(spec)
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertTrue(callable(getattr(module, "desktop_smoke", None)))
                self.assertTrue(callable(getattr(module, "mobile_smoke", None)))

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

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "legacy", extra={"agent_api": True})
            with self.assertRaisesRegex(app_platform.AppError, "unsupported agent_api"):
                app_platform.installed_apps(root)

    def test_manifest_rejects_invalid_release_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad_app", release_stage="preview")
            with self.assertRaisesRegex(app_platform.AppError, "must be 'stable' or 'beta'"):
                app_platform.installed_apps(root)

    def test_manifest_requires_release_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad_app")
            manifest_path = root / "bad_app" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            del manifest["release_stage"]
            manifest_path.write_text(json.dumps(manifest))

            with self.assertRaisesRegex(app_platform.AppError, "missing release_stage"):
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

    def test_manifest_agent_contract_is_required_and_loads_bounded_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "plain", host_slot=0)
            self._write_minimal_app(
                root,
                "agentic",
                host_slot=1,
                agent_instructions="Use the app API exactly as documented.",
                agent_api=True,
            )
            apps = {app.id: app for app in app_platform.installed_apps(root)}
            self.assertEqual(apps["plain"].agent_instructions, "Instructions for this app.")
            self.assertFalse(apps["plain"].agent_api)
            self.assertEqual(apps["agentic"].agent_instructions, "Use the app API exactly as documented.")
            self.assertTrue(apps["agentic"].agent_api)
            self.assertNotIn("agent", apps["agentic"].public())

    def test_manifest_rejects_invalid_agent_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad", agent_instructions=None)
            with self.assertRaisesRegex(app_platform.AppError, "missing agent"):
                app_platform.installed_apps(root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad", extra={"agent": "agent.md"})
            with self.assertRaisesRegex(app_platform.AppError, "agent must be an object"):
                app_platform.installed_apps(root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "bad", extra={"agent": {"instructions": "agent.md"}})
            with self.assertRaisesRegex(app_platform.AppError, "missing api"):
                app_platform.installed_apps(root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(
                root,
                "bad",
                extra={"agent": {"instructions": "agent.md", "api": "yes"}},
            )
            with self.assertRaisesRegex(app_platform.AppError, "agent.api must be a boolean"):
                app_platform.installed_apps(root)

    def test_manifest_rejects_missing_empty_oversized_and_symlinked_agent_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "missing", extra={"agent": {"instructions": "missing.md", "api": False}})
            with self.assertRaisesRegex(app_platform.AppError, "agent.instructions does not exist"):
                app_platform.installed_apps(root)

        for label, content, error in (
            ("empty", "", "must not be empty"),
            ("nul", "before\0after", "must not contain NUL bytes"),
            ("oversized", "x" * (app_platform.MAX_AGENT_INSTRUCTIONS_BYTES + 1), "exceeds 16384 bytes"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._write_minimal_app(root, label, agent_instructions=content)
                with self.assertRaisesRegex(app_platform.AppError, error):
                    app_platform.installed_apps(root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "linked", agent_instructions="ignored")
            instructions = root / "linked" / "agent.md"
            instructions.unlink()
            instructions.symlink_to(root / "linked" / "backend.py")
            with self.assertRaisesRegex(app_platform.AppError, "regular non-symlink"):
                app_platform.installed_apps(root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_minimal_app(root, "binary", agent_instructions="placeholder")
            (root / "binary" / "agent.md").write_bytes(b"\xff")
            with self.assertRaisesRegex(app_platform.AppError, "must be UTF-8"):
                app_platform.installed_apps(root)

    def _write_minimal_app(
        self,
        root: Path,
        app_id: str,
        *,
        host_slot: int = 0,
        title: str | None = None,
        release_stage: str = "stable",
        extra: dict[str, object] | None = None,
        agent_instructions: str | None = "Instructions for this app.",
        agent_api: bool = False,
    ) -> None:
        app_dir = root / app_id
        app_dir.mkdir()
        (app_dir / "backend.py").write_text("")
        (app_dir / "migrations").mkdir()
        (app_dir / "ui").mkdir()
        manifest: dict[str, object] = {
            "host_slot": host_slot,
            "title": app_id if title is None else title,
            "release_stage": release_stage,
            "backend": {"entrypoint": "backend.py"},
            "database": {"migrations": "migrations"},
            "ui": {"path": "ui"},
        }
        if agent_instructions is not None:
            (app_dir / "agent.md").write_text(agent_instructions)
            manifest["agent"] = {"instructions": "agent.md", "api": agent_api}
        manifest.update(extra or {})
        (app_dir / "manifest.json").write_text(json.dumps(manifest))


if __name__ == "__main__":
    unittest.main()
