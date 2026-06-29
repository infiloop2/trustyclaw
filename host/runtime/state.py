from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, TypedDict


DEFAULT_STATE_DIR = Path("/mnt/trustyclaw-admin/admin-state")
DEFAULT_PROXY_STATE_DIR = Path("/mnt/trustyclaw-admin/proxy-state")
# Guards every state.json access. Private on purpose: runtime code reads and
# writes state only through read_state() and state_update() below, so the
# locking contract is enforced by structure rather than by convention at each
# call site. Three things to know:
# - It is in-process only. That is sufficient because the admin service is the
#   single writer of state.json, enforced by the port bind in admin_api.main()
#   (a second instance dies on the bind before touching state); the proxy
#   service never writes state.json.
# - It is an RLock so a helper that reads state can be called from inside a
#   state_update() block without deadlocking.
# - Nesting: code inside state_update() may take the JSONL lock below (event
#   appends) and the orchestrator's _POOL_LOCK (task claiming). Nothing enters
#   state_update() while holding either of those — keep it that way, or the
#   lock graph grows a cycle.
_STATE_LOCK = threading.RLock()
TASK_LIMIT = 5
EVENT_LIMIT = 5
# Event files keep only the most recent MAX_EVENTS entries; the prune runs
# every PRUNE_EVERY appends so its cost stays amortized.
MAX_EVENTS = 10_000
PRUNE_EVERY = 500
# The JSONL event files have separate Unix owners now:
# - events.jsonl is admin-state and is touched only by the admin service.
# - network_events.jsonl is proxy-state. The admin service cannot read it
#   directly; admin API reads go through the narrow read-network-state sudo
#   helper, which demotes to trustyclaw-proxy before opening the file.
#
# network_events.jsonl is therefore still touched by more than one process with
# permission: the long-lived proxy service appends/prunes it, and short-lived
# proxy-user read helpers page it for the admin API. Cross-process safety comes
# from the fcntl flock in locked_jsonl(); file locks are owned by the process,
# so they exclude the other process but not reliably the other threads of your
# own — that is what these in-process locks are for. One lock per file path
# (each process holds its own instances; they share nothing across processes)
# keeps unrelated event files from queueing behind each other.
_JSONL_THREAD_LOCKS: dict[str, threading.RLock] = {}
_JSONL_THREAD_LOCKS_GUARD = threading.Lock()
# The proxy service derives network-event seq values from network_events.jsonl
# instead of state.json, so proxy threads need one in-process lock around
# read-max/increment/append.
_NETWORK_EVENT_LOCK = threading.Lock()
class _NetworkEventSeq(TypedDict):
    path: Path | None
    seq: int


_NETWORK_EVENT_SEQ: _NetworkEventSeq = {"path": None, "seq": 0}


def _jsonl_thread_lock(path: Path) -> threading.RLock:
    with _JSONL_THREAD_LOCKS_GUARD:
        return _JSONL_THREAD_LOCKS.setdefault(str(path), threading.RLock())


def _state_dir() -> Path:
    return Path(os.environ.get("TRUSTYCLAW_STATE_DIR", str(DEFAULT_STATE_DIR)))


def _proxy_state_dir() -> Path:
    if "TRUSTYCLAW_PROXY_STATE_DIR" in os.environ:
        return Path(os.environ["TRUSTYCLAW_PROXY_STATE_DIR"])
    if "TRUSTYCLAW_STATE_DIR" in os.environ:
        # Tests and local single-process harnesses commonly redirect one state
        # directory. Keep that override self-contained unless a proxy-state
        # override is provided explicitly.
        return _state_dir()
    return DEFAULT_PROXY_STATE_DIR


def _config_path() -> Path:
    return _state_dir() / "config.json"


def _state_path() -> Path:
    return _state_dir() / "state.json"


def _events_path() -> Path:
    return _state_dir() / "events.jsonl"


def _network_events_path() -> Path:
    return _proxy_state_dir() / "network_events.jsonl"


@dataclass(frozen=True)
class NetworkPolicyFiles:
    controls: Path
    lock: Path


@dataclass(frozen=True)
class NetworkProxyCertFiles:
    directory: Path
    cert: Path
    key: Path
    csr: Path
    ext: Path
    ca_cert: Path
    ca_key: Path


def network_policy_files() -> NetworkPolicyFiles:
    return NetworkPolicyFiles(
        controls=_proxy_state_dir() / "network_controls.json",
        lock=_proxy_state_dir() / ".network_policy.lock",
    )


def network_proxy_cert_files(host: str) -> NetworkProxyCertFiles:
    safe_host = "".join(char if char.isalnum() or char in ".-" else "_" for char in host)
    directory = _proxy_state_dir() / "generated-certs"
    return NetworkProxyCertFiles(
        directory=directory,
        cert=directory / f"{safe_host}.crt",
        key=directory / f"{safe_host}.key",
        csr=directory / f"{safe_host}.csr",
        ext=directory / f"{safe_host}.ext",
        ca_cert=_proxy_state_dir() / "network_proxy_ca.crt",
        ca_key=_proxy_state_dir() / "network_proxy_ca.key",
    )


def read_network_proxy_ca_cert() -> bytes:
    return (_proxy_state_dir() / "network_proxy_ca.crt").read_bytes()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def load_config() -> dict[str, Any]:
    return load_json(_config_path(), {})


def load_state() -> dict[str, Any]:
    default = {
        "agent_runtime_statuses": {
            "codex": {"status": "loading"},
            "claude_code": {"status": "loading"},
        },
        "tasks": [],
        "next_task_number": 1,
        "next_event_seq": 1,
        "codex_threads": {},
        "claude_sessions": {},
        "codex_oauth": None,
        "claude_oauth": None,
        "idempotency": {},
    }
    state = load_json(_state_path(), default)
    for key, value in default.items():
        state.setdefault(key, value)
    statuses = state.get("agent_runtime_statuses")
    if not isinstance(statuses, dict):
        statuses = {}
        state["agent_runtime_statuses"] = statuses
    for runtime in ("codex", "claude_code"):
        current = statuses.get(runtime)
        if not isinstance(current, dict):
            statuses[runtime] = {"status": "loading"}
        else:
            current.setdefault("status", "loading")
    return state


def save_state(state: dict[str, Any]) -> None:
    write_json(_state_path(), state)


def _openai_account_path() -> Path:
    return _state_dir() / "openai_account.json"


def _claude_account_path() -> Path:
    return _state_dir() / "claude_account.json"


def _proxy_openai_account_path() -> Path:
    return _proxy_state_dir() / "openai_account.json"


def _proxy_claude_account_path() -> Path:
    return _proxy_state_dir() / "claude_account.json"


def save_openai_account_id(account_id: str | None) -> None:
    path = _openai_account_path()
    write_json(path, {"account_id": account_id})
    path.chmod(0o600)


def read_openai_account_id() -> str | None:
    value = load_json(_openai_account_path(), {}).get("account_id")
    return value if isinstance(value, str) and value else None


def save_claude_account(account: dict[str, Any] | None) -> None:
    path = _claude_account_path()
    write_json(path, account or {})
    path.chmod(0o600)


def read_claude_account() -> dict[str, Any]:
    value = load_json(_claude_account_path(), {})
    return value if isinstance(value, dict) else {}


def save_proxy_openai_account_id(account_id: str | None) -> None:
    path = _proxy_openai_account_path()
    write_json(path, {"account_id": account_id})
    path.chmod(0o600)


def read_proxy_openai_account_id() -> str | None:
    value = load_json(_proxy_openai_account_path(), {}).get("account_id")
    return value if isinstance(value, str) and value else None


def save_proxy_claude_account(account: dict[str, Any] | None) -> None:
    path = _proxy_claude_account_path()
    write_json(path, account or {})
    path.chmod(0o600)


def read_proxy_claude_account() -> dict[str, Any]:
    value = load_json(_proxy_claude_account_path(), {})
    return value if isinstance(value, dict) else {}


@contextmanager
def state_update() -> Any:
    """The single sanctioned read-modify-write for state.json: lock, load,
    yield the state dict, and save it on normal exit. The lock spans the whole
    cycle, so concurrent updates cannot interleave between load and save. An
    exception skips the save. Do slow work (runtime spawns, helper subprocesses,
    process closes) outside this block so reads never stall behind it.

    A transaction that ends with the state unchanged skips the save, so
    polling paths (the worker claim loop, early returns) do not rewrite the
    file every few seconds for nothing.

    The bare load_state()/save_state() helpers are for single-threaded callers
    only (tests, the bootstrap); runtime code goes through here or
    read_state()."""
    with _STATE_LOCK:
        state = load_state()
        before = json.dumps(state, sort_keys=True)
        yield state
        if json.dumps(state, sort_keys=True) != before:
            save_state(state)


def read_state() -> dict[str, Any]:
    """A read-only snapshot of state.json. The returned dict is the caller's
    own copy; mutations to it are not persisted (use state_update() for
    that)."""
    with _STATE_LOCK:
        return load_state()


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with locked_jsonl(path):
        _append_jsonl_unlocked(path, value)


def _append_jsonl_unlocked(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with locked_jsonl(path):
        return _read_jsonl_unlocked(path)


def _read_jsonl_unlocked(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    values: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                values.append(value)
    return values


def prune_jsonl(path: Path, max_lines: int = MAX_EVENTS) -> None:
    with locked_jsonl(path):
        _prune_jsonl_unlocked(path, max_lines)


def _prune_jsonl_unlocked(path: Path, max_lines: int = MAX_EVENTS) -> None:
    values = _read_jsonl_unlocked(path)
    if len(values) <= max_lines:
        return
    mode = path.stat()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            for value in values[-max_lines:]:
                handle.write(json.dumps(value, sort_keys=True) + "\n")
        os.chmod(tmp, mode.st_mode & 0o7777)
        try:
            os.chown(tmp, mode.st_uid, mode.st_gid)
        except (PermissionError, OSError):
            pass  # only root can restore ownership; tests run unprivileged
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def locked_jsonl(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _jsonl_thread_lock(path):
        while True:
            handle = path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                # Pruning replaces the file by rename. flock binds to the inode,
                # not the path, so if another process pruned while we waited we
                # now hold a lock on the orphaned inode and a write would be
                # lost. Re-check and retry against the live file.
                if os.fstat(handle.fileno()).st_ino == path.stat().st_ino:
                    break
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            except BaseException:
                handle.close()
                raise
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def next_seq(state: dict[str, Any], key: str) -> int:
    seq = int(state.get(key, 1))
    state[key] = seq + 1
    return seq


def _append_event(path: Path, seq: int, fields: dict[str, Any]) -> None:
    """Append one sequenced event to a JSONL log and prune on the shared
    cadence. Callers own how ``seq`` is derived: agent events use the state
    counter (inside state_update()); the proxy service derives it from the file,
    since it cannot touch state.json. Centralizing the append+timestamp+prune
    keeps the two writers from drifting."""
    with locked_jsonl(path):
        _append_jsonl_unlocked(path, {"seq": seq, "timestamp": utc_now(), **fields})
        if seq % PRUNE_EVERY == 0:
            _prune_jsonl_unlocked(path)


def append_agent_event(state: dict[str, Any], event_type: str, task_id: str | None, payload: dict[str, Any]) -> None:
    """Only called with the live dict inside a state_update() block, which is
    the one place such a dict can legitimately come from — so the state lock is
    always held here."""
    seq = next_seq(state, "next_event_seq")
    # Persist the advanced counter before the event reaches the log. A crash
    # between the two leaves a harmless gap in seq numbers; the reverse order
    # would reuse a seq after restart, and duplicate seqs break since-based
    # event pagination. Callers mutate state before appending events, so the
    # intermediate snapshot saved here is always consistent.
    save_state(state)
    _append_event(
        _events_path(),
        seq,
        {
            "event_id": f"event_{seq}",
            "event_type": event_type,
            "task_id": task_id,
            "payload": payload,
        },
    )


def append_network_event(
    protocol: str,
    method: str,
    host: str,
    port: int,
    path: str,
    query: str,
    allowed: bool,
    reason: str | None = None,
) -> None:
    with _NETWORK_EVENT_LOCK:
        # The proxy is a separate (root) process from the admin API, so it
        # cannot use state.json's counter; it derives the next seq from the
        # file's current max instead.
        event_path = _network_events_path()
        if _NETWORK_EVENT_SEQ["path"] != event_path:
            existing = read_network_events()
            _NETWORK_EVENT_SEQ["path"] = event_path
            _NETWORK_EVENT_SEQ["seq"] = max((int(event["seq"]) for event in existing), default=0)
        _NETWORK_EVENT_SEQ["seq"] += 1
        fields = {
            "protocol": protocol,
            "method": method,
            "host": host,
            "port": port,
            "path": path,
            "query": query,
            "decision": "allowed" if allowed else "denied",
        }
        if reason:
            fields["reason"] = reason
        _append_event(event_path, int(_NETWORK_EVENT_SEQ["seq"]), fields)


def read_agent_events() -> list[dict[str, Any]]:
    return read_jsonl(_events_path())


def read_network_events() -> list[dict[str, Any]]:
    return read_jsonl(_network_events_path())


def prune_agent_events() -> None:
    prune_jsonl(_events_path())


@dataclass(frozen=True)
class Page:
    items: list[dict[str, Any]]


def _page_after_seq(
    items: list[dict[str, Any]], since: int | None, match: Callable[[dict[str, Any]], bool] | None = None
) -> Page:
    values: list[dict[str, Any]] = []
    for item in items:
        try:
            seq = int(item["seq"])
        except (KeyError, TypeError, ValueError):
            continue
        if (match is None or match(item)) and (since is None or seq > since):
            values.append(item)
    values.sort(key=lambda item: int(item["seq"]))
    return Page(values[:EVENT_LIMIT])


def page_agent_events(since: int | None) -> Page:
    return _page_after_seq(read_agent_events(), since)


def page_task_events(task_id: str, since: int | None) -> Page:
    return _page_after_seq(read_agent_events(), since, match=lambda event: event.get("task_id") == task_id)


def page_network_events(since: int | None) -> Page:
    return _page_after_seq(read_network_events(), since)
