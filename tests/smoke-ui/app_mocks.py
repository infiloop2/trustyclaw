"""Discover app-specific mock backends for admin UI smoke tests."""

from __future__ import annotations

from http import HTTPStatus
import importlib.util
from pathlib import Path
import re
import sys
from types import ModuleType
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from host.runtime import app_platform


ApiErrorFactory = Callable[[HTTPStatus, str], Exception]
HostApi = Callable[[str, str, dict[str, list[str]], Any], dict[str, Any]]
APP_API_RE = re.compile(r"^/v1/apps/([a-z][a-z0-9]*(?:_[a-z0-9]+)*)/api(?:/(.*))?$")
APP_SMOKE_ROOT = REPO_ROOT / "tests" / "apps"
_SMOKE_MODULES: dict[str, ModuleType | None] = {}


def route_app_api(
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: Any,
    api_error: ApiErrorFactory,
    host_api: HostApi,
) -> dict[str, Any] | None:
    match = APP_API_RE.fullmatch(path)
    if match is None:
        return None
    app_id = match.group(1)
    relative = match.group(2) or ""
    module = _load_app_smoke(app_id)
    handler = None if module is None else getattr(module, "route_app_api", None)
    if handler is None:
        raise api_error(HTTPStatus.NOT_FOUND, f"mock app service not found: {app_id}")
    return handler(method, relative, query, body, api_error, host_api)


def _load_app_smoke(app_id: str) -> ModuleType | None:
    if app_id in _SMOKE_MODULES:
        return _SMOKE_MODULES[app_id]
    app = app_platform.app_by_id(app_id)
    smoke_path = None if app is None else APP_SMOKE_ROOT / app.id / "smoke.py"
    if smoke_path is None or not smoke_path.is_file():
        _SMOKE_MODULES[app_id] = None
        return None
    module_name = f"trustyclaw_smoke_{app_id}"
    spec = importlib.util.spec_from_file_location(module_name, smoke_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load app smoke module: {smoke_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _SMOKE_MODULES[app_id] = module
    return module
