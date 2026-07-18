"""Normalized host-state accessors and proxy file-path helpers.

Host, network, app-migration, and tool state lives in the local
``trustyclaw_admin`` Postgres database (see ``host/migrations/`` for the schema
and ``host.runtime.core.db``/``host.runtime.core.pgclient`` for the Unix-socket client).
This module exposes per-operation queries rather than materializing the full
state. Reads use MVCC snapshots; process-local check-then-act writes use
``mutation()``, while cross-process transitions rely on database constraints
and conditional updates.

Agent runtime statuses deliberately do not live here: runtime status is
in-process memory in ``orchestrator`` (derived health, re-computed within
seconds of startup) and resets with the service.

The proxy and tools services participate under narrow database roles. A
database outage fails closed: the proxy denies every request until the
database returns. Proxy TLS material stays in proxy-owned files because
``ssl`` and OpenSSL consume paths.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import hmac
import secrets
import threading
import time
from typing import Any, Iterator

from host.runtime.core import db, secretbox


DEFAULT_PROXY_STATE_DIR = Path("/mnt/trustyclaw-admin/proxy-state")
# Serializes every admin-state write cycle. Private on purpose: writes go only
# through mutation() below, so the locking contract is enforced by structure.
# Three things to know:
# - It is in-process only. The admin port bind permits one admin process, while
#   the proxy and tools processes reach only their granted operations. Postgres
#   transactions and conditional updates carry cross-process correctness; this
#   lock makes a whole check-then-act sequence atomic against sibling threads.
# - It is an RLock so a helper that reads state can be called from inside a
#   mutation() block without deadlocking (reads run on their own database
#   connections and see the last committed state).
# - Nesting: code inside mutation() may take the orchestrator's _LIVE_LOCK
#   (task claiming). Nothing enters mutation() while holding it — keep it
#   that way, or the lock graph grows a cycle.
_MUTATION_LOCK = threading.RLock()
TASK_LIMIT = 5
EVENT_LIMIT = 5
# Every audit log (agent events, network events, tool events) keeps only the
# most recent MAX_EVENTS entries; the prune runs every PRUNE_EVERY appends so
# its cost stays amortized.
MAX_EVENTS = 1_000_000
PRUNE_EVERY = 500
# The audit logs page newest-first in pages of EVENT_PAGE_LIMIT rows; the
# limit query parameter can only shrink a page.
EVENT_PAGE_LIMIT = 100

_TASK_COLUMNS = (
    "number",
    "status",
    "thread_id",
    "input_message",
    "output_message",
    "error_message",
    "created_at",
    "updated_at",
)
_TASK_FIELDS = ", ".join(_TASK_COLUMNS)
_TASK_WITH_SESSION_COLUMNS = (*_TASK_COLUMNS, "agent_runtime", "model", "effort")
_TASK_SELECT_FIELDS = ", ".join(f"tasks.{column}" for column in _TASK_COLUMNS) + (
    ", thread_sessions.agent_runtime, thread_sessions.model, thread_sessions.effort"
)
_TASK_SESSION_JOIN = (
    " JOIN thread_sessions ON thread_sessions.thread_id = tasks.thread_id"
)
ACTIVE_STATUSES_SQL = "('queued', 'running')"
TERMINAL_STATUSES_SQL = "('completed', 'failed', 'cancelled')"


def _proxy_state_dir() -> Path:
    return Path(os.environ.get("TRUSTYCLAW_PROXY_STATE_DIR", str(DEFAULT_PROXY_STATE_DIR)))


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
                connection["tunnel_token"] = secretbox.decrypt(tunnel_token)
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
                    _encrypt_secret(connection.get("tunnel_token")),
                ),
            )


# -- tasks ----------------------------------------------------------------------


def _task_from_row(row: Any) -> dict[str, Any]:
    task: dict[str, Any] = dict(zip(_TASK_WITH_SESSION_COLUMNS, row))
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
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        [number] + [task.get(column) for column in _TASK_COLUMNS[1:]],
    )
    for message in task.get("steer_messages") or []:
        append_task_steer(cur, str(task.get("task_id")), message)


def save_task(cur: Any, task: dict[str, Any]) -> None:
    """Write back every mutable task field (matched by task_id). Steers are
    not part of the row; use the task_steers accessors."""
    cur.execute(
        "UPDATE tasks SET status = %s, thread_id = %s,"
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
        cur.execute(
            f"SELECT {_TASK_SELECT_FIELDS} FROM tasks{_TASK_SESSION_JOIN}"
            " WHERE tasks.number = %s",
            (number,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        task = _task_from_row(row)
        task["steer_messages"] = task_steers(task_id, cur)
    return task


def active_tasks() -> list[dict[str, Any]]:
    """Queued and running tasks in creation order."""
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_TASK_SELECT_FIELDS} FROM tasks{_TASK_SESSION_JOIN}"
            f" WHERE tasks.status IN {ACTIVE_STATUSES_SQL} ORDER BY tasks.number"
        )
        return [_task_from_row(row) for row in cur.fetchall()]


def running_tasks(cur: Any = None) -> list[dict[str, Any]]:
    with _read(cur) as cur:
        cur.execute(
            f"SELECT {_TASK_SELECT_FIELDS} FROM tasks{_TASK_SESSION_JOIN}"
            " WHERE tasks.status = 'running' ORDER BY tasks.number"
        )
        return [_task_from_row(row) for row in cur.fetchall()]


def queued_task_count(cur: Any) -> int:
    cur.execute("SELECT count(*) FROM tasks WHERE status = 'queued'")
    return int(cur.fetchone()[0])


def queued_tasks_brief(cur: Any) -> list[dict[str, Any]]:
    """Queued tasks in claim order, without their (potentially large)
    messages — the claim loop only needs identity and routing fields."""
    cur.execute(
        "SELECT tasks.number, thread_sessions.agent_runtime, tasks.thread_id"
        f" FROM tasks{_TASK_SESSION_JOIN}"
        " WHERE tasks.status = 'queued' ORDER BY tasks.number"
    )
    return [
        {"task_id": f"task_{number}", "agent_runtime": agent_runtime, "thread_id": thread_id}
        for number, agent_runtime, thread_id in cur.fetchall()
    ]


def tasks_for_thread(thread_id: str, limit: int) -> list[dict[str, Any]]:
    """A thread's tasks, most recently updated first (ties broken by newest)."""
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_TASK_SELECT_FIELDS} FROM tasks{_TASK_SESSION_JOIN}"
            " WHERE tasks.thread_id = %s"
            " ORDER BY tasks.updated_at DESC, tasks.number DESC LIMIT %s",
            (thread_id, limit),
        )
        return [_task_from_row(row) for row in cur.fetchall()]


def thread_summaries() -> list[dict[str, Any]]:
    """Canonical thread configuration plus retained task aggregates."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT thread_sessions.thread_id, thread_sessions.agent_runtime,"
            " thread_sessions.model, thread_sessions.effort,"
            " GREATEST(COALESCE(thread_sessions.last_used_at, ''),"
            " COALESCE(max(tasks.updated_at), '')), count(tasks.number)"
            " FROM thread_sessions LEFT JOIN tasks"
            " ON tasks.thread_id = thread_sessions.thread_id"
            " GROUP BY thread_sessions.thread_id, thread_sessions.agent_runtime,"
            " thread_sessions.model, thread_sessions.effort, thread_sessions.last_used_at"
        )
        summaries = {
            str(thread_id): {
                "thread_id": str(thread_id),
                "agent_runtime": agent_runtime,
                "model": model,
                "effort": effort,
                "last_used_at": last_used_at or "",
                "active_tasks": [],
                "task_count": int(count),
            }
            for thread_id, agent_runtime, model, effort, last_used_at, count in cur.fetchall()
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
        sql += " AND thread_id IN (SELECT thread_id FROM thread_sessions WHERE agent_runtime = %s)"
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
        "SELECT provider_session_id, last_used_at, model, effort FROM thread_sessions"
        " WHERE agent_runtime = %s AND thread_id = %s",
        (runtime, thread_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "provider_session_id": row[0],
        "last_used_at": row[1],
        "model": str(row[2]),
        "effort": str(row[3]),
    }


def save_thread_session(
    cur: Any,
    runtime: str,
    thread_id: str,
    provider_session_id: str | None,
    last_used_at: str | None,
    model: str,
    effort: str,
) -> None:
    cur.execute(
        "INSERT INTO thread_sessions (agent_runtime, thread_id, provider_session_id, last_used_at, model, effort)"
        " VALUES (%s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (thread_id) DO UPDATE SET"
        " provider_session_id = EXCLUDED.provider_session_id,"
        " last_used_at = EXCLUDED.last_used_at"
        " WHERE thread_sessions.agent_runtime = EXCLUDED.agent_runtime"
        " AND thread_sessions.model = EXCLUDED.model"
        " AND thread_sessions.effort = EXCLUDED.effort"
        " RETURNING 1",
        (runtime, thread_id, provider_session_id, last_used_at, model, effort),
    )
    if cur.fetchone() is None:
        raise ValueError(f"thread {thread_id!r} already has another session configuration")


def thread_session_config(cur: Any, thread_id: str) -> dict[str, Any] | None:
    cur.execute(
        "SELECT agent_runtime, provider_session_id, last_used_at, model, effort"
        " FROM thread_sessions WHERE thread_id = %s",
        (thread_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "agent_runtime": str(row[0]),
        "provider_session_id": row[1],
        "last_used_at": row[2],
        "model": str(row[3]),
        "effort": str(row[4]),
    }


def prune_thread_sessions(cur: Any, runtime: str, keep: int) -> None:
    """Drop least-recently-used unreferenced threads beyond ``keep``.

    Retained tasks keep their canonical thread row; once task history pruning
    removes the last reference, the ordinary LRU cap applies.
    """
    cur.execute(
        "DELETE FROM thread_sessions AS candidate"
        " WHERE candidate.agent_runtime = %s"
        " AND NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.thread_id = candidate.thread_id)"
        " AND candidate.thread_id NOT IN ("
        "  SELECT retained.thread_id FROM thread_sessions AS retained"
        "  WHERE retained.agent_runtime = %s"
        "  AND NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.thread_id = retained.thread_id)"
        "  ORDER BY retained.last_used_at DESC NULLS LAST, retained.thread_id LIMIT %s)",
        (runtime, runtime, keep),
    )


# -- OAuth logins -------------------------------------------------------------------


_OAUTH_COLUMNS = ("status", "login_url", "expires_at", "device_code", "login_id", "access_token_sha256")


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
        "INSERT INTO oauth_logins (runtime, status, login_url, expires_at, device_code, login_id, access_token_sha256)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (runtime) DO UPDATE SET status = EXCLUDED.status,"
        " login_url = EXCLUDED.login_url, expires_at = EXCLUDED.expires_at,"
        " device_code = EXCLUDED.device_code, login_id = EXCLUDED.login_id,"
        " access_token_sha256 = EXCLUDED.access_token_sha256",
        (key, *(data.get(column) for column in _OAUTH_COLUMNS)),
    )


# -- provider account records ---------------------------------------------------------


def save_openai_account(account: dict[str, Any] | None, cur: Any = None) -> None:
    _save_provider_account("openai", account if account is not None else {"account_id": None}, cur)


def read_openai_account(cur: Any = None) -> dict[str, Any]:
    value = _read_provider_account("openai", cur)
    return value if isinstance(value, dict) else {}


def save_claude_account(account: dict[str, Any] | None, cur: Any = None) -> None:
    _save_provider_account("claude", account or {}, cur)


def read_claude_account(cur: Any = None) -> dict[str, Any]:
    value = _read_provider_account("claude", cur)
    return value if isinstance(value, dict) else {}


def _save_provider_account(provider: str, data: dict[str, Any], cur: Any = None) -> None:
    # account_id is a typed column; the rest is the provider CLI's own shape,
    # cached verbatim as metadata.
    if cur is None:
        with mutation() as fresh:
            _save_provider_account(provider, data, fresh)
        return
    metadata = {key: value for key, value in data.items() if key != "account_id"}
    cur.execute(
        "INSERT INTO provider_accounts (provider, account_id, metadata) VALUES (%s, %s, %s)"
        " ON CONFLICT (provider) DO UPDATE SET account_id = EXCLUDED.account_id,"
        " metadata = EXCLUDED.metadata",
        (provider, data.get("account_id"), db.jsonb(metadata)),
    )


def _read_provider_account(provider: str, cur: Any = None) -> dict[str, Any]:
    with _read(cur) as cur:
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


def _prune_events(cur: Any, table: str) -> None:
    # Shared retention for the three audit logs (agent, network, tool events).
    # seq is a serial, so newest-N retention is a primary-key range
    # delete below MAX(seq) - N: two index-endpoint lookups and the excess
    # rows, instead of scanning N index entries per prune. Seq gaps from
    # aborted transactions only make retention keep slightly fewer rows.
    # ``table`` is always a module-constant name, never external input.
    cur.execute(
        f"DELETE FROM {table} WHERE"
        f" seq <= (SELECT COALESCE(MAX(seq), 0) FROM {table}) - %s",
        (MAX_EVENTS,),
    )


def _page_before(
    table: str,
    fields: str,
    row_fn: Any,
    before: int | None,
    limit: int,
    extra_clause: str | None = None,
    extra_params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    """One newest-first page of an audit log: rows with ``seq < before`` (all
    rows when ``before`` is None). ``table``/``fields``/``extra_clause`` are
    module constants, never external input."""
    clauses = list(() if extra_clause is None else (extra_clause,))
    params: list[Any] = list(extra_params)
    if before is not None:
        clauses.append("seq < %s")
        params.append(before)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {fields} FROM {table}{where} ORDER BY seq DESC LIMIT %s",
            tuple(params) + (limit,),
        )
        return [row_fn(row) for row in cur.fetchall()]


def prune_agent_events(cur: Any) -> None:
    _prune_events(cur, "agent_events")


def page_agent_events_before(
    before: int | None, *, limit: int = EVENT_PAGE_LIMIT
) -> list[dict[str, Any]]:
    return _page_before("agent_events", _EVENT_FIELDS, _event_dict, before, limit)


def page_task_events(task_id: str, since: int | None) -> list[dict[str, Any]]:
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_EVENT_FIELDS} FROM agent_events"
            " WHERE task_id = %s AND seq > %s ORDER BY seq LIMIT %s",
            (task_id, since if since is not None else 0, EVENT_LIMIT),
        )
        return [_event_dict(row) for row in cur.fetchall()]


def page_thread_events(thread_id: str, since: int | None, limit: int) -> list[dict[str, Any]]:
    """One oldest-first page of a thread's task events: rows with
    ``seq > since`` across every task in the thread, so a chat surface can
    accumulate the full message stream incrementally."""
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_EVENT_FIELDS} FROM agent_events"
            " WHERE task_id IN (SELECT 'task_' || number FROM tasks WHERE thread_id = %s)"
            " AND seq > %s ORDER BY seq LIMIT %s",
            (thread_id, since if since is not None else 0, limit),
        )
        return [_event_dict(row) for row in cur.fetchall()]


# -- network policy and proxy account pins (admin writes, proxy reads) ---------------


def network_policy_record() -> dict[str, Any] | None:
    """The stored policy assembled back into the operator-facing shape:
    ``{"controls": ..., "updated_at": ...}``, or None when nothing was ever
    stored (the fail-closed empty default)."""
    with db.transaction() as cur:
        # One snapshot for every SELECT: under the default READ COMMITTED a
        # concurrent policy replace could commit between statements and this
        # read would recombine two policies (for example old allowed methods
        # with new missing path guards).
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur.execute("SELECT updated_at FROM network_policy")
        row = cur.fetchone()
        if row is None:
            return None
        updated_at = row[0]
        cur.execute("SELECT integration FROM managed_integrations")
        integrations: dict[str, dict[str, Any]] = {
            str(integration): {"enabled": True} for (integration,) in cur.fetchall()
        }
        cur.execute("SELECT owner, repo FROM github_repositories ORDER BY position")
        write_repositories = [{"owner": str(owner), "repo": str(repo)} for owner, repo in cur.fetchall()]
        # Validation guarantees write repositories exist only while GitHub is
        # enabled, so a row without the enabled integration is unreachable.
        if write_repositories and "github" in integrations:
            integrations["github"]["write_repositories"] = write_repositories
        cur.execute("SELECT require_dot_github_approval FROM github_settings")
        settings_row = cur.fetchone()
        if settings_row and settings_row[0] and "github" in integrations:
            integrations["github"]["require_dot_github_approval"] = True
        cur.execute("SELECT web_search FROM claude_settings")
        claude_row = cur.fetchone()
        if claude_row and claude_row[0] and "claude" in integrations:
            integrations["claude"]["web_search"] = True
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
        if allowed:
            integrations["custom"] = {"domains": allowed}
    return {
        "controls": {"network_integrations": integrations},
        "updated_at": updated_at,
    }


def read_claude_web_search() -> bool:
    """Whether the operator enabled Anthropic server-side web search for Claude
    Code. Read by the orchestrator to tell the root launcher whether to expose
    the WebSearch tool; the proxy enforces the same toggle independently. Any
    read failure returns False (fail closed — web search stays off)."""
    try:
        with db.transaction() as cur:
            cur.execute("SELECT web_search FROM claude_settings")
            row = cur.fetchone()
            return bool(row and row[0])
    except Exception:
        return False


def save_network_policy(controls: dict[str, Any], updated_at: str) -> None:
    """Replace the active policy in one transaction (admin service only; the
    proxy role can only read these tables). ``controls`` is the already
    validated operator-facing shape from host.config."""
    with mutation() as cur:
        cur.execute("DELETE FROM domain_path_guards")
        cur.execute("DELETE FROM domain_methods")
        cur.execute("DELETE FROM allowed_domains")
        cur.execute("DELETE FROM github_repositories")
        cur.execute("DELETE FROM github_settings")
        cur.execute("DELETE FROM claude_settings")
        cur.execute("DELETE FROM managed_integrations")
        cur.execute(
            "INSERT INTO network_policy (singleton, updated_at) VALUES (TRUE, %s)"
            " ON CONFLICT (singleton) DO UPDATE SET updated_at = EXCLUDED.updated_at",
            (updated_at,),
        )
        integrations = controls.get("network_integrations") or {}
        for integration, value in integrations.items():
            if integration == "custom":
                continue  # custom's domains live in the domain tables below
            if isinstance(value, dict) and value.get("enabled") is True:
                cur.execute("INSERT INTO managed_integrations (integration) VALUES (%s)", (integration,))
        github = integrations.get("github")
        if isinstance(github, dict):
            for position, repository in enumerate(github.get("write_repositories") or []):
                cur.execute(
                    "INSERT INTO github_repositories (position, owner, repo) VALUES (%s, %s, %s)",
                    (position, repository["owner"], repository["repo"]),
                )
            if github.get("require_dot_github_approval") is True:
                cur.execute(
                    "INSERT INTO github_settings (singleton, require_dot_github_approval) VALUES (TRUE, TRUE)"
                )
        claude = integrations.get("claude")
        if isinstance(claude, dict) and claude.get("web_search") is True:
            cur.execute("INSERT INTO claude_settings (singleton, web_search) VALUES (TRUE, TRUE)")
        custom = integrations.get("custom")
        custom_domains = custom.get("domains") if isinstance(custom, dict) else {}
        for domain, rule in (custom_domains or {}).items():
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


def save_proxy_openai_account_id(account_id: str | None, cur: Any = None) -> None:
    _save_proxy_pin("openai", {"account_id": account_id}, cur)


def read_proxy_openai_account_id() -> str | None:
    value = _read_proxy_pin("openai").get("account_id")
    return value if isinstance(value, str) and value else None


def save_proxy_claude_account(account: dict[str, Any] | None, cur: Any = None) -> None:
    _save_proxy_pin("claude", account or {}, cur)


def read_proxy_claude_account() -> dict[str, Any]:
    return _read_proxy_pin("claude")


def _save_proxy_pin(provider: str, data: dict[str, Any], cur: Any = None) -> None:
    # Exactly the two values the guards compare (account_id and
    # access_token_sha256); the proxy never receives the rest of the account
    # metadata. Callers inside a mutation pass their cursor so the pin commits
    # atomically with the anchor/status change.
    if cur is None:
        with mutation() as fresh:
            _save_proxy_pin(provider, data, fresh)
        return
    account_id = data.get("account_id")
    access_token_sha256 = data.get("access_token_sha256")
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


# -- github credential (admin only; the proxy role has no grant on it) ----------------


_GITHUB_CREDENTIAL_COLUMNS = (
    "mode",
    "token",
    "app_id",
    "installation_id",
    "private_key_pem",
    "updated_at",
    "validation",
)
_GITHUB_CREDENTIAL_SECRET_COLUMNS = ("token", "private_key_pem")


def _encrypt_secret(value: Any) -> str | None:
    """Encrypt a secret column value. Secrets are either absent or non-empty
    strings — anything else is a programming error, refused loudly rather
    than ever stored unencrypted."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("stored secrets must be non-empty strings")
    return secretbox.encrypt(value)


def read_github_credential() -> dict[str, Any]:
    with db.transaction() as cur:
        cur.execute(f"SELECT {', '.join(_GITHUB_CREDENTIAL_COLUMNS)} FROM github_credential")
        row = cur.fetchone()
    if row is None:
        return {}
    credential = {column: value for column, value in zip(_GITHUB_CREDENTIAL_COLUMNS, row) if value is not None}
    for column in _GITHUB_CREDENTIAL_SECRET_COLUMNS:
        if isinstance(credential.get(column), str):
            credential[column] = secretbox.decrypt(credential[column])
    return credential


def save_github_credential(credential: dict[str, Any] | None) -> None:
    """Replace or clear the single fixed GitHub credential row."""
    with mutation() as cur:
        cur.execute("DELETE FROM github_credential")
        if credential is None:
            return
        cur.execute(
            f"INSERT INTO github_credential (singleton, {', '.join(_GITHUB_CREDENTIAL_COLUMNS)})"
            f" VALUES (TRUE, {', '.join(['%s'] * len(_GITHUB_CREDENTIAL_COLUMNS))})",
            tuple(
                db.jsonb(credential.get(column) or {})
                if column == "validation"
                else _encrypt_secret(credential.get(column))
                if column in _GITHUB_CREDENTIAL_SECRET_COLUMNS
                else credential.get(column)
                for column in _GITHUB_CREDENTIAL_COLUMNS
            ),
        )


def set_github_credential_validation(validation: dict[str, Any]) -> None:
    with mutation() as cur:
        cur.execute("UPDATE github_credential SET validation = %s", (db.jsonb(validation),))


# -- proxy github token (the proxy's working copy; SELECT grant) ----------------------


def save_proxy_github_token(token: str | None, expires_at: str | None = None) -> None:
    """Replace or clear the proxy's working copy of the active GitHub token —
    the only copy: ``expires_at`` (app mode; None for a PAT) is what reconcile
    checks to re-mint in time. Stored as secretbox ciphertext like every other
    secret; the proxy role holds SELECT on this row and on secret_keys (see
    migration 0002), which together decrypt exactly this working set and
    nothing else."""
    ciphertext = _encrypt_secret(token)
    with mutation() as cur:
        cur.execute("DELETE FROM proxy_github_token")
        if ciphertext is not None:
            cur.execute(
                "INSERT INTO proxy_github_token (singleton, token, expires_at, updated_at) VALUES (TRUE, %s, %s, %s)",
                (ciphertext, expires_at, utc_now()),
            )


_proxy_github_token_cache: tuple[str, str] | None = None


def read_proxy_github_token() -> str | None:
    """The active token the proxy injects, or None while GitHub is disabled
    or no credential is stored. Runs under the proxy role (SELECT grant)."""
    record = read_proxy_github_token_record()
    return record["token"] if record else None


def read_proxy_github_token_record() -> dict[str, Any] | None:
    """The working-token row (``token`` decrypted, plus ``expires_at``), or
    None when nothing is published. Reconcile reads the expiry to decide
    whether the published app token still has margin or must be re-minted."""
    global _proxy_github_token_cache
    with db.transaction() as cur:
        cur.execute("SELECT token, expires_at FROM proxy_github_token")
        row = cur.fetchone()
    if not row:
        return None
    ciphertext, expires_at = str(row[0]), row[1]
    cached = _proxy_github_token_cache
    if cached is not None and cached[0] == ciphertext:
        token = cached[1]
    else:
        token = secretbox.decrypt(ciphertext)
        _proxy_github_token_cache = (ciphertext, token)
    return {"token": token, "expires_at": expires_at}


# -- github repository audits (admin only; no proxy grant) ----------------------------


def save_github_repo_audit(owner: str, repo: str, facts: dict[str, Any], error: str | None) -> None:
    """Upsert one repository's audit facts (or the fetch error)."""
    with mutation() as cur:
        cur.execute(
            "INSERT INTO github_repo_audit (owner, repo, fetched_at, facts, error)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (owner, repo) DO UPDATE SET"
            " fetched_at = EXCLUDED.fetched_at, facts = EXCLUDED.facts, error = EXCLUDED.error",
            (owner, repo, utc_now(), db.jsonb(facts), error),
        )


def read_github_repo_audits() -> dict[tuple[str, str], dict[str, Any]]:
    """All stored audits keyed by (owner, repo):
    ``{"fetched_at": ..., "facts": {...}, "error": ...?}``."""
    with db.transaction() as cur:
        cur.execute("SELECT owner, repo, fetched_at, facts, error FROM github_repo_audit")
        rows = cur.fetchall()
    audits: dict[tuple[str, str], dict[str, Any]] = {}
    for owner, repo, fetched_at, facts, error in rows:
        audit: dict[str, Any] = {"fetched_at": fetched_at, "facts": facts if isinstance(facts, dict) else {}}
        if error is not None:
            audit["error"] = str(error)
        audits[(str(owner), str(repo))] = audit
    return audits


def prune_github_repo_audits(keep: set[tuple[str, str]]) -> None:
    """Drop audits for repositories no longer in the policy."""
    with mutation() as cur:
        cur.execute("SELECT owner, repo FROM github_repo_audit")
        for owner, repo in cur.fetchall():
            if (str(owner), str(repo)) not in keep:
                cur.execute("DELETE FROM github_repo_audit WHERE owner = %s AND repo = %s", (owner, repo))


# -- github .github push-approval gate (pending_pushes) ------------------------
# The proxy enqueues a row when a gated push touches .github/ (INSERT grant);
# the admin service lists and resolves them.

def enqueue_pending_push(
    push_id: str,
    owner: str,
    repo: str,
    ref_updates: list[dict[str, str]],
    changed_paths: list[str],
) -> None:
    with mutation() as cur:
        cur.execute(
            "INSERT INTO pending_pushes (id, owner, repo, ref_updates, changed_paths, requested_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (push_id, owner, repo, db.jsonb(ref_updates), db.jsonb(changed_paths), utc_now()),
        )


def _pending_push_row(row: tuple[Any, ...]) -> dict[str, Any]:
    push_id, owner, repo, ref_updates, changed_paths, requested_at, status, resolved_at, detail = row
    value: dict[str, Any] = {
        "id": str(push_id),
        "owner": str(owner),
        "repo": str(repo),
        "ref_updates": ref_updates if isinstance(ref_updates, list) else [],
        "changed_paths": changed_paths if isinstance(changed_paths, list) else [],
        "requested_at": requested_at,
        "status": str(status),
    }
    if resolved_at is not None:
        value["resolved_at"] = resolved_at
    if detail is not None:
        value["detail"] = str(detail)
    return value


_PENDING_PUSH_COLUMNS = "id, owner, repo, ref_updates, changed_paths, requested_at, status, resolved_at, detail"


def read_pending_pushes() -> list[dict[str, Any]]:
    """Pending pushes, newest first."""
    with db.transaction() as cur:
        cur.execute(f"SELECT {_PENDING_PUSH_COLUMNS} FROM pending_pushes ORDER BY requested_at DESC")
        return [_pending_push_row(row) for row in cur.fetchall()]


def get_pending_push(push_id: str) -> dict[str, Any] | None:
    with db.transaction() as cur:
        cur.execute(f"SELECT {_PENDING_PUSH_COLUMNS} FROM pending_pushes WHERE id = %s", (push_id,))
        row = cur.fetchone()
    return _pending_push_row(row) if row else None


def resolve_pending_push(push_id: str, status: str, detail: str | None = None) -> dict[str, Any]:
    """Mark a pending push resolved (approved/rejected/failed) with an optional
    detail message, and return the resolved row. The caller
    (push_gate.pending) holds RESOLVE_LOCK and has just read the row as
    pending, so the conditional update always matches; a vanished row would be
    a programming error and fails loudly."""
    with mutation() as cur:
        cur.execute(
            "UPDATE pending_pushes SET status = %s, resolved_at = %s, detail = %s"
            " WHERE id = %s AND status = 'pending'"
            f" RETURNING {_PENDING_PUSH_COLUMNS}",
            (status, utc_now(), detail or None, push_id),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"pending push {push_id} vanished mid-resolve")
    return _pending_push_row(row)


def read_github_credential_metadata() -> dict[str, Any]:
    credential = read_github_credential()
    mode = credential.get("mode")
    if mode not in ("pat", "app"):
        return {"configured": False}
    value: dict[str, Any] = {"configured": True, "mode": mode}
    if isinstance(credential.get("updated_at"), str):
        value["updated_at"] = credential["updated_at"]
    if mode == "app":
        if isinstance(credential.get("app_id"), str):
            value["app_id"] = credential["app_id"]
        if isinstance(credential.get("installation_id"), str):
            value["installation_id"] = credential["installation_id"]
        published = read_proxy_github_token_record()
        if published and isinstance(published.get("expires_at"), str):
            value["app_token_expires_at"] = published["expires_at"]
    validation = credential.get("validation")
    value["validation"] = validation if isinstance(validation, dict) else {"status": "not_checked"}
    return value


def append_network_event(
    protocol: str,
    method: str,
    host: str,
    port: int,
    path: str,
    query: str,
    allowed: bool,
    reason_code: str | None = None,
) -> None:
    """Record one allow/deny decision. Runs in the proxy process under its own
    database role, whose event-table grant permits this operation. A failure
    surfaces to the caller's connection handler: a decision that cannot be
    logged fails that request, never the proxy itself — fail closed."""
    # Field caps keep the row cap a real disk bound: the agent's own request
    # stream feeds this log, and headers allow multi-kilobyte URLs.
    host = host[:512]
    path = path[:2048]
    query = query[:2048]
    if reason_code is not None:
        reason_code = reason_code[:128]
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO network_events (created_at, protocol, method, host, port, path,"
            " query, decision, reason_code) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
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
                reason_code,
            ),
        )
        row = cur.fetchone()
        assert row is not None  # INSERT ... RETURNING always yields one row
        seq = int(row[0])
        if seq % PRUNE_EVERY == 0:
            prune_network_events(cur)


def prune_network_events(cur: Any) -> None:
    """Runs on the proxy's request path (every PRUNE_EVERY appends), so it
    must never scan the whole retained log — see _prune_events."""
    _prune_events(cur, "network_events")


def _network_event_dict(row: Any) -> dict[str, Any]:
    seq, created_at, protocol, method, host, port, path, query, decision, reason_code = row
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
    if reason_code is not None:
        event["reason_code"] = reason_code
    return event


_NETWORK_EVENT_FIELDS = "seq, created_at, protocol, method, host, port, path, query, decision, reason_code"


def page_network_events_before(
    before: int | None,
    *,
    decision: str | None = None,
    limit: int = EVENT_PAGE_LIMIT,
) -> list[dict[str, Any]]:
    extra = ("decision = %s", (decision,)) if decision is not None else (None, ())
    return _page_before(
        "network_events", _NETWORK_EVENT_FIELDS, _network_event_dict, before, limit,
        extra_clause=extra[0], extra_params=extra[1],
    )


# -- tools ---------------------------------------------------------------------


# Public "approval_<number>" ids, like "task_<number>" for tasks.
_APPROVAL_ID_PREFIX = "approval_"
_TOOL_APPROVAL_FIELDS = "number, tool_id, action_id, status, summary, payload, check_token, result, created_at, decided_at"


class PendingToolApprovalLimitReached(Exception):
    """Raised when inserting another pending tool approval would exceed the cap."""


def enabled_tool_ids() -> set[str]:
    with db.transaction() as cur:
        cur.execute("SELECT tool_id FROM enabled_tools")
        return {row[0] for row in cur.fetchall()}


def set_tool_enabled(cur: Any, tool_id: str, enabled: bool) -> None:
    if enabled:
        cur.execute(
            "INSERT INTO enabled_tools (tool_id) VALUES (%s) ON CONFLICT (tool_id) DO NOTHING",
            (tool_id,),
        )
    else:
        cur.execute("DELETE FROM enabled_tools WHERE tool_id = %s", (tool_id,))


def tool_config_keys(tool_id: str) -> set[str]:
    """The configured key names for one tool; values stay in the database
    except for tool_config_values callers building a tool call's config view."""
    with db.transaction() as cur:
        cur.execute("SELECT key FROM tool_config WHERE tool_id = %s", (tool_id,))
        return {row[0] for row in cur.fetchall()}


def tool_config_values(tool_id: str, keys: list[str]) -> dict[str, str]:
    """Configured values for one tool's manifest keys. Config is scoped per
    tool, so a shared key name resolves to this tool's own value. Values are
    secretbox ciphertext at rest and decrypted here for the tool call's config
    view."""
    wanted = set(keys)
    if not wanted:
        return {}
    with db.transaction() as cur:
        cur.execute("SELECT key, value FROM tool_config WHERE tool_id = %s", (tool_id,))
        return {row[0]: secretbox.decrypt(row[1]) for row in cur.fetchall() if row[0] in wanted}


def save_tool_config_value(cur: Any, tool_id: str, key: str, value: str) -> None:
    """Set one tool's deployment config value; an empty value clears the key.
    Stored as secretbox ciphertext so config secrets never sit in the clear."""
    if value:
        cur.execute(
            "INSERT INTO tool_config (tool_id, key, value) VALUES (%s, %s, %s)"
            " ON CONFLICT (tool_id, key) DO UPDATE SET value = EXCLUDED.value",
            (tool_id, key, secretbox.encrypt(value)),
        )
    else:
        cur.execute("DELETE FROM tool_config WHERE tool_id = %s AND key = %s", (tool_id, key))


def tool_credential(tool_id: str) -> dict[str, Any] | None:
    """One tool's stored OAuth credential (the store behind HostAPI.credentials),
    reassembled into the StoredCredential shape from its columns, or None if
    the tool is not connected."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT account_id, account_label, account_scopes, secret, metadata"
            " FROM tool_credentials WHERE tool_id = %s",
            (tool_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    account_id, account_label, account_scopes, secret_ciphertext, metadata = row
    secret = json.loads(secretbox.decrypt(secret_ciphertext))
    return {
        "account": {
            "id": account_id,
            "label": account_label,
            "scopes": [str(scope) for scope in account_scopes] if isinstance(account_scopes, list) else [],
        },
        "secret": secret if isinstance(secret, dict) else {},
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


def put_tool_credential(tool_id: str, value: dict[str, Any]) -> None:
    """Store a StoredCredential in its columns. Only the provider token
    material is a secret: it is serialized and secretbox-encrypted; the
    connected-account fields and tool bookkeeping are non-secret by contract
    (host/tools/host_api.py) and stored as plain columns. Malformed records
    are rejected rather than stored partially."""
    account = value.get("account")
    secret = value.get("secret")
    metadata = value.get("metadata")
    if (
        not isinstance(account, dict)
        or not isinstance(account.get("id"), str)
        or not account["id"]
        or not isinstance(account.get("label"), str)
        or not isinstance(account.get("scopes"), list)
        or not isinstance(secret, dict)
        or not isinstance(metadata, dict)
    ):
        raise ValueError(f"malformed stored credential for tool {tool_id}")
    with mutation() as cur:
        cur.execute(
            "INSERT INTO tool_credentials (tool_id, account_id, account_label, account_scopes, secret, metadata)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (tool_id) DO UPDATE SET account_id = EXCLUDED.account_id,"
            " account_label = EXCLUDED.account_label, account_scopes = EXCLUDED.account_scopes,"
            " secret = EXCLUDED.secret, metadata = EXCLUDED.metadata",
            (
                tool_id,
                account["id"],
                account["label"],
                db.jsonb([str(scope) for scope in account["scopes"]]),
                secretbox.encrypt(json.dumps(secret)),
                db.jsonb(metadata),
            ),
        )


def delete_tool_credential(tool_id: str) -> None:
    with mutation() as cur:
        cur.execute("DELETE FROM tool_credentials WHERE tool_id = %s", (tool_id,))


# -- tool audit log ------------------------------------------------------------
# The tool-side peer of the agent and network event logs: one row per tool
# event, paged newest-first with the same before-cursor model.

_TOOL_EVENT_FIELDS = "seq, created_at, tool_id, action_id, outcome, detail, arguments"


def _tool_event_dict(row: Any, *, include_arguments: bool = False) -> dict[str, Any]:
    seq, created_at, tool_id, action_id, outcome, detail, arguments = row
    event: dict[str, Any] = {
        "seq": int(seq),
        "timestamp": created_at,
        "event_id": f"tool_event_{seq}",
        "tool_id": tool_id,
        "action_id": action_id,
        "outcome": outcome,
        "detail": detail or "",
        "has_arguments": isinstance(arguments, dict),
    }
    if include_arguments:
        event["arguments"] = arguments if isinstance(arguments, dict) else None
    return event


def record_tool_event(
    tool_id: str,
    action_id: str,
    outcome: str,
    detail: str = "",
    arguments: dict[str, Any] | None = None,
) -> None:
    """Append one tool audit event in its own transaction. seq is a serial:
    unique and increasing, with harmless gaps from aborted transactions.
    Prunes to MAX_EVENTS amortized, like the agent event log."""
    with mutation() as cur:
        cur.execute(
            "INSERT INTO tool_events (created_at, tool_id, action_id, outcome, detail, arguments)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING seq",
            (utc_now(), tool_id, action_id, outcome, detail, db.jsonb(arguments) if arguments is not None else None),
        )
        if int(cur.fetchone()[0]) % PRUNE_EVERY == 0:
            _prune_events(cur, "tool_events")


def page_tool_events_before(
    before: int | None, *, limit: int = EVENT_PAGE_LIMIT
) -> list[dict[str, Any]]:
    return _page_before("tool_events", _TOOL_EVENT_FIELDS, _tool_event_dict, before, limit)


def tool_event(seq: int) -> dict[str, Any] | None:
    """Load one audit event with its exact arguments for an operator expansion."""
    with db.transaction() as cur:
        cur.execute(f"SELECT {_TOOL_EVENT_FIELDS} FROM tool_events WHERE seq = %s", (seq,))
        row = cur.fetchone()
    return _tool_event_dict(row, include_arguments=True) if row is not None else None


def _approval_id(number: int, check_token: str) -> str:
    # The public id carries the unguessable check token, so the id itself is
    # the agent's poll capability: no separate token to marry back up, and the
    # sequential number alone cannot be enumerated. token_urlsafe has no dots,
    # so the number splits off unambiguously.
    return f"{_APPROVAL_ID_PREFIX}{number}.{check_token}"


def _tool_approval_dict(row: Any) -> dict[str, Any]:
    number, tool_id, action_id, status, summary, payload, check_token, result, created_at, decided_at = row
    return {
        "approval_id": _approval_id(number, check_token),
        "tool_id": tool_id,
        "action_id": action_id,
        "status": status,
        "summary": summary,
        "payload": dict(payload) if isinstance(payload, dict) else {},
        "result": result or "",
        "created_at": int(created_at),
        "decided_at": int(decided_at),
    }


def _approval_number(approval_id: str) -> int | None:
    if not isinstance(approval_id, str) or not approval_id.startswith(_APPROVAL_ID_PREFIX):
        return None
    number_part = approval_id[len(_APPROVAL_ID_PREFIX):].split(".", 1)[0]
    return int(number_part) if number_part.isdigit() else None


def insert_tool_approval(
    tool_id: str,
    action_id: str,
    summary: str,
    payload: dict[str, Any],
    created_at: int,
    *,
    pending_limit: int,
) -> dict[str, Any]:
    with mutation() as cur:
        # All inserts run in the tools service under this process's
        # mutation lock, so the count check cannot race another insert.
        # (The admin maintenance pass may expire rows concurrently, which
        # only makes the backpressure count conservative.)
        cur.execute("SELECT COUNT(*) FROM tool_approvals WHERE status = 'pending'")
        if int(cur.fetchone()[0]) >= pending_limit:
            raise PendingToolApprovalLimitReached()
        cur.execute(
            "INSERT INTO tool_approvals (tool_id, action_id, status, summary, payload, check_token, created_at)"
            " VALUES (%s, %s, 'pending', %s, %s, %s, %s)"
            f" RETURNING {_TOOL_APPROVAL_FIELDS}",
            (tool_id, action_id, summary, db.jsonb(payload), secrets.token_urlsafe(32), created_at),
        )
        return _tool_approval_dict(cur.fetchone())


def tool_approval(approval_id: str, tool_id: str | None = None) -> dict[str, Any] | None:
    """The approval record for the full ``approval_<n>.<token>`` id, optionally
    restricted to one tool's partition. Returns None unless the id's token
    matches the stored one (constant-time), so a guessed number never
    resolves — verification for every caller lives here."""
    number = _approval_number(approval_id)
    if number is None:
        return None
    with db.transaction() as cur:
        if tool_id is None:
            cur.execute(
                f"SELECT {_TOOL_APPROVAL_FIELDS} FROM tool_approvals WHERE number = %s",
                (number,),
            )
        else:
            cur.execute(
                f"SELECT {_TOOL_APPROVAL_FIELDS} FROM tool_approvals"
                " WHERE number = %s AND tool_id = %s",
                (number, tool_id),
            )
        row = cur.fetchone()
    if row is None:
        return None
    record = _tool_approval_dict(row)
    if not hmac.compare_digest(approval_id, record["approval_id"]):
        return None
    return record


def list_tool_approvals(limit: int, tool_id: str | None = None) -> list[dict[str, Any]]:
    """Newest approvals first, pending before decided so open decisions
    surface at the top of the admin UI. Scoped to one tool when tool_id is set,
    which is how the operator UI shows approvals per tool rather than unified."""
    order = " ORDER BY (status = 'pending') DESC, number DESC LIMIT %s"
    with db.transaction() as cur:
        if tool_id is None:
            cur.execute(f"SELECT {_TOOL_APPROVAL_FIELDS} FROM tool_approvals{order}", (limit,))
        else:
            cur.execute(
                f"SELECT {_TOOL_APPROVAL_FIELDS} FROM tool_approvals WHERE tool_id = %s{order}",
                (tool_id, limit),
            )
        return [_tool_approval_dict(row) for row in cur.fetchall()]


def transition_tool_approval(
    approval_id: str,
    from_status: str,
    to_status: str,
    decided_at: int,
    result: str | None = None,
) -> bool:
    """Atomic conditional status transition; False when the record is absent
    or no longer in from_status, so concurrent decisions cannot both win.
    ``result`` is the terminal outcome text: the approved action's
    user-visible message when it executed, or the error when it failed."""
    number = _approval_number(approval_id)
    if number is None:
        return False
    with mutation() as cur:
        cur.execute(
            "UPDATE tool_approvals SET status = %s, decided_at = %s,"
            " result = COALESCE(%s, result)"
            " WHERE number = %s AND status = %s RETURNING number",
            (to_status, decided_at, result, number, from_status),
        )
        return cur.fetchone() is not None


def fail_approved_tool_approvals(decided_at: int) -> None:
    """Mark every approval stuck in ``approved`` as failed — a direct scan, so
    no record escapes through a listing horizon. Write a failure ``result``
    too, so ``check_tool_approval`` reports the interrupted execution instead
    of an empty outcome."""
    result = "The tools service restarted while executing this approved action; its outcome is unknown."
    with mutation() as cur:
        cur.execute(
            "UPDATE tool_approvals SET status = 'failed', decided_at = %s, result = %s WHERE status = 'approved'",
            (decided_at, result),
        )


def expire_tool_approvals(cutoff: int) -> None:
    """Expire pending approvals created before the cutoff (host expiry
    policy, applied by the maintenance pass)."""
    with mutation() as cur:
        cur.execute(
            "UPDATE tool_approvals SET status = 'expired', decided_at = %s"
            " WHERE status = 'pending' AND created_at < %s",
            (int(time.time()), cutoff),
        )


def prune_tool_approvals(keep: int) -> None:
    """Cap decided-approval history; pending records are never pruned."""
    with mutation() as cur:
        cur.execute(
            "DELETE FROM tool_approvals WHERE status <> 'pending' AND number <="
            " (SELECT COALESCE(MAX(number), 0) FROM tool_approvals) - %s",
            (keep,),
        )
