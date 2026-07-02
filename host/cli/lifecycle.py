"""CLI orchestration for host lifecycle commands.

Provisioning happens in two stages:

1. EC2 user data (small, secret-free): create the ``trustyclaw-operator`` account
   with only the single-use deploy key generated for this run.
2. Over SSH with the deploy key: copy the runtime code and the bootstrap
   script to the instance and run the bootstrap as root. Bootstrap removes
   the deploy key when it finishes.

By default, generated admin passwords appear only in local deploy/reconfigure
result files. The host receives just its SHA-256 hash, so nothing on the
instance ever contains the cleartext. Persistent environments can instead pass
``--admin-password-env`` to reuse a stable password during deploy or
reconfiguration.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

from host.config import ConfigError, load_input_config, public_operator_connections, runtime_operator_connections_from_input
from host.constants import ADMIN_API_PORT
from host.cli.lifecycle_aws import (
    _aws,
    _aws_env,
    _default_network,
    _ensure_security_group,
    _ensure_storage_volumes,
    _existing_storage_roles,
    _existing_storage_volume_availability_zone,
    _find_available_storage_volume,
    _find_existing_instances,
    _find_storage_volume,
    _launch_instance,
    _preserve_attached_volume_on_instance_termination,
    _preserve_existing_storage_volumes_on_instance_termination,
    _set_security_group_cloudflare_egress,
    _set_security_group_ssh_ingress,
    _subnet_has_public_ipv4_route,
    _tag_spec,
    _terminate_instances,
    _ubuntu_ami,
    _volume_tag_spec,
    _wait_for_instance,
)
from host.cli.lifecycle_bootstrap import (
    _bootstrap_payload,
    _generate_deploy_key,
    _provision_over_ssh,
    _render_bootstrap,
    _render_user_data,
    _write_runtime_code_archive,
)
from host.cli.lifecycle_checks import _check_existing_version_hints, _validate_command_preflight, _version_hint_error
from host.cli.lifecycle_constants import SSH_USER
from host.cli.lifecycle_logging import _log
from host.cli.lifecycle_types import LifecycleCommand
from host.version import repo_version


def _parse_args(mode: str, argv: list[str] | None) -> LifecycleCommand:
    if mode not in {"deploy", "upgrade", "recover", "reconfigure"}:
        raise ValueError(f"unsupported lifecycle mode: {mode}")
    descriptions = {
        "deploy": "Create a new TrustyClaw host with no existing instance or data volumes",
        "upgrade": "Upgrade preserved TrustyClaw state without changing admin password or operator access",
        "recover": "Create a replacement host from preserved data volumes and existing operator access",
        "reconfigure": "Replace operator access and refresh the admin password for preserved TrustyClaw state",
    }
    parser = argparse.ArgumentParser(
        prog=f"python3 -m host.cli.{mode}",
        description=descriptions[mode],
    )
    parser.add_argument("--config", required=True, help="Path to input config JSON")
    parser.add_argument(
        "--result-file",
        help=f"Path for the local result JSON. Defaults to <agent_name>-{mode}.json.",
    )
    if mode in {"deploy", "reconfigure"}:
        parser.add_argument(
            "--admin-password-env",
            help=(
                "Environment variable containing the admin password to install. "
                "Deploy and reconfigure generate a password when this is omitted."
            ),
        )
    if mode == "recover":
        parser.add_argument(
            "--allow-upgrade",
            action="store_true",
            help="Allow recovery to also advance preserved state to the local VERSION.",
        )
    args = parser.parse_args(argv)
    return LifecycleCommand(
        mode=mode,
        config_path=args.config,
        admin_password_env=getattr(args, "admin_password_env", None),
        allow_upgrade=bool(getattr(args, "allow_upgrade", False)),
        result_file=getattr(args, "result_file", None),
    )


def main_for_mode(mode: str, argv: list[str] | None = None) -> int:
    command = _parse_args(mode, argv)

    try:
        config = load_input_config(
            command.config_path,
            require_operator_connections=command.mode in {"deploy", "reconfigure"},
        )
        target_version = repo_version()
        admin_password = _command_admin_password(command)
        replacement_operator_connections = (
            runtime_operator_connections_from_input(config.operator_connections, os.environ)
            if config.operator_connections is not None
            else None
        )
        aws_env = _aws_env(config)
        _log(
            f"region {config.aws_region}; preparing {command.mode} for "
            f"'{config.agent_name}' at TrustyClaw {target_version}"
        )
        preferred_availability_zone = _existing_storage_volume_availability_zone(config, aws_env)
        existing = _find_existing_instances(config, aws_env)
        storage_roles = _existing_storage_roles(config, aws_env)
        _validate_command_preflight(command, config, existing, storage_roles)
        _check_existing_version_hints(command, config, aws_env, existing, target_version)

        replacement_network: tuple[str, str] | None = None
        if existing:
            replacement_network = _default_network(
                config,
                aws_env,
                preferred_availability_zone=preferred_availability_zone,
            )
            _preserve_existing_storage_volumes_on_instance_termination(config, aws_env, existing)
            _log(f"terminating existing instance(s): {', '.join(existing)}")
            _terminate_instances(existing, aws_env)
        with tempfile.TemporaryDirectory() as workdir_name:
            workdir = Path(workdir_name)
            deploy_key = _generate_deploy_key(workdir)
            _log("launching EC2 instance")
            instance_id, security_group_id = _launch_instance(
                config,
                deploy_key,
                aws_env,
                target_version=target_version,
                preferred_availability_zone=preferred_availability_zone,
                network=replacement_network,
            )
            created_storage_volumes: list[str] = []
            try:
                _log(f"launched {instance_id}; waiting for it to reach 'running'")
                instance = _wait_for_instance(instance_id, aws_env)
                public_dns = instance["PublicDnsName"]
                availability_zone = instance["Placement"]["AvailabilityZone"]
                _log(f"instance running at {public_dns}")
                storage_volumes, _ = _ensure_storage_volumes(
                    config,
                    aws_env,
                    instance_id=instance_id,
                    availability_zone=availability_zone,
                    wait_for_detach=bool(existing),
                    created_storage_volumes=created_storage_volumes,
                )
                access_summary = _provision_over_ssh(
                    config,
                    admin_password,
                    replacement_operator_connections,
                    public_dns,
                    deploy_key,
                    workdir,
                    storage_volumes,
                    mode=command.mode,
                    target_version=target_version,
                    allow_upgrade=command.allow_upgrade,
                )
                _set_security_group_ssh_ingress(
                    aws_env,
                    security_group_id,
                    enabled=_access_summary_includes_ssh(access_summary),
                )
                _set_security_group_cloudflare_egress(
                    aws_env,
                    security_group_id,
                    enabled=_access_summary_includes_cloudflare(access_summary),
                )
            except BaseException:
                # A failure here leaves a running instance with temporary
                # provisioning access still open. Tear it down so a retry starts
                # clean and nothing is exposed.
                _log(f"provisioning failed; terminating {instance_id} to avoid a half-provisioned, exposed host")
                try:
                    _terminate_instances([instance_id], aws_env)
                except Exception as cleanup_exc:  # noqa: BLE001 — best-effort cleanup
                    _log(f"warning: could not terminate {instance_id}: {cleanup_exc}")
                if created_storage_volumes:
                    _log(
                        "provisioning failed after creating data volume(s) "
                        f"{', '.join(created_storage_volumes)}; leaving them in place. "
                        "A later deploy retry will refuse existing data volumes. If this was a failed first install "
                        "and those volumes contain no initialized TrustyClaw state, delete the tagged volumes before "
                        "retrying deploy."
                    )
                raise
            _log("provisioning complete")
        result = {
            "agent_name": config.agent_name,
            "instance_id": instance_id,
            "region": config.aws_region,
            "public_dns": public_dns,
            "ssh_user": SSH_USER,
            "admin_ui_local_url": f"http://127.0.0.1:{ADMIN_API_PORT}",
            "admin_volume_id": storage_volumes["admin"],
            "agent_volume_id": storage_volumes["agent"],
            "version": target_version,
        }
        if admin_password is not None:
            result["admin_password"] = admin_password
        if replacement_operator_connections is not None:
            result["operator_connections"] = public_operator_connections(
                [connection.to_json() for connection in replacement_operator_connections]
            )
        output_path = _result_path(config.agent_name, command)
        output_path.touch(mode=0o600)
        output_path.chmod(0o600)  # contains the admin password for deploy/reconfigure
        output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(f"Wrote {command.mode} result to {output_path}")
        return 0
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
        print(f"deploy command failed: {stderr or exc}", file=sys.stderr)
        return 1


def _admin_password(env_name: str | None) -> str:
    if env_name is None:
        return secrets.token_urlsafe(32)
    value = os.environ.get(env_name)
    if not value:
        raise ConfigError(f"environment variable {env_name} is not set or is empty")
    return value


def _command_admin_password(command: LifecycleCommand) -> str | None:
    if command.mode in {"deploy", "reconfigure"}:
        return _admin_password(command.admin_password_env)
    return None


def _result_path(agent_name: str, command: LifecycleCommand) -> Path:
    if command.result_file is not None:
        return Path(command.result_file)
    return Path(f"{agent_name}-{command.mode}.json")


def _access_summary_includes_ssh(access_summary: dict[str, object]) -> bool:
    return access_summary.get("ssh_enabled") is True


def _access_summary_includes_cloudflare(access_summary: dict[str, object]) -> bool:
    return access_summary.get("cloudflare_enabled") is True
