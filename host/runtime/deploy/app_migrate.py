"""Migration runner for installed app schemas.

Core host migrations create the host-owned ``app_schema_migrations`` table.
Bootstrap drives the one migration path: ``pending``, then per version
``apply-sql`` as the app Linux/Postgres role (peer auth) and ``record`` as
the admin role after the SQL succeeds.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from host.runtime.core import app_platform, db
from host.runtime.deploy import migrate


def pending(app_id: str) -> list[int]:
    app = app_platform.app_by_id(app_id)
    if app is None:
        raise migrate.MigrationError(f"unknown app: {app_id}")
    migrations = migrate.load_migrations(app.migrations_dir)
    with db.transaction() as cur:
        done = applied_versions(cur, app.id)
    return [migration.version for migration in migrations if migration.version not in done]


def apply_sql(app_id: str, version: int, *, connection_user: str | None = None) -> None:
    app, migration = _migration_by_version(app_id, version)
    with db.transaction(user=connection_user) as cur:
        cur.execute("SELECT current_user")
        row = cur.fetchone()
        current_user = str(row[0]) if row is not None else ""
        if current_user != app.db_role:
            raise migrate.MigrationError(
                f"app migration must run as {app.db_role}, got {current_user}"
            )
        cur.execute(f"SET LOCAL search_path TO {_quote_ident(app.db_schema)}")
        cur.execute(migration.up_sql)


def record(app_id: str, version: int) -> None:
    app, migration = _migration_by_version(app_id, version)
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO app_schema_migrations (app_id, version, name) VALUES (%s, %s, %s)",
            (app.id, migration.version, migration.name),
        )


def applied_versions(cur: Any, app_id: str) -> dict[int, str]:
    cur.execute(
        "SELECT version, name FROM app_schema_migrations WHERE app_id = %s ORDER BY version",
        (app_id,),
    )
    return {int(version): str(name) for version, name in cur.fetchall()}


def _migration_by_version(app_id: str, version: int) -> tuple[app_platform.AppManifest, migrate.Migration]:
    app = app_platform.app_by_id(app_id)
    if app is None:
        raise migrate.MigrationError(f"unknown app: {app_id}")
    for migration in migrate.load_migrations(app.migrations_dir):
        if migration.version == version:
            return app, migration
    raise migrate.MigrationError(f"unknown app migration: {app_id}:{version:04d}")


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="App schema migrations")
    parser.add_argument("command", choices=("pending", "apply-sql", "record"))
    parser.add_argument("app_id")
    parser.add_argument("version", nargs="?", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "pending":
            for version in pending(args.app_id):
                print(version)
        elif args.command == "apply-sql":
            if args.version is None:
                raise migrate.MigrationError("apply-sql requires a version")
            apply_sql(args.app_id, args.version)
        elif args.command == "record":
            if args.version is None:
                raise migrate.MigrationError("record requires a version")
            record(args.app_id, args.version)
    except (app_platform.AppError, migrate.MigrationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
