"""Rendering for host provisioning artifacts.

One bootstrap implementation serves both provisioning deliveries: the SSH
delivery renders these artifacts on the operator machine and pushes them to
the instance, and the GitHub delivery renders them on the instance itself from
a fetched checkout (``host.bootstrap.self_provision``). Everything here runs
with the standard library only, because on the GitHub path it executes on a
stock Ubuntu image before any packages are installed.
"""

from __future__ import annotations

import json
from pathlib import Path
import tarfile
from typing import Any

from host.config import InputConfig, RuntimeOperatorConnection
from host.constants import (
    ADMIN_API_PORT,
    APP_BACKEND_ADMIN_SOCKET_PATH,
    APP_PORT_BASE,
    PROXY_PORT,
    PUBLIC_GITHUB_REPOSITORY,
    SERVICE_ACCOUNTS,
)
from host.runtime.core import app_platform

TEMPLATE_DIR = Path(__file__).resolve().parent


def _load_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text()


def _bootstrap_payload(
    config: InputConfig,
    admin_password_sha256: str | None,
    replacement_operator_connections: tuple[RuntimeOperatorConnection, ...] | None = None,
    storage_volumes: dict[str, str] | None = None,
    *,
    mode: str,
    target_version: str,
    allow_upgrade: bool = False,
) -> dict[str, Any]:
    runtime_config: dict[str, Any] = {
        "agent_name": config.agent_name,
    }
    if replacement_operator_connections is not None:
        runtime_config["operator_connections"] = [
            connection.to_json() for connection in replacement_operator_connections
        ]
    if admin_password_sha256 is not None:
        runtime_config["admin_password_sha256"] = admin_password_sha256
    return {
        "operation": {
            "mode": mode,
            "target_version": target_version,
            "allow_upgrade": allow_upgrade,
        },
        "runtime_config": runtime_config,
        "storage_volumes": storage_volumes or {},
    }


def _write_runtime_code_archive(code_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]

    def runtime_only(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if "__pycache__" in tarinfo.name:
            return None
        if tarinfo.name == "host/cli" or tarinfo.name.startswith("host/cli/"):
            return None  # lifecycle CLIs never run on the host
        return tarinfo

    with tarfile.open(code_path, "w:gz") as tar:
        # host/ includes the bundled tool framework and packages under
        # host/tools; the admin service imports them at startup. VERSION rides
        # along so self_provision can enforce the version gate on the
        # delivered tree, the same as on a fetched checkout.
        tar.add(root / "host", arcname="host", filter=runtime_only)
        tar.add(root / "VERSION", arcname="VERSION")


def _render_ssh_user_data(payload: dict[str, Any], deploy_public_key: str) -> str:
    # json.dumps never emits raw newlines, so the compact payload is one line
    # and can never collide with the heredoc delimiter.
    return (
        SSH_USER_DATA_TEMPLATE
        .replace("@PAYLOAD_JSON@", json.dumps(payload, sort_keys=True))
        .replace("@DEPLOY_PUBLIC_KEY@", deploy_public_key)
    )


def _render_github_user_data(payload: dict[str, Any], commit_sha: str) -> str:
    # json.dumps never emits raw newlines, so the compact payload is one line
    # and can never collide with the heredoc delimiter.
    return (
        GITHUB_USER_DATA_TEMPLATE
        .replace("@PAYLOAD_JSON@", json.dumps(payload, sort_keys=True))
        .replace("@GITHUB_REPOSITORY@", PUBLIC_GITHUB_REPOSITORY)
        .replace("@COMMIT_SHA@", commit_sha)
    )


def _service_account_constants() -> str:
    """Shell uid/gid variables for the pinned core service accounts, rendered
    from the same host.constants table verify_deploy checks on the host."""
    lines = []
    for name, uid in SERVICE_ACCOUNTS.items():
        prefix = name.upper().replace("-", "_")
        lines.append(f"{prefix}_UID={uid}")
        lines.append(f"{prefix}_GID={uid}")
    return "\n".join(lines)


def _render_bootstrap() -> str:
    rendered = (
        BOOTSTRAP_TEMPLATE
        .replace("@ADMIN_PORT@", str(ADMIN_API_PORT))
        .replace("@APP_PORT_BASE@", str(APP_PORT_BASE))
        .replace("@PROXY_PORT@", str(PROXY_PORT))
        .replace("@SERVICE_ACCOUNT_CONSTANTS@", _service_account_constants())
    )
    return _render_app_bootstrap(rendered)


def _render_app_bootstrap(template: str) -> str:
    apps = app_platform.installed_apps()

    def env_prefix(app_id: str) -> str:
        return app_id.upper()

    def port_name(app_id: str) -> str:
        return f"APP_{env_prefix(app_id)}_PORT"

    uid_lines = [
        "# App package host_slot values generate stable UID/GID assignments in",
        "# the reserved 48000-48099 range. Existing slots must not change.",
    ]
    port_lines = [
        "# App ports are generated from the same package-local host_slot values.",
    ]
    ensure_group_lines: list[str] = []
    ensure_user_lines: list[str] = []
    pg_hba_lines: list[str] = []
    role_lines: list[str] = []
    schema_grant_lines: list[str] = []
    connect_grant_lines: list[str] = []
    migration_lines: list[str] = []
    nftables_lines: list[str] = []
    unit_blocks: list[str] = []
    enable_start_lines: list[str] = []

    for app in apps:
        allocation = app.allocation
        env = env_prefix(app.id)
        backend_entrypoint = app.backend_entrypoint.relative_to(app.package_dir)
        port = port_name(app.id)
        uid_lines.extend([
            f"TRUSTYCLAW_APP_{env}_UID={allocation.uid}",
            f"TRUSTYCLAW_APP_{env}_GID={allocation.gid}",
        ])
        port_lines.append(f"{port}={app.port}")
        ensure_group_lines.append(f"ensure_group {app.linux_user} \"$TRUSTYCLAW_APP_{env}_GID\"")
        ensure_user_lines.append(
            f"ensure_user {app.linux_user} \"$TRUSTYCLAW_APP_{env}_UID\" {app.linux_user} /nonexistent"
        )
        pg_hba_lines.append(f"local  trustyclaw_admin  {app.linux_user}  peer")
        role_lines.extend([
            f"  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{app.linux_user}') THEN",
            f"    CREATE ROLE \"{app.linux_user}\" LOGIN;",
            "  END IF;",
        ])
        schema_grant_lines.append(
            f"  -c \"CREATE SCHEMA IF NOT EXISTS {app.db_schema} AUTHORIZATION \\\"{app.db_role}\\\";\" \\"
        )
        connect_grant_lines.append(
            f"  -c \"GRANT CONNECT ON DATABASE trustyclaw_admin TO \\\"{app.db_role}\\\";\""
        )
        pending_var = f"app_{app.id}_pending"
        migration_lines.extend([
            f"{pending_var}=\"$(runuser -u trustyclaw-admin -- env PYTHONPATH=/opt/trustyclaw-host python3 -m host.runtime.deploy.app_migrate pending {app.id})\"",
            "while IFS= read -r app_migration_version; do",
            "  [ -n \"$app_migration_version\" ] || continue",
            f"  runuser -u {app.linux_user} -- env PYTHONPATH=/opt/trustyclaw-host python3 -m host.runtime.deploy.app_migrate apply-sql {app.id} \"$app_migration_version\"",
            f"  runuser -u trustyclaw-admin -- env PYTHONPATH=/opt/trustyclaw-host python3 -m host.runtime.deploy.app_migrate record {app.id} \"$app_migration_version\"",
            f"done <<< \"${pending_var}\"",
        ])
        # New connections to an app port are allowed from exactly two uids:
        # the admin service (browser-bridge reverse proxy) and the agent-app
        # service (agent app_api reverse proxy). Everything else — agent
        # runtimes, other app users, ordinary local users — hits the drop.
        nftables_lines.extend([
            f"    oif lo ct state established,related meta skuid \"{app.linux_user}\" accept",
            f"    oif lo meta skuid \"{app.linux_user}\" drop",
            f"    oif lo tcp dport ${port} meta skuid \"trustyclaw-admin\" accept",
            f"    oif lo tcp dport ${port} meta skuid \"trustyclaw-agent-app\" accept",
            f"    oif lo tcp dport ${port} drop",
        ])
        unit_blocks.append(
            "\n".join([
                f"cat > /etc/systemd/system/{app.service_name} <<'UNIT'",
                "[Unit]",
                f"Description=TrustyClaw App: {app.title}",
                "After=network-online.target trustyclaw-admin-api.service trustyclaw-postgres.service",
                "Wants=network-online.target trustyclaw-admin-api.service trustyclaw-postgres.service",
                "StartLimitIntervalSec=0",
                "",
                "[Service]",
                f"User={app.linux_user}",
                "Slice=trustyclaw_app.slice",
                "UMask=0077",
                "Environment=PYTHONPATH=/opt/trustyclaw-host",
                "Environment=TRUSTYCLAW_APP_HOST=127.0.0.1",
                f"Environment=TRUSTYCLAW_APP_PORT={app.port}",
                f"Environment=TRUSTYCLAW_APP_DB_SCHEMA={app.db_schema}",
                f"Environment=TRUSTYCLAW_APP_ADMIN_API_SOCKET={APP_BACKEND_ADMIN_SOCKET_PATH}",
                f"WorkingDirectory=/opt/trustyclaw-host/host/apps/{app.id}",
                f"ExecStart=/usr/bin/python3 /opt/trustyclaw-host/host/apps/{app.id}/{backend_entrypoint}",
                "Restart=always",
                "RestartSec=3",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "UNIT",
            ])
        )
        enable_start_lines.extend([
            f"systemctl enable {app.service_name}",
            f"systemctl start {app.service_name}",
        ])

    replacements = {
        "@APP_UID_GID_CONSTANTS@": "\n".join(uid_lines),
        "@APP_PORT_CONSTANTS@": "\n".join(port_lines),
        "@APP_ENSURE_GROUPS@": "\n".join(ensure_group_lines),
        "@APP_ENSURE_USERS@": "\n".join(ensure_user_lines),
        "@APP_PG_HBA_LINES@": "\n".join(pg_hba_lines),
        "@APP_ROLE_SQL@": "\n".join(role_lines),
        "@APP_POSTGRES_SCHEMA_GRANTS@": "\n".join(schema_grant_lines),
        "@APP_POSTGRES_CONNECT_GRANTS@": " \\\n".join(connect_grant_lines),
        "@APP_MIGRATION_COMMANDS@": "\n".join(migration_lines),
        "@APP_NFTABLES_RULES@": "\n".join(nftables_lines),
        "@APP_SYSTEMD_UNITS@": "\n\n".join(unit_blocks),
        "@APP_ENABLE_START_COMMANDS@": "\n".join(enable_start_lines),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


# SSH delivery, stage 1 EC2 user data: base-account hardening, the single-use
# deploy key (in authorized_keys2 so stage 2 can delete that whole file to
# revoke it), and the embedded provisioning payload. Both deliveries stage the
# payload through user data; only code delivery differs.
SSH_USER_DATA_TEMPLATE = _load_template("user_data_ssh.sh")


# GitHub delivery, single-stage EC2 user data: hardens the base accounts,
# stages the payload, fetches the pinned public commit, and hands off to
# host.bootstrap.self_provision on the instance itself.
GITHUB_USER_DATA_TEMPLATE = _load_template("user_data_github.sh")


# The one bootstrap script both deliveries run as root on the instance,
# rendered on the instance itself by host.bootstrap.self_provision from the
# delivered code: the scp'd archive on the SSH delivery, the fetched checkout
# on the GitHub delivery. It consumes the payload staged by user data.
BOOTSTRAP_TEMPLATE = _load_template("bootstrap.sh")
