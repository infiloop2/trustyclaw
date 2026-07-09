"""SSH bootstrap packaging for TrustyClaw host lifecycle commands."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import time
from typing import Any

from host.config import ConfigError, InputConfig, RuntimeOperatorConnection
from host.constants import ADMIN_API_PORT, APP_PORT_BASE, PROXY_PORT
from host.runtime import app_platform
from host.cli.lifecycle_constants import SSH_USER, SSH_WAIT_ATTEMPTS, SSH_WAIT_SECONDS
from host.cli.lifecycle_logging import _log


TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "bootstrap"
ACCESS_SUMMARY_PREFIX = "TRUSTYCLAW_BOOTSTRAP_ACCESS_SUMMARY "


def _load_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text()


def _generate_deploy_key(workdir: Path) -> Path:
    key_path = workdir / "deploy_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-C", "trustyclaw-deploy", "-f", str(key_path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return key_path


def _provision_over_ssh(
    config: InputConfig,
    admin_password: str | None,
    replacement_operator_connections: tuple[RuntimeOperatorConnection, ...] | None,
    public_dns: str,
    deploy_key: Path,
    workdir: Path,
    storage_volumes: dict[str, str],
    *,
    mode: str,
    target_version: str,
    allow_upgrade: bool,
) -> dict[str, Any]:
    payload = _bootstrap_payload(
        config,
        admin_password,
        replacement_operator_connections,
        storage_volumes,
        mode=mode,
        target_version=target_version,
        allow_upgrade=allow_upgrade,
    )
    payload_path = workdir / "trustyclaw_payload.json"
    payload_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    bootstrap_path = workdir / "trustyclaw_bootstrap.sh"
    bootstrap_path.write_text(_render_bootstrap(config))
    code_path = workdir / "trustyclaw-host-code.tar.gz"
    _write_runtime_code_archive(code_path)

    ssh = [
        "ssh",
        "-i",
        str(deploy_key),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={workdir / 'known_hosts'}",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
    ]
    target = f"{SSH_USER}@{public_dns}"
    _log("waiting for SSH on the new instance (it is still booting)")
    _wait_for_ssh(ssh, target)
    _log("SSH is up; copying runtime code and bootstrap script to the host")
    subprocess.run(
        ["scp", *ssh[1:], str(payload_path), str(bootstrap_path), str(code_path), f"{target}:/tmp/"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _log("running bootstrap: apt + security updates, Postgres + schema migrations, npm,")
    _log("agent CLIs, services. This takes several minutes; the host's own output streams below.")
    print("-" * 70, flush=True)
    access_summary = _run_bootstrap_and_read_access_summary(ssh, target)
    print("-" * 70, flush=True)
    return access_summary


def _bootstrap_payload(
    config: InputConfig,
    admin_password: str | None,
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
    if admin_password is not None:
        runtime_config["admin_password_sha256"] = hashlib.sha256(admin_password.encode()).hexdigest()
    return {
        "operation": {
            "mode": mode,
            "target_version": target_version,
            "allow_upgrade": allow_upgrade,
        },
        "runtime_config": runtime_config,
        "storage_volumes": storage_volumes or {},
    }


def _run_bootstrap_and_read_access_summary(ssh: list[str], target: str) -> dict[str, bool]:
    process = subprocess.Popen(
        [*ssh, target, "sudo", "bash", "/tmp/trustyclaw_bootstrap.sh"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if process.stdout is None:
        raise ConfigError("could not read bootstrap output")
    access_summary: dict[str, bool] | None = None
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if line.startswith(ACCESS_SUMMARY_PREFIX):
            access_summary = _parse_access_summary(line[len(ACCESS_SUMMARY_PREFIX) :])
    returncode = process.wait()
    if returncode != 0:
        raise ConfigError(f"bootstrap failed on the host (exit {returncode}); see the output above")
    if access_summary is None:
        raise ConfigError("bootstrap did not emit final access summary")
    return access_summary


def _parse_access_summary(value: str) -> dict[str, bool]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"bootstrap emitted invalid access summary: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("bootstrap access summary is not an object")
    summary: dict[str, bool] = {}
    for key in ("ssh_enabled", "cloudflare_enabled"):
        enabled = parsed.get(key)
        if not isinstance(enabled, bool):
            raise ConfigError(f"bootstrap access summary field {key!r} is missing or not a boolean")
        summary[key] = enabled
    return summary


def _wait_for_ssh(ssh: list[str], target: str) -> None:
    for attempt in range(SSH_WAIT_ATTEMPTS):
        result = subprocess.run(
            [*ssh, target, "true"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            return
        if attempt % 3 == 0:
            _log(f"  still waiting for SSH ({(attempt + 1) * SSH_WAIT_SECONDS}s elapsed)")
        time.sleep(SSH_WAIT_SECONDS)
    raise ConfigError(f"could not reach {target} over SSH after {SSH_WAIT_ATTEMPTS * SSH_WAIT_SECONDS} seconds")


def _write_runtime_code_archive(code_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]

    def runtime_only(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if "__pycache__" in tarinfo.name:
            return None
        if tarinfo.name == "host/cli" or tarinfo.name.startswith("host/cli/"):
            return None  # lifecycle CLIs never run on the host
        return tarinfo

    with tarfile.open(code_path, "w:gz") as tar:
        tar.add(root / "host", arcname="host", filter=runtime_only)


def _render_user_data(deploy_public_key: str) -> str:
    return (
        USER_DATA_TEMPLATE
        .replace("@DEPLOY_PUBLIC_KEY@", deploy_public_key)
    )


def _render_bootstrap(config: InputConfig) -> str:
    rendered = (
        BOOTSTRAP_TEMPLATE
        .replace("@ADMIN_PORT@", str(ADMIN_API_PORT))
        .replace("@APP_PORT_BASE@", str(APP_PORT_BASE))
        .replace("@PROXY_PORT@", str(PROXY_PORT))
    )
    return _render_app_bootstrap(rendered)


def _render_app_bootstrap(template: str) -> str:
    apps = app_platform.installed_apps()
    registry = app_platform.app_registry()
    missing_allocations = [app.id for app in apps if app.id not in registry]
    if missing_allocations:
        raise ConfigError(f"missing app registry allocation for: {', '.join(missing_allocations)}")

    def env_prefix(app_id: str) -> str:
        return app_id.upper()

    def port_name(app_id: str) -> str:
        return f"APP_{env_prefix(app_id)}_PORT"

    uid_lines = [
        "# Installed app service users use the reserved static range 48000-48099.",
        "# Add new apps to host/apps/registry.json; keep existing UID/GID values",
        "# unchanged across upgrades and update the deploy invariant test.",
    ]
    port_lines = [
        "# App ports are host-owned static assignments from host/apps/registry.json.",
        "# Add new apps there so bootstrap provisioning and /v1/apps stay aligned.",
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
        allocation = registry[app.id]
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
            f"{pending_var}=\"$(runuser -u trustyclaw-admin -- env PYTHONPATH=/opt/trustyclaw-host python3 -m host.runtime.app_migrate pending {app.id})\"",
            "while IFS= read -r app_migration_version; do",
            "  [ -n \"$app_migration_version\" ] || continue",
            f"  runuser -u {app.linux_user} -- env PYTHONPATH=/opt/trustyclaw-host python3 -m host.runtime.app_migrate apply-sql {app.id} \"$app_migration_version\"",
            f"  runuser -u trustyclaw-admin -- env PYTHONPATH=/opt/trustyclaw-host python3 -m host.runtime.app_migrate record {app.id} \"$app_migration_version\"",
            f"done <<< \"${pending_var}\"",
        ])
        nftables_lines.extend([
            f"    oif lo ct state established,related meta skuid \"{app.linux_user}\" accept",
            f"    oif lo meta skuid \"{app.linux_user}\" drop",
            f"    oif lo tcp dport ${port} meta skuid \"trustyclaw-admin\" accept",
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
                "UMask=0077",
                "Environment=PYTHONPATH=/opt/trustyclaw-host",
                "Environment=TRUSTYCLAW_APP_HOST=127.0.0.1",
                f"Environment=TRUSTYCLAW_APP_PORT={app.port}",
                f"Environment=TRUSTYCLAW_APP_DB_SCHEMA={app.db_schema}",
                "Environment=TRUSTYCLAW_APP_ADMIN_API_SOCKET=/run/trustyclaw-admin-api/app-backend.sock",
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


# Stage 1, EC2 user data: trustyclaw-operator account and deploy SSH key only, no secrets.
# The deploy key lands in authorized_keys2 so stage 2 can delete that whole
# file to revoke it.
USER_DATA_TEMPLATE = _load_template("user_data.sh")


# Stage 2, run as root over SSH after the code and payload are copied over.
BOOTSTRAP_TEMPLATE = _load_template("bootstrap.sh")
