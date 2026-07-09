"""Discover and run per-app Playwright smoke tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from host.runtime import app_platform


APP_SMOKE_ROOT = REPO_ROOT / "tests" / "apps"
_SMOKE_MODULES: dict[str, ModuleType] = {}


def desktop_smoke(page: Any) -> None:
    _run_app_smokes("desktop_smoke", page)


def mobile_smoke(page: Any) -> None:
    _run_app_smokes("mobile_smoke", page)


def _run_app_smokes(function_name: str, page: Any) -> None:
    for app, module in _iter_app_smokes():
        smoke = getattr(module, function_name, None)
        if smoke is None:
            raise AssertionError(f"{app.id} smoke.py is missing {function_name}()")
        smoke(page)


def _iter_app_smokes() -> Iterator[tuple[app_platform.AppManifest, ModuleType]]:
    for app in app_platform.installed_apps():
        yield app, _load_app_smoke(app)


def _load_app_smoke(app: app_platform.AppManifest) -> ModuleType:
    if app.id in _SMOKE_MODULES:
        return _SMOKE_MODULES[app.id]
    smoke_path = APP_SMOKE_ROOT / app.id / "smoke.py"
    if not smoke_path.is_file():
        raise AssertionError(f"{app.id} is missing app smoke module {smoke_path}")
    module_name = f"trustyclaw_smoke_{app.id}"
    spec = importlib.util.spec_from_file_location(module_name, smoke_path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load app smoke module: {smoke_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _SMOKE_MODULES[app.id] = module
    return module
