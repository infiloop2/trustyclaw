"""Preflight and version validation for host lifecycle commands."""

from __future__ import annotations

from host.config import ConfigError, InputConfig
from host.cli import lifecycle_aws
from host.cli.lifecycle_constants import VERSION_TAG_KEY
from host.cli.lifecycle_logging import _log
from host.cli.lifecycle_types import LifecycleCommand
from host.version import compare_versions


def _validate_command_preflight(
    command: LifecycleCommand,
    config: InputConfig,
    existing_instances: list[str],
    storage_roles: set[str],
) -> None:
    expected_roles = {"admin", "agent"}
    if command.mode == "deploy":
        if existing_instances:
            raise ConfigError(
                f"deploy requires no existing TrustyClaw instance for {config.agent_name}; "
                "use upgrade or recover for preserved hosts"
            )
        if storage_roles:
            raise ConfigError(
                f"deploy requires no existing TrustyClaw data volumes for {config.agent_name}; "
                f"found {', '.join(sorted(storage_roles))}. If a previous first-time deploy failed after creating "
                "blank data volumes, delete those tagged volumes before retrying deploy. Use recover only for "
                "initialized TrustyClaw volumes with admin-state/version.json."
            )
        return

    if storage_roles != expected_roles:
        missing = ", ".join(sorted(expected_roles - storage_roles)) or "none"
        found = ", ".join(sorted(storage_roles)) or "none"
        raise ConfigError(
            f"{command.mode} requires existing admin and agent data volumes for {config.agent_name}; "
            f"found {found}, missing {missing}"
        )
    if command.mode in {"upgrade", "reconfigure"} and not existing_instances:
        raise ConfigError(
            f"{command.mode} requires an existing TrustyClaw instance for {config.agent_name}; "
            "use recover to recreate a missing or broken host"
        )
    if command.mode == "recover" and existing_instances:
        raise ConfigError(
            f"recover requires no existing TrustyClaw instance for {config.agent_name}; "
            "use upgrade for a normal release upgrade, or reconfigure to change admin password or operator access"
        )


def _check_existing_version_hints(
    command: LifecycleCommand,
    config: InputConfig,
    env: dict[str, str],
    instance_ids: list[str],
    target_version: str,
) -> None:
    if not instance_ids:
        return
    response = lifecycle_aws._aws(env, "ec2", "describe-instances", "--instance-ids", *instance_ids)
    hints: dict[str, str] = {}
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            tags = {tag.get("Key"): tag.get("Value") for tag in instance.get("Tags", [])}
            hint = tags.get(VERSION_TAG_KEY)
            if isinstance(hint, str) and hint:
                hints[instance["InstanceId"]] = hint
    if not hints:
        _log("existing EC2 instance has no TrustyClaw version tag; admin disk version will be authoritative")
        return
    hint_text = ", ".join(f"{instance_id}={version}" for instance_id, version in sorted(hints.items()))
    _log(f"existing EC2 version tag hint(s): {hint_text}; admin disk version is authoritative")
    for instance_id, version in sorted(hints.items()):
        try:
            comparison = compare_versions(version, target_version)
        except ValueError:
            raise ConfigError(
                f"existing TrustyClaw instance {instance_id} has invalid {VERSION_TAG_KEY} tag {version!r}; "
                "fix or remove the tag before running upgrade or recover"
            )
        error = _version_hint_error(command, version, target_version, comparison)
        if error is not None:
            raise ConfigError(f"existing TrustyClaw instance {instance_id} is tagged with version {version}; {error}")


def _version_hint_error(
    command: LifecycleCommand,
    version: str,
    target_version: str,
    comparison: int,
) -> str | None:
    if command.mode == "upgrade":
        if comparison >= 0:
            return (
                f"upgrade requires preserved state older than local VERSION {target_version}; "
                "run recover for same-version repair, or use a newer checkout for newer preserved state"
            )
        return None
    if command.mode == "recover":
        if command.allow_upgrade:
            if comparison > 0:
                return (
                    f"recover --allow-upgrade cannot move preserved state backward to local VERSION {target_version}; "
                    "use a checkout at the same or newer TrustyClaw version"
                )
            return None
        if comparison != 0:
            return (
                f"recover requires preserved state to match local VERSION {target_version}; "
                "use recover --allow-upgrade to advance older state, or use a newer checkout for newer preserved state"
            )
    if command.mode == "reconfigure":
        if comparison != 0:
            return (
                f"{command.mode} requires preserved state to match local VERSION {target_version}; "
                "run upgrade first, or use a checkout matching the preserved state"
            )
    return None
