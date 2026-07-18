"""Power operations for existing TrustyClaw EC2 instances."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

from host.config import ConfigError, InputConfig, build_input_config
from host.constants import ADMIN_API_PORT
from host.cli.lifecycle_aws import _aws, _aws_env, _existing_storage_roles, _find_existing_instances, _find_storage_volume
from host.cli.lifecycle_constants import SSH_USER


def main_for_power_mode(mode: str, argv: list[str] | None = None) -> int:
    if mode not in {"start", "stop"}:
        raise ValueError(f"unsupported power mode: {mode}")
    parser = argparse.ArgumentParser(
        prog=f"python3 -m host.cli.{mode}",
        description=f"{mode.capitalize()} an existing TrustyClaw EC2 instance.",
    )
    parser.add_argument(
        "--agent-name",
        required=True,
        help="Stable host name: 1-50 characters of letters, numbers, '-' or '_'.",
    )
    args = parser.parse_args(argv)
    try:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not region:
            raise ConfigError("set AWS_REGION to the agent's AWS region")
        config = build_input_config(args.agent_name, region)
        env = _aws_env(config)
        instance_id = _single_existing_instance(config, env)
        _require_preserved_storage(config, env)
        initial = _describe_instance(env, instance_id)
        initial_state = _state(initial)
        if mode == "start":
            final = _start_instance(env, instance_id, initial_state)
        else:
            final = _stop_instance(env, instance_id, initial_state)
        # stdout carries only this result JSON.
        print(
            json.dumps(
                _result(config, env, final, mode=mode, initial_state=initial_state),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
        print(f"{mode} command failed: {stderr or exc}", file=sys.stderr)
        return 1


def _single_existing_instance(config: InputConfig, env: dict[str, str]) -> str:
    instances = _find_existing_instances(config, env)
    if len(instances) != 1:
        found = ", ".join(instances) or "none"
        raise ConfigError(f"power operation requires exactly one existing TrustyClaw instance; found {found}")
    return instances[0]


def _require_preserved_storage(config: InputConfig, env: dict[str, str]) -> None:
    roles = _existing_storage_roles(config, env)
    expected = {"admin", "agent"}
    if roles != expected:
        missing = ", ".join(sorted(expected - roles)) or "none"
        found = ", ".join(sorted(roles)) or "none"
        raise ConfigError(
            f"power operation requires existing admin and agent data volumes for {config.agent_name}; "
            f"found {found}, missing {missing}"
        )


def _describe_instance(env: dict[str, str], instance_id: str) -> dict[str, Any]:
    response = _aws(env, "ec2", "describe-instances", "--instance-ids", instance_id)
    return response["Reservations"][0]["Instances"][0]


def _state(instance: dict[str, Any]) -> str:
    state = instance.get("State", {}).get("Name")
    if not isinstance(state, str) or not state:
        raise ConfigError("TrustyClaw instance response is missing state")
    return state


def _start_instance(env: dict[str, str], instance_id: str, state: str) -> dict[str, Any]:
    if state == "stopping":
        _aws(env, "ec2", "wait", "instance-stopped", "--instance-ids", instance_id)
        state = "stopped"
    if state == "stopped":
        _aws(env, "ec2", "start-instances", "--instance-ids", instance_id)
    if state in {"pending", "stopped", "running"}:
        _aws(env, "ec2", "wait", "instance-running", "--instance-ids", instance_id)
        instance = _describe_instance(env, instance_id)
        if _state(instance) != "running":
            raise ConfigError(f"TrustyClaw instance {instance_id} did not reach running state")
        if not instance.get("PublicDnsName"):
            raise ConfigError(f"TrustyClaw instance {instance_id} has no public DNS after start")
        return instance
    raise ConfigError(f"cannot start TrustyClaw instance {instance_id} from state {state}")


def _stop_instance(env: dict[str, str], instance_id: str, state: str) -> dict[str, Any]:
    if state == "pending":
        _aws(env, "ec2", "wait", "instance-running", "--instance-ids", instance_id)
        state = "running"
    if state == "running":
        _aws(env, "ec2", "stop-instances", "--instance-ids", instance_id)
    if state in {"running", "stopping", "stopped"}:
        _aws(env, "ec2", "wait", "instance-stopped", "--instance-ids", instance_id)
        instance = _describe_instance(env, instance_id)
        if _state(instance) != "stopped":
            raise ConfigError(f"TrustyClaw instance {instance_id} did not reach stopped state")
        return instance
    raise ConfigError(f"cannot stop TrustyClaw instance {instance_id} from state {state}")


def _result(
    config: InputConfig,
    env: dict[str, str],
    instance: dict[str, Any],
    *,
    mode: str,
    initial_state: str,
) -> dict[str, object]:
    result: dict[str, object] = {
        "agent_name": config.agent_name,
        "instance_id": instance["InstanceId"],
        "region": config.aws_region,
        "state": _state(instance),
        "initial_state": initial_state,
        "operation": mode,
        "ssh_user": SSH_USER,
        "admin_ui_local_url": f"http://127.0.0.1:{ADMIN_API_PORT}",
    }
    public_dns = instance.get("PublicDnsName")
    if isinstance(public_dns, str) and public_dns:
        result["public_dns"] = public_dns
    public_ip = instance.get("PublicIpAddress")
    if isinstance(public_ip, str) and public_ip:
        result["public_ip"] = public_ip
    admin_volume = _find_storage_volume(config, env, "admin")
    agent_volume = _find_storage_volume(config, env, "agent")
    if admin_volume is not None:
        result["admin_volume_id"] = admin_volume["VolumeId"]
    if agent_volume is not None:
        result["agent_volume_id"] = agent_volume["VolumeId"]
    return result
