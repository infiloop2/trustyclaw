"""Scratch PostgreSQL cluster for the unit tests.

Admin state lives in Postgres, so state/admin-API/orchestrator tests need a
real database. This harness starts one throwaway cluster per test process —
Unix socket only, in a temp directory, no network — applies the repo's schema
migrations once, and exports the ``TRUSTYCLAW_DB_*`` environment so the
runtime code under test connects to it. ``reset_database()`` truncates all
tables between tests, which is much faster than a cluster or database per
test.

The server binaries come from PATH, from the newest ``/usr/lib/postgresql/*``
install, or from ``TRUSTYCLAW_TEST_PG_BIN``. If the binaries are unavailable
the calling test is skipped with instructions; CI installs PostgreSQL in the
sandbox image, so the suite never silently loses this coverage there. No
Python driver is needed anywhere: the runtime brings its own protocol client
(host.runtime.pgclient) and cluster administration uses the createdb/dropdb
binaries.
"""

from __future__ import annotations

import atexit
import glob
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import unittest

_STARTED = False
_SKIP_REASON: str | None = None


def _subprocess_env(work_dir: Path) -> dict[str, str]:
    """initdb and postgres require a passwd entry for the effective uid. The
    CI sandbox runs as an arbitrary uid with none, so fake one through
    nss_wrapper when needed (and available)."""
    env = os.environ.copy()
    try:
        pwd.getpwuid(os.geteuid())
        return env
    except KeyError:
        pass
    wrappers = glob.glob("/usr/lib/*/libnss_wrapper.so") + glob.glob("/usr/lib/libnss_wrapper.so")
    if not wrappers:
        return env  # initdb will fail with its own clear error
    passwd_file = work_dir / "nss_passwd"
    group_file = work_dir / "nss_group"
    uid, gid = os.geteuid(), os.getegid()
    passwd_file.write_text(f"pgtest:x:{uid}:{gid}:pgtest:{work_dir}:/bin/sh\n")
    group_file.write_text(f"pgtest:x:{gid}:\n")
    env["LD_PRELOAD"] = wrappers[0]
    env["NSS_WRAPPER_PASSWD"] = str(passwd_file)
    env["NSS_WRAPPER_GROUP"] = str(group_file)
    return env


def _find_pg_bin() -> Path | None:
    override = os.environ.get("TRUSTYCLAW_TEST_PG_BIN")
    if override:
        return Path(override)
    initdb = shutil.which("initdb")
    if initdb:
        return Path(initdb).resolve().parent
    versions = Path("/usr/lib/postgresql")
    if versions.is_dir():
        candidates = sorted(
            (path for path in versions.iterdir() if (path / "bin" / "initdb").exists()),
            key=lambda path: int(path.name) if path.name.isdigit() else 0,
        )
        if candidates:
            return candidates[-1] / "bin"
    return None


def ensure_database() -> None:
    """Start the scratch cluster once per process and point TRUSTYCLAW_DB_* at
    it. Raises unittest.SkipTest when PostgreSQL is unavailable."""
    global _STARTED, _SKIP_REASON
    if _SKIP_REASON is not None:
        raise unittest.SkipTest(_SKIP_REASON)
    if _STARTED:
        return
    pg_bin = _find_pg_bin()
    if pg_bin is None or not (pg_bin / "initdb").exists():
        _SKIP_REASON = (
            "PostgreSQL server binaries not found "
            "(apt install postgresql, or set TRUSTYCLAW_TEST_PG_BIN to a bin directory)"
        )
        raise unittest.SkipTest(_SKIP_REASON)

    data_dir = Path(tempfile.mkdtemp(prefix="trustyclaw-pg-data.")) / "data"
    # A separate short socket path: Unix socket paths are limited to ~107
    # bytes and temp dirs under deep workspaces can exceed that.
    socket_dir = Path(tempfile.mkdtemp(prefix="tcpg.", dir="/tmp"))

    def cleanup() -> None:
        subprocess.run(
            [str(pg_bin / "pg_ctl"), "-D", str(data_dir), "stop", "-m", "immediate"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        shutil.rmtree(data_dir.parent, ignore_errors=True)
        shutil.rmtree(socket_dir, ignore_errors=True)

    atexit.register(cleanup)
    env = _subprocess_env(data_dir.parent)
    subprocess.run(
        [str(pg_bin / "initdb"), "-D", str(data_dir), "-U", "postgres", "-A", "trust", "-E", "UTF8"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
        env=env,
    )
    # Durability off: the cluster is thrown away with the process, and the
    # test suite hits the disk hard without this.
    options = (
        f"-c listen_addresses='' -c unix_socket_directories='{socket_dir}'"
        " -c fsync=off -c synchronous_commit=off -c full_page_writes=off"
    )
    subprocess.run(
        [
            str(pg_bin / "pg_ctl"),
            "-D",
            str(data_dir),
            "-l",
            str(data_dir.parent / "postgres.log"),
            "-o",
            options,
            "start",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
        env=env,
    )
    os.environ["TRUSTYCLAW_DB_SOCKET_DIR"] = str(socket_dir)
    os.environ["TRUSTYCLAW_DB_NAME"] = "trustyclaw_test"
    os.environ["TRUSTYCLAW_DB_USER"] = "postgres"

    # The proxy and tools roles must exist before migrations run (the schema
    # GRANTs them their tables). Tests connect as postgres either way.
    for role in ("trustyclaw-proxy", "trustyclaw-tools"):
        subprocess.run(
            [str(pg_bin / "createuser"), "-h", str(socket_dir), "-U", "postgres", role],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            env=env,
        )
    subprocess.run(
        [str(pg_bin / "createdb"), "-h", str(socket_dir), "-U", "postgres", "trustyclaw_test"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
        env=env,
    )

    from host.runtime import migrate

    migrate.up(quiet=True)
    _STARTED = True


def reset_database() -> None:
    """Truncate every state table and clear the in-process stores (test
    setUp); the schema stays migrated."""
    ensure_database()
    from host.runtime import orchestrator

    orchestrator._RUNTIME_STATUSES.clear()
    from host.runtime import db

    with db.transaction() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables"
            " WHERE schemaname = 'public' AND tablename <> 'schema_migrations'"
        )
        tables = [row[0] for row in cur.fetchall()]
        if tables:
            names = ", ".join(f'"{name}"' for name in tables)
            cur.execute(f"TRUNCATE {names} RESTART IDENTITY")
        # The schema migration seeds the secretbox key at schema time
        # (schema present => key present); truncation wipes data, so restore
        # that invariant the same way the migration does.
        cur.execute(
            "INSERT INTO secret_keys (singleton, key_hex)"
            " VALUES (TRUE, translate(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''))"
        )


def create_database(name: str) -> None:
    """Create an extra empty database on the scratch cluster (migration-runner
    tests use their own so they can migrate down without disturbing the shared
    schema)."""
    ensure_database()
    from host.runtime import db

    # Pooled connections from a previous test would keep the database busy
    # and fail the DROP below.
    db.close_pool()
    pg_bin = _find_pg_bin()
    assert pg_bin is not None  # ensure_database already found it
    socket_dir = os.environ["TRUSTYCLAW_DB_SOCKET_DIR"]
    subprocess.run(
        [str(pg_bin / "dropdb"), "--if-exists", "-h", socket_dir, "-U", "postgres", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
    )
    subprocess.run(
        [str(pg_bin / "createdb"), "-h", socket_dir, "-U", "postgres", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
    )
