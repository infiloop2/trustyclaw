"""Proxy-user policy writer: replace the active network policy.

Reads the replacement ``network_controls`` JSON from stdin, validates it,
writes the proxy-owned policy files, and prints the policy response JSON.
While the swap is in progress the status file says ``reloading``, which the
proxy treats as deny-all.

This is the only code that writes the policy files. The admin API can run it via
a narrow root-owned sudo helper that demotes to ``trustyclaw-proxy``, but cannot
write the files directly.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import json
import sys
import time
from pathlib import Path

from host.config import ConfigError, parse_network_controls
from host.runtime.network_policy import policy_path, save_policy, save_status, status_path
from host.runtime.state import network_policy_files, utc_now

POLICY_UPDATE_LOCK_TIMEOUT_SECONDS = 10


def main() -> int:
    try:
        parsed = parse_network_controls(json.load(sys.stdin))
        policy = parsed.to_json()
        updated_at = utc_now()
        with _policy_update_lock():
            save_status("reloading")
            _proxy_private(status_path())
            save_policy(policy, updated_at)
            _proxy_private(policy_path())
            save_status("active")
            _proxy_private(status_path())
        print(json.dumps({"network_controls": policy, "updated_at": updated_at}, sort_keys=True))
        return 0
    except (ConfigError, json.JSONDecodeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


@contextmanager
def _policy_update_lock() -> object:
    lock_path = network_policy_files().lock
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        deadline = time.monotonic() + POLICY_UPDATE_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for network policy update lock") from exc
                time.sleep(0.1)
        # The lock is held whenever the loop exits (a timeout raises out of the
        # enclosing `with`, which closing the file would release anyway).
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _proxy_private(path: Path) -> None:
    """Proxy state is read by admin only through the read-network-state helper."""
    path.chmod(0o600)


if __name__ == "__main__":
    raise SystemExit(main())
