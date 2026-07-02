from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any


VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
REPO_VERSION_PATH = Path(__file__).resolve().parents[1] / "VERSION"
ROOT_VERSION_PATH = Path(os.environ.get("TRUSTYCLAW_ROOT_VERSION_PATH", "/opt/trustyclaw-host/VERSION"))
STATE_VERSION_FILENAME = "version.json"
# Admin state moved from JSON files into Postgres in 0.5.0, deliberately
# without a data migration; older preserved state cannot be upgraded in place.
# Enforced twice: in CLI preflight from the EC2 version tag hint (before the
# existing instance is terminated) and authoritatively by bootstrap from the
# admin disk's version.json (before preserved state is modified).
MIN_STATE_VERSION = "0.5.0"


def parse_version(version: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(version.strip())
    if not match:
        raise ValueError(f"invalid TrustyClaw version {version!r}; expected MAJOR.MINOR.PATCH")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def compare_versions(left: str, right: str) -> int:
    left_tuple = parse_version(left)
    right_tuple = parse_version(right)
    return (left_tuple > right_tuple) - (left_tuple < right_tuple)


def repo_version(path: Path = REPO_VERSION_PATH) -> str:
    return _validate_version(path.read_text().strip())


def state_version_path(state_dir: Path | None = None) -> Path:
    if state_dir is None:
        state_dir = Path(os.environ.get("TRUSTYCLAW_STATE_DIR", "/mnt/trustyclaw-admin/admin-state"))
    return state_dir / STATE_VERSION_FILENAME


def read_root_version(path: Path = ROOT_VERSION_PATH) -> str | None:
    try:
        return _validate_version(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def read_state_version(path: Path | None = None) -> str | None:
    version_path = path or state_version_path()
    try:
        payload: Any = json.loads(version_path.read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
        return None
    try:
        return _validate_version(payload["version"])
    except ValueError:
        return None


def version_status() -> dict[str, str | None]:
    root = read_root_version()
    state = read_state_version()
    if root is None or state is None:
        status = "error"
    elif root == state:
        status = "ok"
    else:
        status = "mismatch"
    return {"status": status, "runtime": root, "state": state}


def _validate_version(value: str) -> str:
    parse_version(value)
    return value
