"""Schema migrations for the admin-state Postgres database.

Migrations are plain SQL files in ``host/migrations/``, named
``NNNN_description.sql`` with a monotonically increasing zero-padded version.
Each file has an up section and a down section, split by markers::

    -- migrate:up
    CREATE TABLE ...;

    -- migrate:down
    DROP TABLE ...;

Applied versions are recorded in the ``schema_migrations`` table, so ``up`` is
idempotent: it applies only the pending files, all inside one transaction (DDL
is transactional in Postgres), so an upgrade either fully lands or leaves the
schema at the previous version. A transaction-scoped advisory lock serializes
concurrent runners, so a bootstrap run and a manual operator run cannot
interleave.

This runner is deliberately in-repo instead of goose/alembic/dbmate: the host
runs no Go toolchain and no third-party Python packages, the migration *files*
must ship with the runtime code so upgrades can apply them offline, and ~150
lines of runner is easier to audit than a migration framework. The file format matches the goose/dbmate model (versioned SQL,
up/down, a version table) so the history could be ported to one of those tools
later without rewriting migrations.

Usage: ``python3 -m host.runtime.deploy.migrate {up|down|status} [--to VERSION]``.
``up`` runs on every deploy/upgrade (bootstrap, before services start); the
admin service itself never migrates. ``down`` is a manual operator action.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Any

from host.runtime.core import db

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
FILE_NAME_RE = re.compile(r"^(\d{4})_([A-Za-z0-9_]+)\.sql$")
UP_MARKER = "-- migrate:up"
DOWN_MARKER = "-- migrate:down"
# Arbitrary fixed key for pg_advisory_lock; shared by every runner of this
# database so two migration runs serialize instead of interleaving DDL.
ADVISORY_LOCK_KEY = 0x74727573_7479636C % (2**63)


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up_sql: str
    down_sql: str


def load_migrations(directory: Path | None = None) -> list[Migration]:
    directory = MIGRATIONS_DIR if directory is None else directory
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = FILE_NAME_RE.fullmatch(path.name)
        if not match:
            raise MigrationError(
                f"{path.name}: migration files must be named NNNN_description.sql"
            )
        version = int(match.group(1))
        up_sql, down_sql = _split_sections(path)
        migrations.append(Migration(version, match.group(2), up_sql, down_sql))
    versions = [migration.version for migration in migrations]
    for previous, current in zip(versions, versions[1:]):
        if current == previous:
            raise MigrationError(f"duplicate migration version {current:04d}")
    return migrations


def _split_sections(path: Path) -> tuple[str, str]:
    text = path.read_text()
    if UP_MARKER not in text:
        raise MigrationError(f"{path.name}: missing '{UP_MARKER}' marker")
    if DOWN_MARKER not in text:
        raise MigrationError(f"{path.name}: missing '{DOWN_MARKER}' marker")
    up_part, down_part = text.split(DOWN_MARKER, 1)
    up_sql = up_part.split(UP_MARKER, 1)[1].strip()
    down_sql = down_part.strip()
    if not up_sql:
        raise MigrationError(f"{path.name}: empty up section")
    return up_sql, down_sql


def applied_versions(cur: Any) -> dict[int, str]:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version BIGINT PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    cur.execute("SELECT version, name FROM schema_migrations ORDER BY version")
    return {int(version): str(name) for version, name in cur.fetchall()}


def up(target: int | None = None, *, directory: Path | None = None, quiet: bool = False) -> list[int]:
    """Apply every pending migration (or up to ``target``). Returns the
    versions applied. Safe to call concurrently and repeatedly."""
    migrations = load_migrations(directory)
    applied: list[int] = []
    with db.transaction() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
        done = applied_versions(cur)
        for migration in migrations:
            if migration.version in done:
                continue
            if target is not None and migration.version > target:
                break
            cur.execute(migration.up_sql)
            cur.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (migration.version, migration.name),
            )
            applied.append(migration.version)
            if not quiet:
                print(f"applied {migration.version:04d}_{migration.name}")
    return applied


def down(target: int | None = None, *, directory: Path | None = None, quiet: bool = False) -> list[int]:
    """Roll back the newest applied migration, or with ``target`` every
    migration above it (``--to 0`` reverts everything)."""
    migrations = {migration.version: migration for migration in load_migrations(directory)}
    reverted: list[int] = []
    with db.transaction() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
        done = sorted(applied_versions(cur))
        to_revert = [v for v in done if v > target] if target is not None else done[-1:]
        for version in reversed(to_revert):
            migration = migrations.get(version)
            if migration is None:
                raise MigrationError(
                    f"cannot roll back version {version:04d}: no migration file for it"
                )
            if not migration.down_sql:
                raise MigrationError(
                    f"cannot roll back version {version:04d}: its down section is empty"
                )
            cur.execute(migration.down_sql)
            cur.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
            reverted.append(version)
            if not quiet:
                print(f"reverted {migration.version:04d}_{migration.name}")
    return reverted


def status(*, directory: Path | None = None) -> list[tuple[int, str, bool]]:
    migrations = load_migrations(directory)
    with db.transaction() as cur:
        done = applied_versions(cur)
    return [(m.version, m.name, m.version in done) for m in migrations]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Admin-state schema migrations")
    parser.add_argument("command", choices=("up", "down", "status"))
    parser.add_argument(
        "--to",
        type=int,
        default=None,
        help="for up: highest version to apply; for down: roll back every version above this",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "up":
            applied = up(args.to)
            if not applied:
                print("nothing to apply")
        elif args.command == "down":
            reverted = down(args.to)
            if not reverted:
                print("nothing to roll back")
        else:
            for version, name, is_applied in status():
                marker = "applied" if is_applied else "pending"
                print(f"{version:04d}_{name}: {marker}")
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
