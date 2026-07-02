"""Connection layer for the local admin-state Postgres database.

Two processes use this module: the admin service (full access as the
schema-owning role) and the network proxy (a narrow role granted exactly the
policy/pin reads and its own network_events table). Both connect over the
local Unix socket through the in-repo protocol client (``host.runtime.pgclient``
— standard library only, no driver dependency) and authenticate with Postgres
``peer`` auth, so access control is the operating system's user identity; no
role exists for the agent user (pg_hba additionally rejects everyone else),
and there are no database passwords anywhere.

Cross-process concurrency is deliberately trivial: the admin service is the
only writer of every table except network_events, which only the proxy writes
(single-INSERT appends on a serial key — no shared-row contention exists),
and the proxy's policy/pin reads are MVCC snapshots that never block the
admin's transactional replacements. Schema migrations serialize under an
advisory lock.

Connections are pooled process-wide. A caller gets a connection for exactly
the span of one ``transaction()`` block; nested ``transaction()`` blocks on the
same thread intentionally get *different* connections, so an inner read-only
transaction sees the last committed state rather than the outer transaction's
uncommitted work (state.py's read-inside-update semantics depend on this).

Environment overrides (tests and local harnesses only — production services
inherit systemd's controlled environment, where none of these are set, so the
defaults below are the production configuration and a stray shell variable
cannot repoint a service):

- ``TRUSTYCLAW_DB_SOCKET_DIR``: Unix socket directory.
- ``TRUSTYCLAW_DB_PORT``: socket port suffix.
- ``TRUSTYCLAW_DB_NAME``: database name (default ``trustyclaw_admin``).
- ``TRUSTYCLAW_DB_USER``: role name (default: the OS user, for peer auth).
"""

from __future__ import annotations

from contextlib import contextmanager
import os
import threading
import time
from typing import Any, Iterator

from host.runtime import pgclient

DEFAULT_DB_NAME = "trustyclaw_admin"
# Idle pooled connections kept per process. The admin API serves one operator
# over loopback; a couple of warm connections cover the request handler plus
# the worker threads without holding dozens of server slots.
POOL_LIMIT = 4
# Cap on this process's *active* database sessions, set so the admin and
# proxy processes together stay well below the server's max_connections (50)
# with headroom for operator psql sessions. Sessions are millisecond-lived
# (slow work happens outside transactions), so a burst — e.g. the proxy's 64
# concurrent handlers each logging a decision — queues here briefly instead
# of failing at the server. Must be at least 2: a nested read inside a
# mutation holds a second session.
MAX_ACTIVE_CONNECTIONS = 18
CHECKOUT_TIMEOUT_SECONDS = 10
_ACTIVE = threading.BoundedSemaphore(MAX_ACTIVE_CONNECTIONS)
# SQLSTATE for the server refusing a connection because every slot is taken;
# transient by construction here, so worth a brief retry.
_TOO_MANY_CONNECTIONS = "53300"

_POOL: list[tuple[tuple[tuple[str, Any], ...], pgclient.Connection]] = []
_POOL_LOCK = threading.Lock()


def jsonb(value: Any) -> pgclient.Jsonb:
    """Adapt a Python value to a json/jsonb parameter with deterministic key
    order."""
    return pgclient.Jsonb(value)


def connect_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"dbname": os.environ.get("TRUSTYCLAW_DB_NAME", DEFAULT_DB_NAME)}
    if os.environ.get("TRUSTYCLAW_DB_SOCKET_DIR"):
        kwargs["socket_dir"] = os.environ["TRUSTYCLAW_DB_SOCKET_DIR"]
    if os.environ.get("TRUSTYCLAW_DB_PORT"):
        kwargs["port"] = int(os.environ["TRUSTYCLAW_DB_PORT"])
    if os.environ.get("TRUSTYCLAW_DB_USER"):
        kwargs["user"] = os.environ["TRUSTYCLAW_DB_USER"]
    return kwargs


def _checkout() -> pgclient.Connection:
    """Take a live connection for the current parameters from the pool, or
    open a new one. Pooled connections created under different parameters
    (tests repoint the environment) are discarded, and every checkout is
    ping-verified so a Postgres restart costs a reconnect, not a failed
    request."""
    if not _ACTIVE.acquire(timeout=CHECKOUT_TIMEOUT_SECONDS):
        raise pgclient.Error(
            f"database session budget exhausted ({MAX_ACTIVE_CONNECTIONS} active for"
            f" {CHECKOUT_TIMEOUT_SECONDS}s); sessions should be millisecond-lived —"
            " something is holding transactions open"
        )
    try:
        key = tuple(sorted(connect_kwargs().items()))
        while True:
            with _POOL_LOCK:
                if not _POOL:
                    break
                pooled_key, conn = _POOL.pop()
            if pooled_key != key:
                _close_quietly(conn)
                continue
            try:
                conn.execute("SELECT 1")
                return conn
            except (pgclient.Error, OSError):
                _close_quietly(conn)
        # Operator psql sessions share the server's connection budget, so a
        # full server is possible even under our own cap; those sessions and
        # our bursts are short, so back off briefly before giving up.
        for attempt in range(3):
            try:
                return pgclient.connect(**connect_kwargs())
            except pgclient.Error as exc:
                if exc.sqlstate != _TOO_MANY_CONNECTIONS or attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))
        raise AssertionError("unreachable")
    except BaseException:
        _ACTIVE.release()
        raise


def _checkin(conn: pgclient.Connection) -> None:
    try:
        if conn.closed:
            return
        key = tuple(sorted(connect_kwargs().items()))
        with _POOL_LOCK:
            if len(_POOL) < POOL_LIMIT:
                _POOL.append((key, conn))
                return
        _close_quietly(conn)
    finally:
        _ACTIVE.release()


def _close_quietly(conn: pgclient.Connection) -> None:
    try:
        conn.close()
    except Exception:
        pass


def close_pool() -> None:
    """Close every pooled connection (test teardown between databases)."""
    with _POOL_LOCK:
        conns = [conn for _, conn in _POOL]
        _POOL.clear()
    for conn in conns:
        _close_quietly(conn)


@contextmanager
def transaction() -> Iterator[pgclient.Cursor]:
    """One database transaction: yields a cursor, commits on normal exit,
    rolls back on exception. Reentrant per thread by taking a second
    connection, never by nesting on one connection."""
    conn = _checkout()
    try:
        conn.execute("BEGIN")
        with pgclient.Cursor(conn) as cur:
            yield cur
        conn.execute("COMMIT")
    except BaseException:
        try:
            if not conn.closed:
                conn.execute("ROLLBACK")
        except (pgclient.Error, OSError):
            _close_quietly(conn)
        raise
    finally:
        _checkin(conn)
