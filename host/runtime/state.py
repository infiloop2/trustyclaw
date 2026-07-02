"""Admin-state storage and the proxy-state file helpers.

Admin state — host config, the task queue, agent/task events, thread->session
maps, OAuth logins, provider account records — lives in the
local ``trustyclaw_admin`` Postgres database (see ``host/migrations/`` for the
schema and ``host.runtime.db``/``host.runtime.pgclient`` for how the service
connects). This module is the storage API: per-operation accessors that run
real queries against the normalized tables. Reads are plain lock-free
transactions (MVCC snapshots) that fetch only what the caller needs; writes go
through ``mutation()``, which pairs one database transaction with the
process-wide mutation lock below so check-then-act sequences stay atomic.

Idempotency-key replay records and agent runtime statuses deliberately do
not live here: idempotency is in-process memory in ``admin_api``, runtime
status is in-process memory in ``orchestrator`` (derived health, re-computed
within seconds of startup), and both reset with the service.

The network proxy participates under its own database role with narrow
grants: read-only on the policy and pin tables (which the admin service
writes after validation), plus insert/select/delete on its own
``network_events`` table. A database outage fails closed: the proxy denies
every request until the database returns. The proxy's TLS material (CA
keypair, minted leaf certificates) lives as proxy-owned files because ``ssl``
and ``openssl`` consume paths.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator

from host.runtime import db


DEFAULT_STATE_DIR = Path("/mnt/trustyclaw-admin/admin-state")
DEFAULT_PROXY_STATE_DIR = Path("/mnt/trustyclaw-admin/proxy-state")
# Serializes every admin-state write cycle. Private on purpose: writes go only
# through mutation() below, so the locking contract is enforced by structure.
# Three things to know:
# - It is in-process only. That is sufficient because the admin service is the
#   single writer of admin state, enforced by the port bind in admin_api.main()
#   (a second instance dies on the bind before touching state); the proxy
#   service has no database access at all. Postgres transactions make each
#   commit atomic, but this lock is what makes a whole check-then-act sequence
#   (read a status, decide, write) atomic against the other threads of this
#   process, without per-row lock ceremony.
# - It is an RLock so a helper that reads state can be called from inside a
#   mutation() block without deadlocking (reads run on their own database
#   connections and see the last committed state).
# - Nesting: code inside mutation() may take the JSONL lock below (network
#   event appends in tests) and the orchestrator's _POOL_LOCK (task claiming).
#   Nothing enters mutation() while holding either of those — keep it that
#   way, or the lock graph grows a cycle.
_MUTATION_LOCK = threading.RLock()
TASK_LIMIT = 5
EVENT_LIMIT = 5
# The event table keeps only the most recent MAX_EVENTS entries; the prune
# runs every PRUNE_EVERY appends so its cost stays amortized.
MAX_EVENTS = 1_000_000
PRUNE_EVERY = 500
# The network event table keeps the same generous cap as the agent events.
MAX_NETWORK_EVENTS = 1_000_000

_TASK_COLUMNS = (
    "number",
    "status",
    "agent_runtime",
    "thread_id",
    "input_message",
    "output_message",
    "error_message",
    "created_at",
    "updated_at",
)
_TASK_FIELDS = ", ".join(_TASK_COLUMNS)
ACTIVE_STATUSES_SQL = "('queued', 'running')"
TERMINAL_STATUSES_SQL = "('completed', 'failed', 'cancelled')"


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


@dataclass(frozen=True)
class NetworkProxyCertFiles:
    directory: Path
    cert: Path
    key: Path
    csr: Path
    ext: Path
    ca_cert: Path
    ca_key: Path


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


# -- transactions -------------------------------------------------------------


@contextmanager
def mutation() -> Iterator[Any]:
    """The single sanctioned way to write admin state: the process-wide
    mutation lock plus one database transaction, yielded as a cursor. The lock
    spans the whole check-then-act cycle so concurrent mutations cannot
    interleave between a read and its dependent write; an exception rolls the
    transaction back. Do slow work (runtime spawns, helper subprocesses,
    process closes) outside this block so reads never stall behind it. Plain
    reads use the read-only accessors below — no lock, their own snapshot."""
    with _MUTATION_LOCK:
        with db.transaction() as cur:
            yield cur


@contextmanager
def _read(cur: Any = None) -> Iterator[Any]:
    """Run with the given cursor (already inside a transaction) or a fresh
    read-only transaction."""
    if cur is not None:
        yield cur
        return
    with db.transaction() as fresh:
        yield fresh


# -- host config ---------------------------------------------------------------


def load_config() -> dict[str, Any]:
    """The host config as a dict: the singleton config row plus the operator
    connection rows, with absent values omitted."""
    config: dict[str, Any] = {}
    with db.transaction() as cur:
        cur.execute("SELECT agent_name, admin_password_sha256 FROM config")
        row = cur.fetchone()
        if row:
            if row[0] is not None:
                config["agent_name"] = row[0]
            if row[1] is not None:
                config["admin_password_sha256"] = row[1]
        cur.execute(
            "SELECT mode, ssh_public_key, hostname, tunnel_token"
            " FROM operator_connections ORDER BY mode"
        )
        connections = []
        for mode, ssh_public_key, hostname, tunnel_token in cur.fetchall():
            connection: dict[str, Any] = {"mode": mode}
            if ssh_public_key is not None:
                connection["ssh_public_key"] = ssh_public_key
            if hostname is not None:
                connection["hostname"] = hostname
            if tunnel_token is not None:
                connection["tunnel_token"] = tunnel_token
            connections.append(connection)
        if connections:
            config["operator_connections"] = connections
    return config


def save_config(config: dict[str, Any]) -> None:
    """Replace the whole host config, the way deploy refreshes it. The table
    constraints validate field formats and per-mode shapes; write_config
    performs the friendlier completeness validation before calling this."""
    with mutation() as cur:
        cur.execute("DELETE FROM operator_connections")
        cur.execute("DELETE FROM config")
        agent_name = config.get("agent_name")
        admin_password_sha256 = config.get("admin_password_sha256")
        if agent_name is not None or admin_password_sha256 is not None:
            cur.execute(
                "INSERT INTO config (agent_name, admin_password_sha256) VALUES (%s, %s)",
                (agent_name, admin_password_sha256),
            )
        for connection in config.get("operator_connections") or []:
            cur.execute(
                "INSERT INTO operator_connections (mode, ssh_public_key, hostname, tunnel_token)"
                " VALUES (%s, %s, %s, %s)",
                (
                    connection.get("mode"),
                    connection.get("ssh_public_key"),
                    connection.get("hostname"),
                    connection.get("tunnel_token"),
                ),
            )


# -- tasks ----------------------------------------------------------------------


def _task_from_row(row: Any) -> dict[str, Any]:
    task: dict[str, Any] = dict(zip(_TASK_COLUMNS, row[: len(_TASK_COLUMNS)]))
    task["task_id"] = f"task_{task.pop('number')}"
    return task


def _task_number(task_id: str) -> int | None:
    """The numeric task identity behind a public "task_N" id, or None for an
    id that cannot name a stored task."""
    prefix, _, tail = task_id.partition("_")
    if prefix != "task" or not tail.isdigit():
        return None
    return int(tail)


def allocate_task_number(cur: Any) -> int:
    """Return the next task number and advance the counter, atomically with
    the enclosing transaction (an abort rolls the counter back, so numbering
    stays dense)."""
    cur.execute(
        "INSERT INTO counters (name, value) VALUES ('next_task_number', 2)"
        " ON CONFLICT (name) DO UPDATE SET value = counters.value + 1"
        " RETURNING value - 1",
    )
    return int(cur.fetchone()[0])


def insert_task(cur: Any, task: dict[str, Any]) -> None:
    number = _task_number(str(task.get("task_id")))
    if number is None:
        raise ValueError(f"task_id must look like task_<number>: {task.get('task_id')!r}")
    cur.execute(
        f"INSERT INTO tasks ({_TASK_FIELDS})"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        [number] + [task.get(column) for column in _TASK_COLUMNS[1:]],
    )
    for message in task.get("steer_messages") or []:
        append_task_steer(cur, str(task.get("task_id")), message)


def save_task(cur: Any, task: dict[str, Any]) -> None:
    """Write back every mutable task field (matched by task_id). Steers are
    not part of the row; use the task_steers accessors."""
    cur.execute(
        "UPDATE tasks SET status = %s, agent_runtime = %s, thread_id = %s,"
        " input_message = %s, output_message = %s, error_message = %s,"
        " created_at = %s, updated_at = %s"
        " WHERE number = %s",
        [task.get(column) for column in _TASK_COLUMNS[1:]] + [_task_number(str(task["task_id"]))],
    )


def task_steers(task_id: str, cur: Any = None) -> list[str]:
    """The task's undelivered steer messages, oldest first."""
    with _read(cur) as cur:
        cur.execute(
            "SELECT message FROM task_steers WHERE task_number = %s ORDER BY id",
            (_task_number(task_id),),
        )
        return [row[0] for row in cur.fetchall()]


def pending_steer_count(cur: Any, task_id: str) -> int:
    cur.execute("SELECT count(*) FROM task_steers WHERE task_number = %s", (_task_number(task_id),))
    return int(cur.fetchone()[0])


def append_task_steer(cur: Any, task_id: str, message: str) -> None:
    cur.execute(
        "INSERT INTO task_steers (task_number, message) VALUES (%s, %s)",
        (_task_number(task_id), message),
    )


def pop_task_steer(cur: Any, task_id: str, message: str) -> None:
    """Drop the oldest undelivered steer if it is ``message`` (the one the
    worker just handed to the turn). Only the owning worker pops and the API
    only appends, so popping the head it delivered is race-free."""
    cur.execute(
        "DELETE FROM task_steers WHERE id = ("
        " SELECT id FROM task_steers WHERE task_number = %s ORDER BY id LIMIT 1)"
        " AND message = %s",
        (_task_number(task_id), message),
    )


def get_task(task_id: str, cur: Any = None) -> dict[str, Any] | None:
    """One task with its undelivered steers attached (the list accessors stay
    lean and skip steers; no list caller needs them)."""
    number = _task_number(task_id)
    if number is None:
        return None
    with _read(cur) as cur:
        cur.execute(f"SELECT {_TASK_FIELDS} FROM tasks WHERE number = %s", (number,))
        row = cur.fetchone()
        if row is None:
            return None
        task = _task_from_row(row)
        task["steer_messages"] = task_steers(task_id, cur)
    return task


def active_tasks(cur: Any = None) -> list[dict[str, Any]]:
    """Queued and running tasks in creation order."""
    with _read(cur) as cur:
        cur.execute(
            f"SELECT {_TASK_FIELDS} FROM tasks WHERE status IN {ACTIVE_STATUSES_SQL}"
            " ORDER BY number"
        )
        return [_task_from_row(row) for row in cur.fetchall()]


def running_tasks(cur: Any = None) -> list[dict[str, Any]]:
    with _read(cur) as cur:
        cur.execute(f"SELECT {_TASK_FIELDS} FROM tasks WHERE status = 'running' ORDER BY number")
        return [_task_from_row(row) for row in cur.fetchall()]


def queued_task_count(cur: Any) -> int:
    cur.execute("SELECT count(*) FROM tasks WHERE status = 'queued'")
    return int(cur.fetchone()[0])


def queued_tasks_brief(cur: Any) -> list[dict[str, Any]]:
    """Queued tasks in claim order, without their (potentially large)
    messages — the claim loop only needs identity and routing fields."""
    cur.execute(
        "SELECT number, agent_runtime, thread_id FROM tasks"
        " WHERE status = 'queued' ORDER BY number"
    )
    return [
        {"task_id": f"task_{number}", "agent_runtime": agent_runtime, "thread_id": thread_id}
        for number, agent_runtime, thread_id in cur.fetchall()
    ]


def tasks_for_thread(thread_id: str, limit: int) -> list[dict[str, Any]]:
    """A thread's tasks, most recently updated first (ties broken by newest)."""
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_TASK_FIELDS} FROM tasks WHERE thread_id = %s"
            " ORDER BY updated_at DESC, number DESC LIMIT %s",
            (thread_id, limit),
        )
        return [_task_from_row(row) for row in cur.fetchall()]


def thread_task_runtime(cur: Any, thread_id: str) -> str | None:
    """The runtime already bound to this thread by any existing task."""
    cur.execute("SELECT agent_runtime FROM tasks WHERE thread_id = %s LIMIT 1", (thread_id,))
    row = cur.fetchone()
    return str(row[0]) if row else None


def thread_summaries() -> list[dict[str, Any]]:
    """Per-thread aggregates over the whole task history plus the active task
    list, for the threads listing. Message columns are never fetched."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT thread_id, min(agent_runtime), max(updated_at), count(*)"
            " FROM tasks GROUP BY thread_id"
        )
        summaries = {
            str(thread_id): {
                "thread_id": str(thread_id),
                "agent_runtime": agent_runtime,
                "last_used_at": last_used_at or "",
                "active_tasks": [],
                "task_count": int(count),
            }
            for thread_id, agent_runtime, last_used_at, count in cur.fetchall()
        }
        cur.execute(
            "SELECT thread_id, number, status FROM tasks"
            f" WHERE status IN {ACTIVE_STATUSES_SQL} ORDER BY number"
        )
        for thread_id, number, status in cur.fetchall():
            summaries[str(thread_id)]["active_tasks"].append(
                {"task_id": f"task_{number}", "status": status}
            )
    return list(summaries.values())


def fail_running_tasks(cur: Any, error_message: str, runtime: str | None = None) -> list[str]:
    """Fail every running task (optionally only one runtime's) and return the
    affected task ids; the caller records the per-task events in the same
    transaction."""
    sql = (
        "UPDATE tasks SET status = 'failed', error_message = %s, updated_at = %s"
        " WHERE status = 'running'"
    )
    params: list[Any] = [error_message, utc_now()]
    if runtime is not None:
        sql += " AND agent_runtime = %s"
        params.append(runtime)
    cur.execute(sql + " RETURNING number", params)
    return [f"task_{row[0]}" for row in cur.fetchall()]


def prune_finished_tasks(cur: Any, keep: int) -> None:
    """Drop the oldest finished tasks beyond ``keep`` (active tasks are never
    pruned). Recency follows the task history order: updated_at, then task
    creation order."""
    cur.execute(
        f"DELETE FROM tasks WHERE status IN {TERMINAL_STATUSES_SQL} AND number NOT IN ("
        f" SELECT number FROM tasks WHERE status IN {TERMINAL_STATUSES_SQL}"
        "  ORDER BY updated_at DESC, number DESC LIMIT %s)",
        (keep,),
    )


# -- thread -> provider session maps ---------------------------------------------


def thread_session(cur: Any, runtime: str, thread_id: str) -> dict[str, Any] | None:
    cur.execute(
        "SELECT provider_session_id, last_used_at FROM thread_sessions"
        " WHERE agent_runtime = %s AND thread_id = %s",
        (runtime, thread_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"provider_session_id": row[0], "last_used_at": row[1]}


def save_thread_session(
    cur: Any,
    runtime: str,
    thread_id: str,
    provider_session_id: str | None,
    last_used_at: str | None,
) -> None:
    cur.execute(
        "INSERT INTO thread_sessions (agent_runtime, thread_id, provider_session_id, last_used_at)"
        " VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (agent_runtime, thread_id) DO UPDATE SET"
        " provider_session_id = EXCLUDED.provider_session_id,"
        " last_used_at = EXCLUDED.last_used_at",
        (runtime, thread_id, provider_session_id, last_used_at),
    )


def thread_session_runtime(cur: Any, thread_id: str) -> str | None:
    cur.execute(
        "SELECT agent_runtime FROM thread_sessions WHERE thread_id = %s LIMIT 1",
        (thread_id,),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


def session_summaries() -> list[tuple[str, str, str]]:
    """(runtime, thread_id, last_used_at) for every mapping, for the threads
    listing."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT agent_runtime, thread_id, COALESCE(last_used_at, '') FROM thread_sessions"
        )
        return [(str(r), str(t), str(u)) for r, t, u in cur.fetchall()]


def prune_thread_sessions(cur: Any, runtime: str, keep: int) -> None:
    """Drop a runtime's least recently used thread mappings beyond ``keep``. A
    dropped thread is not an error — a later task on it starts a fresh runtime
    conversation."""
    cur.execute(
        "DELETE FROM thread_sessions WHERE agent_runtime = %s AND thread_id NOT IN ("
        " SELECT thread_id FROM thread_sessions WHERE agent_runtime = %s"
        "  ORDER BY last_used_at DESC NULLS LAST, thread_id LIMIT %s)",
        (runtime, runtime, keep),
    )


# -- OAuth logins -------------------------------------------------------------------


_OAUTH_COLUMNS = ("status", "login_url", "expires_at", "device_code", "login_id")


def oauth_login(key: str, cur: Any = None) -> dict[str, Any] | None:
    """The in-flight login record for ``codex`` or ``claude``, or None."""
    with _read(cur) as cur:
        cur.execute(
            f"SELECT {', '.join(_OAUTH_COLUMNS)} FROM oauth_logins WHERE runtime = %s", (key,)
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {column: value for column, value in zip(_OAUTH_COLUMNS, row) if value is not None}


def set_oauth_login(cur: Any, key: str, data: dict[str, Any] | None) -> None:
    if data is None:
        cur.execute("DELETE FROM oauth_logins WHERE runtime = %s", (key,))
        return
    unknown = set(data) - set(_OAUTH_COLUMNS)
    if unknown:
        raise ValueError(f"unsupported oauth login keys: {sorted(unknown)}")
    cur.execute(
        "INSERT INTO oauth_logins (runtime, status, login_url, expires_at, device_code, login_id)"
        " VALUES (%s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (runtime) DO UPDATE SET status = EXCLUDED.status,"
        " login_url = EXCLUDED.login_url, expires_at = EXCLUDED.expires_at,"
        " device_code = EXCLUDED.device_code, login_id = EXCLUDED.login_id",
        (key, *(data.get(column) for column in _OAUTH_COLUMNS)),
    )


# -- provider account records ---------------------------------------------------------


def save_openai_account(account: dict[str, Any] | None) -> None:
    _save_provider_account("openai", account if account is not None else {"account_id": None})


def read_openai_account() -> dict[str, Any]:
    value = _read_provider_account("openai")
    return value if isinstance(value, dict) else {}


def save_claude_account(account: dict[str, Any] | None) -> None:
    _save_provider_account("claude", account or {})


def read_claude_account() -> dict[str, Any]:
    value = _read_provider_account("claude")
    return value if isinstance(value, dict) else {}


def _save_provider_account(provider: str, data: dict[str, Any]) -> None:
    # account_id is a typed column; the rest is the provider CLI's own shape,
    # cached verbatim as metadata.
    metadata = {key: value for key, value in data.items() if key != "account_id"}
    with mutation() as cur:
        cur.execute(
            "INSERT INTO provider_accounts (provider, account_id, metadata) VALUES (%s, %s, %s)"
            " ON CONFLICT (provider) DO UPDATE SET account_id = EXCLUDED.account_id,"
            " metadata = EXCLUDED.metadata",
            (provider, data.get("account_id"), db.jsonb(metadata)),
        )


def _read_provider_account(provider: str) -> dict[str, Any]:
    with db.transaction() as cur:
        cur.execute(
            "SELECT account_id, metadata FROM provider_accounts WHERE provider = %s", (provider,)
        )
        row = cur.fetchone()
    if row is None:
        return {}
    account: dict[str, Any] = dict(row[1]) if isinstance(row[1], dict) else {}
    if row[0] is not None:
        account["account_id"] = row[0]
    return account


# -- agent events -------------------------------------------------------------------


# The typed event payload fields; every event the runtime emits uses a subset.
_EVENT_PAYLOAD_COLUMNS = ("message", "source", "error_message", "agent_runtime")
_EVENT_FIELDS = "seq, created_at, event_type, task_id, " + ", ".join(_EVENT_PAYLOAD_COLUMNS)


def _event_dict(row: Any) -> dict[str, Any]:
    seq, created_at, event_type, task_id = row[:4]
    payload = {
        column: value for column, value in zip(_EVENT_PAYLOAD_COLUMNS, row[4:]) if value is not None
    }
    return {
        "seq": int(seq),
        "timestamp": created_at,
        "event_id": f"event_{seq}",
        "event_type": event_type,
        "task_id": task_id,
        "payload": payload,
    }


def append_agent_event(cur: Any, event_type: str, task_id: str | None, payload: dict[str, Any]) -> int:
    """Insert one event inside the caller's mutation transaction, so the event
    commits or rolls back with the state change that caused it. seq is a
    serial: unique and increasing, with harmless gaps from aborted
    transactions. Payload keys map to the typed columns; an unknown key is a
    programming error and fails loudly."""
    unknown = set(payload) - set(_EVENT_PAYLOAD_COLUMNS)
    if unknown:
        raise ValueError(f"unsupported event payload keys: {sorted(unknown)}")
    cur.execute(
        "INSERT INTO agent_events (created_at, event_type, task_id, message, source,"
        " error_message, agent_runtime) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING seq",
        (utc_now(), event_type, task_id, *(payload.get(column) for column in _EVENT_PAYLOAD_COLUMNS)),
    )
    seq = int(cur.fetchone()[0])
    if seq % PRUNE_EVERY == 0:
        prune_agent_events(cur)
    return seq


def record_agent_event(event_type: str, task_id: str | None, payload: dict[str, Any]) -> None:
    """Append one event in its own transaction (for callers with no
    surrounding state change, like agent-message streaming)."""
    with mutation() as cur:
        append_agent_event(cur, event_type, task_id, payload)


def read_agent_events() -> list[dict[str, Any]]:
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_EVENT_FIELDS} FROM agent_events ORDER BY seq"
        )
        return [_event_dict(row) for row in cur.fetchall()]


def prune_agent_events(cur: Any = None) -> None:
    with _read(cur) as cur:
        # seq is a serial, so newest-N retention is a primary-key range
        # delete below MAX(seq) - N: two index-endpoint lookups and the excess
        # rows, instead of scanning N index entries per prune. Seq gaps from
        # aborted transactions only make retention keep slightly fewer rows.
        cur.execute(
            "DELETE FROM agent_events WHERE"
            " seq <= (SELECT COALESCE(MAX(seq), 0) FROM agent_events) - %s",
            (MAX_EVENTS,),
        )


@dataclass(frozen=True)
class Page:
    items: list[dict[str, Any]]


def page_agent_events(since: int | None) -> Page:
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_EVENT_FIELDS} FROM agent_events"
            " WHERE seq > %s ORDER BY seq LIMIT %s",
            (since if since is not None else 0, EVENT_LIMIT),
        )
        return Page([_event_dict(row) for row in cur.fetchall()])


def page_task_events(task_id: str, since: int | None) -> Page:
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_EVENT_FIELDS} FROM agent_events"
            " WHERE task_id = %s AND seq > %s ORDER BY seq LIMIT %s",
            (task_id, since if since is not None else 0, EVENT_LIMIT),
        )
        return Page([_event_dict(row) for row in cur.fetchall()])


# -- network policy and proxy account pins (admin writes, proxy reads) ---------------


def network_policy_record() -> dict[str, Any] | None:
    """The stored policy assembled back into the operator-facing shape:
    ``{"controls": ..., "updated_at": ...}``, or None when nothing was ever
    stored (the fail-closed empty default)."""
    with db.transaction() as cur:
        cur.execute("SELECT updated_at FROM network_policy")
        row = cur.fetchone()
        if row is None:
            return None
        updated_at = row[0]
        cur.execute("SELECT provider FROM managed_provider_access")
        managed = {str(provider): True for (provider,) in cur.fetchall()}
        allowed: dict[str, dict[str, Any]] = {}
        cur.execute("SELECT domain FROM allowed_domains ORDER BY domain")
        for (domain,) in cur.fetchall():
            allowed[str(domain)] = {"allow_http_methods": []}
        cur.execute("SELECT domain, method FROM domain_methods ORDER BY domain, position")
        for domain, method in cur.fetchall():
            allowed[str(domain)]["allow_http_methods"].append(method)
        cur.execute("SELECT domain, pattern FROM domain_path_guards ORDER BY domain, position")
        for domain, pattern in cur.fetchall():
            allowed[str(domain)].setdefault("path_guards", []).append(pattern)
    return {
        "controls": {
            "managed_ai_provider_network_access": managed,
            "allowed_network_access": allowed,
        },
        "updated_at": updated_at,
    }


def save_network_policy(controls: dict[str, Any], updated_at: str) -> None:
    """Replace the active policy in one transaction (admin service only; the
    proxy role can only read these tables). ``controls`` is the already
    validated operator-facing shape from host.config."""
    with mutation() as cur:
        cur.execute("DELETE FROM domain_path_guards")
        cur.execute("DELETE FROM domain_methods")
        cur.execute("DELETE FROM allowed_domains")
        cur.execute("DELETE FROM managed_provider_access")
        cur.execute(
            "INSERT INTO network_policy (singleton, updated_at) VALUES (TRUE, %s)"
            " ON CONFLICT (singleton) DO UPDATE SET updated_at = EXCLUDED.updated_at",
            (updated_at,),
        )
        managed = controls.get("managed_ai_provider_network_access") or {}
        for provider, enabled in managed.items():
            if enabled is True:
                cur.execute("INSERT INTO managed_provider_access (provider) VALUES (%s)", (provider,))
        for domain, rule in (controls.get("allowed_network_access") or {}).items():
            cur.execute("INSERT INTO allowed_domains (domain) VALUES (%s)", (domain,))
            for position, method in enumerate(rule.get("allow_http_methods") or []):
                cur.execute(
                    "INSERT INTO domain_methods (domain, position, method) VALUES (%s, %s, %s)",
                    (domain, position, method),
                )
            for position, pattern in enumerate(rule.get("path_guards") or []):
                cur.execute(
                    "INSERT INTO domain_path_guards (domain, position, pattern) VALUES (%s, %s, %s)",
                    (domain, position, pattern),
                )


def save_proxy_openai_account_id(account_id: str | None) -> None:
    _save_proxy_pin("openai", {"account_id": account_id})


def read_proxy_openai_account_id() -> str | None:
    value = _read_proxy_pin("openai").get("account_id")
    return value if isinstance(value, str) and value else None


def save_proxy_claude_account(account: dict[str, Any] | None) -> None:
    _save_proxy_pin("claude", account or {})


def read_proxy_claude_account() -> dict[str, Any]:
    return _read_proxy_pin("claude")


def _save_proxy_pin(provider: str, data: dict[str, Any]) -> None:
    # Exactly the two values the guards compare; the proxy never receives the
    # rest of the account metadata.
    account_id = data.get("account_id")
    access_token_sha256 = data.get("access_token_sha256")
    with mutation() as cur:
        cur.execute(
            "INSERT INTO proxy_provider_pins (provider, account_id, access_token_sha256)"
            " VALUES (%s, %s, %s)"
            " ON CONFLICT (provider) DO UPDATE SET account_id = EXCLUDED.account_id,"
            " access_token_sha256 = EXCLUDED.access_token_sha256",
            (provider, account_id, access_token_sha256),
        )


def _read_proxy_pin(provider: str) -> dict[str, Any]:
    with db.transaction() as cur:
        cur.execute(
            "SELECT account_id, access_token_sha256 FROM proxy_provider_pins"
            " WHERE provider = %s",
            (provider,),
        )
        row = cur.fetchone()
    if row is None:
        return {}
    pin: dict[str, Any] = {}
    if row[0] is not None:
        pin["account_id"] = row[0]
    if row[1] is not None:
        pin["access_token_sha256"] = row[1]
    return pin


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
    """Record one allow/deny decision. Runs in the proxy process under its own
    database role (granted exactly the network_events table). A failure
    surfaces to the caller's connection handler: a decision that cannot be
    logged fails that request, never the proxy itself — fail closed."""
    # Field caps keep the row cap a real disk bound: the agent's own request
    # stream feeds this log, and headers allow multi-kilobyte URLs.
    host = host[:512]
    path = path[:2048]
    query = query[:2048]
    if reason is not None:
        reason = reason[:512]
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO network_events (created_at, protocol, method, host, port, path,"
            " query, decision, reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING seq",
            (
                utc_now(),
                protocol,
                method,
                host,
                port,
                path,
                query,
                "allowed" if allowed else "denied",
                reason,
            ),
        )
        row = cur.fetchone()
        assert row is not None  # INSERT ... RETURNING always yields one row
        seq = int(row[0])
        if seq % PRUNE_EVERY == 0:
            prune_network_events(cur)


def prune_network_events(cur: Any = None) -> None:
    """Same O(1)-planning range delete as prune_agent_events; this runs on
    the proxy's request path (every PRUNE_EVERY appends), so it must never
    scan the whole retained log."""
    with _read(cur) as cur:
        cur.execute(
            "DELETE FROM network_events WHERE"
            " seq <= (SELECT COALESCE(MAX(seq), 0) FROM network_events) - %s",
            (MAX_NETWORK_EVENTS,),
        )


def _network_event_dict(row: Any) -> dict[str, Any]:
    seq, created_at, protocol, method, host, port, path, query, decision, reason = row
    event: dict[str, Any] = {
        "seq": int(seq),
        "timestamp": created_at,
        "protocol": protocol,
        "method": method,
        "host": host,
        "port": int(port),
        "path": path,
        "query": query,
        "decision": decision,
    }
    if reason is not None:
        event["reason"] = reason
    return event


_NETWORK_EVENT_FIELDS = "seq, created_at, protocol, method, host, port, path, query, decision, reason"


def read_network_events() -> list[dict[str, Any]]:
    with db.transaction() as cur:
        cur.execute(f"SELECT {_NETWORK_EVENT_FIELDS} FROM network_events ORDER BY seq")
        return [_network_event_dict(row) for row in cur.fetchall()]


def page_network_events(since: int | None) -> Page:
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_NETWORK_EVENT_FIELDS} FROM network_events"
            " WHERE seq > %s ORDER BY seq LIMIT %s",
            (since if since is not None else 0, EVENT_LIMIT),
        )
        return Page([_network_event_dict(row) for row in cur.fetchall()])
