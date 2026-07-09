"""Installed app registry and host-derived integration metadata.

Apps provide manifests and files under ``host/apps/<app_id>/``. The host
derives service users, database namespaces, routes, and ports from the app id
so app packages cannot collide with host objects by choosing those names
themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import mimetypes
import re
from typing import Any

from host.constants import APP_PORT_BASE, LOOPBACK


APP_ROOT = Path(__file__).resolve().parents[1] / "apps"
APP_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
POSTGRES_IDENTIFIER_LIMIT = 63
MAX_INSTALLED_APPS = 100
APP_UID_BASE = 48000
APP_UID_MAX = APP_UID_BASE + MAX_INSTALLED_APPS - 1
APP_PORT_OFFSET_MIN = 0
APP_PORT_OFFSET_MAX = MAX_INSTALLED_APPS - 1


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
    package_dir: Path
    backend_entrypoint: Path
    migrations_dir: Path
    ui_dir: Path
    port: int
    allocation: AppAllocation | None = None

    @property
    def linux_user(self) -> str:
        return f"trustyclaw-app-{self.id}"

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
        return {
            "id": self.id,
            "title": self.title,
            "backend": {
                "api_route": self.api_route,
                "localhost_base_url": f"http://{LOOPBACK}:{self.port}",
                "service": self.service_name,
            },
            "database": {
                "schema": self.db_schema,
                "role": self.db_role,
            },
            "ui": {
                "iframe_src": f"{self.ui_route}index.html",
                "sandbox": ["allow-scripts", "allow-forms", "allow-modals"],
            },
        }


def installed_apps(root: Path | None = None) -> list[AppManifest]:
    root = APP_ROOT if root is None else root
    if not root.exists():
        return []
    manifest_paths = sorted(root.glob("*/manifest.json"))
    if len(manifest_paths) > MAX_INSTALLED_APPS:
        raise AppError(f"too many installed apps: maximum is {MAX_INSTALLED_APPS}")
    apps: list[AppManifest] = []
    seen_ids: set[str] = set()
    seen_generated: set[tuple[str, str]] = set()
    registry = app_registry(root)
    for manifest_path in manifest_paths:
        app = _load_manifest(
            manifest_path,
            allocation=registry.get(manifest_path.parent.name),
            port=_port_for_app_id(manifest_path.parent.name, registry=registry),
        )
        if app.id in seen_ids:
            raise AppError(f"duplicate app id: {app.id}")
        seen_ids.add(app.id)
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
    missing = sorted(set(registry) - seen_ids)
    if missing:
        raise AppError(f"app registry references missing app packages: {', '.join(missing)}")
    return apps


def app_registry(root: Path | None = None) -> dict[str, AppAllocation]:
    root = APP_ROOT if root is None else root
    path = root / "registry.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AppError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AppError(f"{path}: registry must be an object")
    registry: dict[str, AppAllocation] = {}
    seen_uids: set[int] = set()
    seen_gids: set[int] = set()
    seen_offsets: set[int] = set()
    for app_id, raw in data.items():
        if not isinstance(app_id, str) or not APP_ID_RE.fullmatch(app_id):
            raise AppError(f"{path}: registry app id must match {APP_ID_RE.pattern}")
        if not isinstance(raw, dict):
            raise AppError(f"{path}: registry entry for {app_id} must be an object")
        allocation = AppAllocation(
            uid=_required_int(raw, "uid", path),
            gid=_required_int(raw, "gid", path),
            port_offset=_required_int(raw, "port_offset", path),
        )
        if not APP_UID_BASE <= allocation.uid <= APP_UID_MAX:
            raise AppError(f"{path}: {app_id} uid must be in {APP_UID_BASE}-{APP_UID_MAX}")
        if not APP_UID_BASE <= allocation.gid <= APP_UID_MAX:
            raise AppError(f"{path}: {app_id} gid must be in {APP_UID_BASE}-{APP_UID_MAX}")
        if not APP_PORT_OFFSET_MIN <= allocation.port_offset <= APP_PORT_OFFSET_MAX:
            raise AppError(
                f"{path}: {app_id} port_offset must be in "
                f"{APP_PORT_OFFSET_MIN}-{APP_PORT_OFFSET_MAX}"
            )
        if allocation.uid in seen_uids or allocation.gid in seen_gids or allocation.port_offset in seen_offsets:
            raise AppError(f"{path}: duplicate uid, gid, or port offset in app registry")
        seen_uids.add(allocation.uid)
        seen_gids.add(allocation.gid)
        seen_offsets.add(allocation.port_offset)
        registry[app_id] = allocation
    return registry


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


def _load_manifest(path: Path, *, allocation: AppAllocation | None, port: int) -> AppManifest:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AppError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AppError(f"{path}: manifest must be an object")
    package_dir = path.parent
    app_id = _required_string(data, "id", path)
    if not APP_ID_RE.fullmatch(app_id):
        raise AppError(f"{path}: id must match {APP_ID_RE.pattern}")
    if package_dir.name != app_id:
        raise AppError(f"{path}: manifest id must match package directory name")
    title = _required_string(data, "title", path)
    if "\n" in title or "\r" in title:
        raise AppError(f"{path}: title must be a single line")
    backend = _required_object(data, "backend", path)
    database = _required_object(data, "database", path)
    ui = _required_object(data, "ui", path)
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
        package_dir=package_dir,
        backend_entrypoint=backend_entrypoint,
        migrations_dir=migrations_dir,
        ui_dir=ui_dir,
        port=port,
        allocation=allocation,
    )
    _validate_postgres_identifier(app.db_schema, "database schema", path)
    _validate_postgres_identifier(app.db_role, "database role", path)
    return app


def _port_for_app_id(app_id: str, *, registry: dict[str, AppAllocation]) -> int:
    allocation = registry.get(app_id)
    if allocation is None:
        # Temp test roots may omit registry.json. Keep those fallback ports
        # independent of manifest scan order; deployed app ports come from the
        # root-owned registry and lifecycle bootstrap requires allocations.
        offset = 100 + sum((index + 1) * ord(char) for index, char in enumerate(app_id)) % 900
    else:
        offset = allocation.port_offset
    return APP_PORT_BASE + offset


def _required_int(data: dict[str, Any], key: str, path: Path) -> int:
    value = data.get(key)
    if not isinstance(value, int):
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
