"""Periodic discovery of newer TrustyClaw releases.

The admin service has no egress. A fixed root helper fetches the public
repository's ``VERSION`` file, then this module validates and compares the
result. The last successful result is process-local and deliberately
disposable across service restarts.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Any

from host.version import compare_versions, read_root_version


CHECK_INTERVAL_SECONDS = 4 * 60 * 60
HELPER_TIMEOUT_SECONDS = 15
HELPER_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/check-for-upgrade"]

_lock = threading.Lock()
_latest_version: str | None = None


def status() -> dict[str, Any]:
    current = read_root_version()
    with _lock:
        latest = _latest_version
    available = False
    if current is None:
        latest = None
    elif latest is not None:
        try:
            available = compare_versions(latest, current) > 0
        except ValueError:
            latest = None
    return {"available": available, "latest": latest}


def refresh() -> None:
    """Replace the cached result after one successful helper check.

    Failure preserves the last successful result. Host health and every other
    admin concern remain independent from this advisory network check.
    """
    latest: str | None = None
    try:
        proc = subprocess.run(
            HELPER_COMMAND,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=HELPER_TIMEOUT_SECONDS,
        )
        if proc.returncode == 0:
            candidate = proc.stdout.strip()
            compare_versions(candidate, candidate)
            latest = candidate
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    if latest is not None:
        with _lock:
            global _latest_version
            _latest_version = latest


def poll() -> None:
    while True:
        refresh()
        time.sleep(CHECK_INTERVAL_SECONDS)
