"""Connection layer for the local host-state Postgres database.

The admin, proxy, tools, and app processes use this module under distinct
database roles. They connect over the local Unix socket through the in-repo
protocol client (``host.runtime.pgclient``; standard library only, with no
driver dependency) and authenticate with Postgres ``peer`` auth. Access
therefore starts from the operating system identity, then table and schema
grants narrow each non-owner role. No role exists for the agent user,
``pg_hba.conf`` rejects every unlisted peer, and no database passwords exist.

The admin role owns host tables; the proxy writes network events and held
pushes; the tools role writes tool credentials, approvals, and events; and
each app role owns only its schema. MVCC transactions, constraints, and
conditional updates carry cross-process correctness. Schema and app
migrations serialize under advisory locks.

Connections are pooled process-wide. A caller gets a connection for exactly
the span of one ``transaction()`` block; nested ``transaction()`` blocks on the
same thread intentionally get *different* connections, so an inner read-only
transaction sees the last committed state rather than the outer transaction's
uncommitted work (state.py's read-inside-update semantics depend on this).
A connection left stale by a Postgres restart fails exactly one request and
is closed on the way out, so the pool self-heals; tests that repoint the
database environment must call ``close_pool()`` (pg_harness does).

Environment overrides (tests and local harnesses only — production services
inherit systemd's controlled environment, where none of these are set, so the
defaults below are the production configuration and a stray shell variable
cannot repoint a service):

- ``TRUSTYCLAW_DB_SOCKET_DIR``: Unix socket directory.
- ``TRUSTYCLAW_DB_NAME``: database name (default ``trustyclaw_admin``).
- ``TRUSTYCLAW_DB_USER``: role name (default: the OS user, for peer auth).
"""

from __future__ import annotations

from contextlib import contextmanager
import os
import threading
from typing import Any, Iterator

from host.runtime import pgclient

DEFAULT_DB_NAME = "trustyclaw_admin"
# Idle pooled connections kept per process. The admin API serves one operator
# over loopback; a couple of warm connections cover the request handler plus
# the worker threads without holding dozens of server slots.
POOL_LIMIT = 4
# Cap on this process's *active* database sessions. The three core database
# clients and two bundled apps use at most 5 x 14 = 70 of the server's 100
# slots, leaving 30 for operator access, the superuser reserve, and deployment
# work. Sessions are millisecond-lived (slow work happens outside
# transactions), so a burst,
# such as the proxy's 64 concurrent handlers each logging a decision, queues
# here briefly instead of failing at the server.
# Must be at least 2: a nested read inside a mutation holds a second session.
MAX_ACTIVE_CONNECTIONS = 14
CHECKOUT_TIMEOUT_SECONDS = 10
_ACTIVE = threading.BoundedSemaphore(MAX_ACTIVE_CONNECTIONS)

_POOL: list[pgclient.Connection] = []
_POOL_LOCK = threading.Lock()


def jsonb(value: Any) -> pgclient.Jsonb:
    """Adapt a Python value to a json/jsonb parameter with deterministic key
    order."""
    return pgclient.Jsonb(value)


def connect_kwargs(*, user: str | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"dbname": os.environ.get("TRUSTYCLAW_DB_NAME", DEFAULT_DB_NAME)}
    if os.environ.get("TRUSTYCLAW_DB_SOCKET_DIR"):
        kwargs["socket_dir"] = os.environ["TRUSTYCLAW_DB_SOCKET_DIR"]
    if user is not None:
        kwargs["user"] = user
    elif os.environ.get("TRUSTYCLAW_DB_USER"):
        kwargs["user"] = os.environ["TRUSTYCLAW_DB_USER"]
    return kwargs


def _checkout() -> pgclient.Connection:
    """Take a pooled connection or open a new one. No liveness ping: a
    connection gone stale (Postgres restart) fails its one request, is closed
    by transaction()'s rollback path, and the pool self-heals."""
    if not _ACTIVE.acquire(timeout=CHECKOUT_TIMEOUT_SECONDS):
        raise pgclient.Error(
            f"database session budget exhausted ({MAX_ACTIVE_CONNECTIONS} active for"
            f" {CHECKOUT_TIMEOUT_SECONDS}s); sessions should be millisecond-lived —"
            " something is holding transactions open"
        )
    try:
        with _POOL_LOCK:
            if _POOL:
                return _POOL.pop()
        # A full server (operator psql sessions share its budget) surfaces as
        # one failed request with a clear error; the caller retries. The
        # semaphore above is the real backpressure.
        return pgclient.connect(**connect_kwargs())
    except BaseException:
        _ACTIVE.release()
        raise


def _checkin(conn: pgclient.Connection) -> None:
    try:
        if conn.closed:
            return
        with _POOL_LOCK:
            if len(_POOL) < POOL_LIMIT:
                _POOL.append(conn)
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
        conns = list(_POOL)
        _POOL.clear()
    for conn in conns:
        _close_quietly(conn)


@contextmanager
def transaction(*, user: str | None = None) -> Iterator[pgclient.Cursor]:
    """One database transaction: yields a cursor, commits on normal exit,
    rolls back on exception. Reentrant per thread by taking a second
    connection, never by nesting on one connection. A ``user`` override (app
    schema migrations) bypasses the pool: it opens a dedicated connection and
    closes it on exit — it runs a handful of times per deploy."""
    pooled = user is None
    conn = _checkout() if pooled else pgclient.connect(**connect_kwargs(user=user))
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
        if pooled:
            _checkin(conn)
        else:
            _close_quietly(conn)
