"""Installed app packages and host-derived integration metadata.

Apps provide manifests and files under ``host/apps/<app_id>/``. The package
directory is the app's identity. The host derives service users, database
namespaces, routes, and ports from that id plus the package's stable host slot,
so there is no second registry that can drift from the packages on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import mimetypes
import re
from typing import Any, Literal, cast

from host.constants import APP_PORT_BASE


APP_ROOT = Path(__file__).resolve().parents[2] / "apps"
APP_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
# Separates the app id from the app-visible thread id in host-internal thread
# names (`<app_id>__<thread_id>`); the canonical constant for every consumer.
APP_SCOPED_ID_SEPARATOR = "__"
PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
POSTGRES_IDENTIFIER_LIMIT = 63
MAX_INSTALLED_APPS = 100
MAX_AGENT_INSTRUCTIONS_BYTES = 16 * 1024
APP_UID_BASE = 48000
APP_UID_MAX = APP_UID_BASE + MAX_INSTALLED_APPS - 1
APP_PORT_OFFSET_MIN = 0
APP_PORT_OFFSET_MAX = MAX_INSTALLED_APPS - 1
APP_ACCOUNT_PREFIX = "trustyclaw-app-"
LINUX_ACCOUNT_NAME_LIMIT = 32
ReleaseStage = Literal["stable", "beta"]


class AppError(Exception):
    pass


@dataclass(frozen=True)
class AppAllocation:
    uid: int
    gid: int
    port_offset: int


@dataclass(frozen=True)
class AppManifest:
    id: str
    title: str
    release_stage: ReleaseStage
    package_dir: Path
    backend_entrypoint: Path
    migrations_dir: Path
    ui_dir: Path
    port: int
    allocation: AppAllocation
    # Static app-owned behavior and protocol guidance. The host attaches this
    # at the runtime instruction boundary for every task created by the app;
    # current user input and app state remain separate task content.
    agent_instructions: str
    # Opt-in agent-facing backend API: when true, agents working this app's
    # tasks get the app_api tool, proxied to the backend's /agent/ routes by
    # the trustyclaw-agent-app service (docs/architecture/apps/agent-app-api.md).
    agent_api: bool = False
    # Opt-in for an app that executes untrusted computation in a blob-backed
    # dedicated worker. The worker remains under the app frame's CSP; no
    # other app gets blob worker execution by default.
    capability_worker: bool = False

    @property
    def linux_user(self) -> str:
        candidate = f"{APP_ACCOUNT_PREFIX}{self.id}"
        if len(candidate.encode()) <= LINUX_ACCOUNT_NAME_LIMIT:
            return candidate
        return f"{APP_ACCOUNT_PREFIX}{self.allocation.port_offset}"

    @property
    def db_schema(self) -> str:
        return "app_" + self.id

    @property
    def db_role(self) -> str:
        return self.linux_user

    @property
    def service_name(self) -> str:
        return f"trustyclaw-app-{self.id}.service"

    @property
    def api_route(self) -> str:
        return f"/v1/apps/{self.id}/api/"

    @property
    def ui_route(self) -> str:
        return f"/v1/apps/{self.id}/ui/"

    def public(self) -> dict[str, Any]:
        # Only what the admin shell reads: the service unit, DB schema/role,
        # and localhost base URL are fixed derivations documented in
        # docs/architecture/apps/apps.md and never leave the host.
        return {
            "id": self.id,
            "title": self.title,
            "release_stage": self.release_stage,
            "backend": {"api_route": self.api_route},
            "ui": {
                "iframe_src": f"{self.ui_route}index.html",
                "sandbox": ["allow-scripts", "allow-forms", "allow-modals"],
            },
        }


def installed_apps(root: Path | None = None) -> list[AppManifest]:
    root = APP_ROOT if root is None else root
    if not root.exists():
        return []
    package_dirs = _app_package_dirs(root)
    if len(package_dirs) > MAX_INSTALLED_APPS:
        raise AppError(f"too many installed apps: maximum is {MAX_INSTALLED_APPS}")
    apps: list[AppManifest] = []
    seen_generated: set[tuple[str, str]] = set()
    for package_dir in package_dirs:
        app = _load_manifest(package_dir / "manifest.json")
        for kind, value in (
            ("linux_user", app.linux_user),
            ("db_schema", app.db_schema),
            ("db_role", app.db_role),
            ("service_name", app.service_name),
            ("port", str(app.port)),
        ):
            key = (kind, value)
            if key in seen_generated:
                raise AppError(f"duplicate generated app {kind}: {value}")
            seen_generated.add(key)
        apps.append(app)
    apps.sort(key=lambda app: (app.allocation.port_offset, app.id))
    return apps


def app_by_id(app_id: str, root: Path | None = None) -> AppManifest | None:
    for app in installed_apps(root):
        if app.id == app_id:
            return app
    return None


def ui_asset(path: str, root: Path | None = None) -> tuple[AppManifest, Path, str] | None:
    prefix = "/v1/apps/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix):].split("/", 2)
    if len(parts) < 2 or parts[1] != "ui":
        return None
    app = app_by_id(parts[0], root)
    if app is None:
        return None
    relative = "index.html" if len(parts) == 2 or not parts[2] else parts[2]
    asset = _resolve_inside(app.ui_dir, relative)
    if asset is None or not asset.is_file():
        raise AppError("app UI asset not found")
    content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
    if asset.suffix == ".js":
        content_type = "application/javascript; charset=utf-8"
    elif asset.suffix == ".css":
        content_type = "text/css; charset=utf-8"
    elif asset.suffix in {".html", ".htm"}:
        content_type = "text/html; charset=utf-8"
    return app, asset, content_type


def _app_package_dirs(root: Path) -> list[Path]:
    packages: list[Path] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if child.name == "__pycache__":
            continue
        if child.is_symlink():
            raise AppError(f"{child}: app package directory must not be a symlink")
        if not child.is_dir():
            raise AppError(f"{child}: every entry under host/apps must be an app package directory")
        if not APP_ID_RE.fullmatch(child.name):
            raise AppError(f"{child}: app package directory must match {APP_ID_RE.pattern}")
        manifest_path = child / "manifest.json"
        if not manifest_path.exists() and (child / "__init__.py").is_file():
            # An importable Python library colocated under host/apps for
            # locality (for example workspace_kit, the shared workspace engine):
            # it has an __init__.py and no manifest, so it is not an installed
            # app and is skipped. A directory with neither is still a mistake.
            continue
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise AppError(f"{child}: app package must contain a regular manifest.json file")
        packages.append(child)
    return packages


def _load_manifest(path: Path) -> AppManifest:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AppError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AppError(f"{path}: manifest must be an object")
    package_dir = path.parent
    _require_exact_keys(
        data,
        {"host_slot", "title", "release_stage", "backend", "database", "ui", "agent"},
        path,
        "manifest",
    )
    agent_value = _required_object(data, "agent", path)
    _require_exact_keys(agent_value, {"instructions", "api"}, path, "agent")
    agent_api = agent_value["api"]
    if not isinstance(agent_api, bool):
        raise AppError(f"{path}: agent.api must be a boolean")
    instructions_relative = _required_string(agent_value, "instructions", path)
    if (package_dir / instructions_relative).is_symlink():
        raise AppError(f"{path}: agent.instructions must be a regular non-symlink file")
    instructions_path = _required_child(package_dir, agent_value, "instructions", path)
    if not instructions_path.is_file():
        raise AppError(f"{path}: agent.instructions does not exist")
    if instructions_path.stat().st_size > MAX_AGENT_INSTRUCTIONS_BYTES:
        raise AppError(
            f"{path}: agent.instructions exceeds {MAX_AGENT_INSTRUCTIONS_BYTES} bytes"
        )
    try:
        instruction_bytes = instructions_path.read_bytes()
        agent_instructions = instruction_bytes.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise AppError(f"{path}: agent.instructions must be UTF-8") from exc
    if not agent_instructions:
        raise AppError(f"{path}: agent.instructions must not be empty")
    if "\0" in agent_instructions:
        raise AppError(f"{path}: agent.instructions must not contain NUL bytes")
    # Defend against a file replacement between stat and read. App files are
    # root-owned in production, but validation remains self-contained.
    if len(instruction_bytes) > MAX_AGENT_INSTRUCTIONS_BYTES:
        raise AppError(
            f"{path}: agent.instructions exceeds {MAX_AGENT_INSTRUCTIONS_BYTES} bytes"
        )
    app_id = package_dir.name
    host_slot = _required_int(data, "host_slot", path)
    if not APP_PORT_OFFSET_MIN <= host_slot <= APP_PORT_OFFSET_MAX:
        raise AppError(
            f"{path}: host_slot must be in {APP_PORT_OFFSET_MIN}-{APP_PORT_OFFSET_MAX}"
        )
    allocation = AppAllocation(
        uid=APP_UID_BASE + host_slot,
        gid=APP_UID_BASE + host_slot,
        port_offset=host_slot,
    )
    title = _required_string(data, "title", path)
    if "\n" in title or "\r" in title:
        raise AppError(f"{path}: title must be a single line")
    release_stage = _required_string(data, "release_stage", path)
    if release_stage not in {"stable", "beta"}:
        raise AppError(f"{path}: release_stage must be 'stable' or 'beta'")
    backend = _required_object(data, "backend", path)
    database = _required_object(data, "database", path)
    ui = _required_object(data, "ui", path)
    _require_exact_keys(backend, {"entrypoint"}, path, "backend")
    _require_exact_keys(database, {"migrations"}, path, "database")
    _require_exact_keys(ui, {"path"}, path, "ui", optional={"capability_worker"})
    capability_worker = ui.get("capability_worker", False)
    if not isinstance(capability_worker, bool):
        raise AppError(f"{path}: ui.capability_worker must be a boolean")
    backend_entrypoint = _required_child(package_dir, backend, "entrypoint", path)
    migrations_dir = _required_child(package_dir, database, "migrations", path)
    ui_dir = _required_child(package_dir, ui, "path", path)
    if not backend_entrypoint.is_file():
        raise AppError(f"{path}: backend.entrypoint does not exist")
    if not migrations_dir.is_dir():
        raise AppError(f"{path}: database.migrations does not exist")
    if not ui_dir.is_dir():
        raise AppError(f"{path}: ui.path does not exist")
    app = AppManifest(
        id=app_id,
        title=title,
        release_stage=cast(ReleaseStage, release_stage),
        package_dir=package_dir,
        backend_entrypoint=backend_entrypoint,
        migrations_dir=migrations_dir,
        ui_dir=ui_dir,
        port=APP_PORT_BASE + allocation.port_offset,
        allocation=allocation,
        agent_instructions=agent_instructions,
        agent_api=agent_api,
        capability_worker=capability_worker,
    )
    _validate_postgres_identifier(app.db_schema, "database schema", path)
    _validate_postgres_identifier(app.db_role, "database role", path)
    return app


def _required_int(data: dict[str, Any], key: str, path: Path) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppError(f"{path}: {key} must be an integer")
    return value


def _required_string(data: dict[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AppError(f"{path}: {key} must be a non-empty string")
    return value.strip()


def _required_object(data: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise AppError(f"{path}: {key} must be an object")
    return value


def _require_exact_keys(
    data: dict[str, Any], expected: set[str], path: Path, label: str, *, optional: set[str] | None = None
) -> None:
    keys = set(data) - (optional or set())
    if keys == expected:
        return
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    details: list[str] = []
    if missing:
        details.append(f"missing {', '.join(missing)}")
    if extra:
        details.append(f"unsupported {', '.join(extra)}")
    raise AppError(f"{path}: {label} fields are invalid: {'; '.join(details)}")


def _required_child(base: Path, data: dict[str, Any], key: str, path: Path) -> Path:
    relative = _required_string(data, key, path)
    child = _resolve_inside(base, relative)
    if child is None:
        raise AppError(f"{path}: {key} must stay inside the app package")
    return child


def _validate_postgres_identifier(value: str, label: str, path: Path) -> None:
    if len(value.encode("utf-8")) > POSTGRES_IDENTIFIER_LIMIT:
        raise AppError(f"{path}: generated {label} exceeds PostgreSQL {POSTGRES_IDENTIFIER_LIMIT}-byte identifier limit")


def _resolve_inside(base: Path, relative: str) -> Path | None:
    if relative.startswith("/") or not PATH_RE.fullmatch(relative) or ".." in Path(relative).parts:
        return None
    base_resolved = base.resolve()
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        return None
    return candidate
