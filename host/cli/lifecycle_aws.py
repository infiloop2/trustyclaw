"""AWS resource operations for TrustyClaw host lifecycle commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

from host.config import ConfigError, InputConfig
from host.cli.lifecycle_bootstrap import _render_user_data
from host.cli.lifecycle_constants import (
    ADMIN_VOLUME_DEVICE,
    ADMIN_VOLUME_SIZE_GB,
    AGENT_VOLUME_DEVICE,
    AGENT_VOLUME_SIZE_GB,
    INSTANCE_TAG_KEY,
    INSTANCE_TYPE,
    OWNER_TAG_KEY,
    ROOT_VOLUME_SIZE_GB,
    SSH_INGRESS,
    VERSION_TAG_KEY,
    VOLUME_ROLE_TAG_KEY,
)
from host.cli.lifecycle_logging import _log

CLOUDFLARE_TUNNEL_EGRESS = (
    {"IpProtocol": "tcp", "FromPort": 7844, "ToPort": 7844, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
    {"IpProtocol": "udp", "FromPort": 7844, "ToPort": 7844, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
)


def _aws_env(config: InputConfig) -> dict[str, str]:
    access_key = os.environ.get(config.aws_access_key_id_env)
    secret_key = os.environ.get(config.aws_secret_access_key_env)
    if not access_key:
        raise ConfigError(f"environment variable {config.aws_access_key_id_env} is not set")
    if not secret_key:
        raise ConfigError(f"environment variable {config.aws_secret_access_key_env} is not set")
    env = os.environ.copy()
    env.update(
        {
            "AWS_ACCESS_KEY_ID": access_key,
            "AWS_SECRET_ACCESS_KEY": secret_key,
            "AWS_DEFAULT_REGION": config.aws_region,
        }
    )
    env.pop("AWS_SESSION_TOKEN", None)
    return env


def _aws(env: dict[str, str], *args: str) -> Any:
    proc = subprocess.run(
        ["aws", *args],
        check=True,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    text = proc.stdout.strip()
    return json.loads(text) if text else None


def _find_existing_instances(config: InputConfig, env: dict[str, str]) -> list[str]:
    response = _aws(
        env,
        "ec2",
        "describe-instances",
        "--filters",
        f"Name=tag:{INSTANCE_TAG_KEY},Values={config.agent_name}",
        f"Name=tag:{OWNER_TAG_KEY},Values=true",
        "Name=instance-state-name,Values=pending,running,stopping,stopped",
    )
    ids: list[str] = []
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            ids.append(instance["InstanceId"])
    return ids


def _existing_storage_roles(config: InputConfig, env: dict[str, str]) -> set[str]:
    roles: set[str] = set()
    for role in ("admin", "agent"):
        if _find_storage_volume(config, env, role) is not None:
            roles.add(role)
    return roles


def _terminate_instances(instance_ids: list[str], env: dict[str, str]) -> None:
    _aws(env, "ec2", "terminate-instances", "--instance-ids", *instance_ids)
    _aws(env, "ec2", "wait", "instance-terminated", "--instance-ids", *instance_ids)


def _launch_instance(
    config: InputConfig,
    deploy_key: Path,
    env: dict[str, str],
    *,
    target_version: str,
    preferred_availability_zone: str | None = None,
    network: tuple[str, str] | None = None,
) -> tuple[str, str]:
    if network is None:
        network = _default_network(config, env, preferred_availability_zone=preferred_availability_zone)
    vpc_id, subnet_id = network
    _log(f"using vpc {vpc_id}, public subnet {subnet_id}")
    security_group_id = _ensure_security_group(config, env, vpc_id)
    ami_id = _ubuntu_ami(env)
    _log(f"using Ubuntu AMI {ami_id}, instance type {INSTANCE_TYPE}")
    user_data = _render_user_data(deploy_key.with_suffix(".pub").read_text().strip())
    # Pass user data via fileb:// rather than a raw string. The AWS CLI v2 default
    # cli_binary_format is "base64", so a raw --user-data string would be decoded
    # as base64 (corrupting the script and breaking cloud-init). fileb:// reads the
    # bytes as-is and base64-encodes them for EC2 regardless of that setting.
    user_data_path = deploy_key.parent / "user_data.sh"
    user_data_path.write_text(user_data)
    response = _aws(env,
        "ec2",
        "run-instances",
        "--image-id",
        ami_id,
        "--instance-type",
        INSTANCE_TYPE,
        "--subnet-id",
        subnet_id,
        "--security-group-ids",
        security_group_id,
        "--associate-public-ip-address",
        "--metadata-options",
        "HttpTokens=required,HttpEndpoint=enabled",
        "--block-device-mappings",
        json.dumps(
            [
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {
                        "VolumeSize": ROOT_VOLUME_SIZE_GB,
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                    },
                }
            ]
        ),
        "--user-data",
        f"fileb://{user_data_path}",
        "--tag-specifications",
        _tag_spec("instance", config.agent_name, target_version=target_version),
        _tag_spec("volume", config.agent_name),
    )
    return response["Instances"][0]["InstanceId"], security_group_id


def _ensure_storage_volumes(
    config: InputConfig,
    env: dict[str, str],
    *,
    instance_id: str,
    availability_zone: str,
    wait_for_detach: bool = False,
    created_storage_volumes: list[str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    volumes: dict[str, str] = {}
    created: list[str] = []
    for role, size_gb, device in (
        ("admin", ADMIN_VOLUME_SIZE_GB, ADMIN_VOLUME_DEVICE),
        ("agent", AGENT_VOLUME_SIZE_GB, AGENT_VOLUME_DEVICE),
    ):
        existing = _find_available_storage_volume(
            config,
            env,
            role,
            availability_zone,
            wait_for_detach=wait_for_detach,
        )
        if existing is None:
            volume_id = _create_storage_volume(config, env, role, size_gb, availability_zone)
            created.append(volume_id)
            if created_storage_volumes is not None:
                created_storage_volumes.append(volume_id)
        else:
            volume_id = existing
            _log(f"reusing {role} storage volume {volume_id}")
        _attach_volume(env, instance_id=instance_id, volume_id=volume_id, device=device)
        volumes[role] = volume_id
    return volumes, created


def _existing_storage_volume_availability_zone(config: InputConfig, env: dict[str, str]) -> str | None:
    availability_zones = set()
    for role in ("admin", "agent"):
        volume = _find_storage_volume(config, env, role)
        if volume is not None:
            availability_zone = volume.get("AvailabilityZone")
            if isinstance(availability_zone, str) and availability_zone:
                availability_zones.add(availability_zone)
    if len(availability_zones) > 1:
        raise ConfigError(
            f"TrustyClaw storage volumes for {config.agent_name} are split across availability zones: "
            f"{', '.join(sorted(availability_zones))}"
        )
    return next(iter(availability_zones), None)


def _find_storage_volume(config: InputConfig, env: dict[str, str], role: str) -> dict[str, Any] | None:
    response = _aws(
        env,
        "ec2",
        "describe-volumes",
        "--filters",
        f"Name=tag:{INSTANCE_TAG_KEY},Values={config.agent_name}",
        f"Name=tag:{OWNER_TAG_KEY},Values=true",
        f"Name=tag:{VOLUME_ROLE_TAG_KEY},Values={role}",
    )
    volumes = [volume for volume in response.get("Volumes", []) if volume.get("State") != "deleted"]
    if not volumes:
        return None
    if len(volumes) > 1:
        volume_ids = ", ".join(sorted(volume["VolumeId"] for volume in volumes))
        raise ConfigError(f"multiple TrustyClaw {role} volumes found for {config.agent_name}: {volume_ids}")
    return volumes[0]


def _find_available_storage_volume(
    config: InputConfig,
    env: dict[str, str],
    role: str,
    availability_zone: str,
    *,
    wait_for_detach: bool = False,
) -> str | None:
    volume = _find_storage_volume(config, env, role)
    if volume is None:
        return None
    state = volume.get("State")
    if state != "available" and wait_for_detach:
        volume_id = volume["VolumeId"]
        _log(f"waiting for preserved {role} volume {volume_id} to detach")
        _aws(env, "ec2", "wait", "volume-available", "--volume-ids", volume_id)
        volume = _find_storage_volume(config, env, role)
        if volume is None:
            raise ConfigError(
                f"TrustyClaw {role} volume {volume_id} for {config.agent_name} disappeared while waiting to detach"
            )
        state = volume.get("State")
    if state != "available":
        raise ConfigError(
            f"TrustyClaw {role} volume {volume['VolumeId']} for {config.agent_name} is {state}; "
            "detach it or wait for the previous instance to terminate before redeploying"
        )
    volume_availability_zone = volume.get("AvailabilityZone")
    if volume_availability_zone != availability_zone:
        raise ConfigError(
            f"TrustyClaw {role} volume {volume['VolumeId']} is in {volume_availability_zone}, "
            f"but the replacement instance is in {availability_zone}"
        )
    return volume["VolumeId"]


def _create_storage_volume(
    config: InputConfig,
    env: dict[str, str],
    role: str,
    size_gb: int,
    availability_zone: str,
) -> str:
    _log(f"creating {role} storage volume ({size_gb} GiB gp3) in {availability_zone}")
    response = _aws(
        env,
        "ec2",
        "create-volume",
        "--availability-zone",
        availability_zone,
        "--size",
        str(size_gb),
        "--volume-type",
        "gp3",
        "--encrypted",
        "--tag-specifications",
        _volume_tag_spec(config.agent_name, role),
    )
    volume_id = response["VolumeId"]
    _aws(env, "ec2", "wait", "volume-available", "--volume-ids", volume_id)
    return volume_id


def _attach_volume(env: dict[str, str], *, instance_id: str, volume_id: str, device: str) -> None:
    _log(f"attaching storage volume {volume_id} as {device}")
    _aws(
        env,
        "ec2",
        "attach-volume",
        "--instance-id",
        instance_id,
        "--volume-id",
        volume_id,
        "--device",
        device,
    )
    _aws(env, "ec2", "wait", "volume-in-use", "--volume-ids", volume_id)
    _preserve_attached_volume_on_instance_termination(env, instance_id=instance_id, device=device)


def _preserve_existing_storage_volumes_on_instance_termination(
    config: InputConfig,
    env: dict[str, str],
    instance_ids: list[str],
) -> None:
    storage_volume_ids = set()
    for role in ("admin", "agent"):
        volume = _find_storage_volume(config, env, role)
        if volume is not None:
            storage_volume_ids.add(volume["VolumeId"])
    if not storage_volume_ids:
        return
    response = _aws(env, "ec2", "describe-instances", "--instance-ids", *instance_ids)
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            instance_id = instance["InstanceId"]
            for mapping in instance.get("BlockDeviceMappings", []):
                ebs = mapping.get("Ebs", {})
                volume_id = ebs.get("VolumeId")
                device = mapping.get("DeviceName")
                if volume_id in storage_volume_ids and isinstance(device, str) and device:
                    _preserve_attached_volume_on_instance_termination(env, instance_id=instance_id, device=device)


def _preserve_attached_volume_on_instance_termination(env: dict[str, str], *, instance_id: str, device: str) -> None:
    _aws(
        env,
        "ec2",
        "modify-instance-attribute",
        "--instance-id",
        instance_id,
        "--block-device-mappings",
        json.dumps(
            [
                {
                    "DeviceName": device,
                    "Ebs": {
                        "DeleteOnTermination": False,
                    },
                }
            ]
        ),
    )


def _default_network(
    config: InputConfig,
    env: dict[str, str],
    *,
    preferred_availability_zone: str | None = None,
) -> tuple[str, str]:
    vpcs = _aws(env, "ec2", "describe-vpcs", "--filters", "Name=is-default,Values=true")
    if not vpcs.get("Vpcs"):
        raise ConfigError("AWS account has no default VPC in the configured region")
    vpc_id = vpcs["Vpcs"][0]["VpcId"]
    subnets = _aws(env,
        "ec2",
        "describe-subnets",
        "--filters",
        f"Name=vpc-id,Values={vpc_id}",
        "Name=default-for-az,Values=true",
    )
    if not subnets.get("Subnets"):
        raise ConfigError("AWS default VPC has no default subnet")
    candidate_subnets = sorted(subnets["Subnets"], key=lambda item: item["SubnetId"])
    if preferred_availability_zone is not None:
        candidate_subnets = [
            subnet
            for subnet in candidate_subnets
            if subnet.get("AvailabilityZone") == preferred_availability_zone
        ]
        if not candidate_subnets:
            raise ConfigError(
                f"AWS default VPC has no default subnet in {preferred_availability_zone} "
                "for the existing TrustyClaw storage volumes"
            )
    public_subnets = [
        subnet
        for subnet in candidate_subnets
        if _subnet_has_public_ipv4_route(env, vpc_id, subnet["SubnetId"])
    ]
    if not public_subnets:
        raise ConfigError(
            "AWS default VPC has no default subnet with an active 0.0.0.0/0 route to an internet gateway"
        )
    subnet_id = public_subnets[0]["SubnetId"]
    return vpc_id, subnet_id


def _subnet_has_public_ipv4_route(env: dict[str, str], vpc_id: str, subnet_id: str) -> bool:
    route_tables = _aws(env,
        "ec2",
        "describe-route-tables",
        "--filters",
        f"Name=association.subnet-id,Values={subnet_id}",
    ).get("RouteTables", [])
    if not route_tables:
        route_tables = _aws(env,
            "ec2",
            "describe-route-tables",
            "--filters",
            f"Name=vpc-id,Values={vpc_id}",
            "Name=association.main,Values=true",
        ).get("RouteTables", [])
    for route_table in route_tables:
        for route in route_table.get("Routes", []):
            if route.get("DestinationCidrBlock") != "0.0.0.0/0":
                continue
            if route.get("State") != "active":
                continue
            gateway_id = route.get("GatewayId", "")
            if gateway_id.startswith("igw-"):
                return True
    return False


def _ensure_security_group(config: InputConfig, env: dict[str, str], vpc_id: str) -> str:
    name = f"trustyclaw-host-{config.agent_name}"
    groups = _aws(env,
        "ec2",
        "describe-security-groups",
        "--filters",
        f"Name=group-name,Values={name}",
        f"Name=vpc-id,Values={vpc_id}",
    ).get("SecurityGroups", [])
    if groups:
        group_id = groups[0]["GroupId"]
        tags = {tag.get("Key"): tag.get("Value") for tag in groups[0].get("Tags", [])}
        if tags.get(OWNER_TAG_KEY) == "true" and tags.get(INSTANCE_TAG_KEY) == config.agent_name:
            _log(f"warning: reusing existing security group {group_id} named {name}")
        else:
            raise ConfigError(
                f"existing security group {group_id} named {name} is not tagged as a TrustyClaw resource; "
                "rename or delete it before deploying"
            )
    else:
        created = _aws(env,
            "ec2",
            "create-security-group",
            "--group-name",
            name,
            "--description",
            f"TrustyClaw {config.agent_name}",
            "--vpc-id",
            vpc_id,
            "--tag-specifications",
            _tag_spec("security-group", config.agent_name),
        )
        group_id = created["GroupId"]
    _reset_security_group_rules(env, group_id)
    # SSH is opened for the single-use provisioning key. After bootstrap, the
    # lifecycle CLI keeps or revokes this ingress based on the stored operator
    # connections.
    _authorize_if_missing(env, "authorize-security-group-ingress", group_id, SSH_INGRESS)
    # Egress is pinned to HTTP, HTTPS, NTP, and a temporary Cloudflare Tunnel
    # connector allowance: bootstrap downloads and all proxied agent traffic use
    # 80/443, timesync uses UDP 123, cloudflared may use 7844, and DNS to the
    # VPC resolver bypasses security groups. After bootstrap, the lifecycle CLI
    # keeps or revokes the connector allowance based on the stored operator
    # connections.
    for egress in (
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        {"IpProtocol": "udp", "FromPort": 123, "ToPort": 123, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        *CLOUDFLARE_TUNNEL_EGRESS,
    ):
        _authorize_if_missing(env, "authorize-security-group-egress", group_id, egress)
    return group_id


def _set_security_group_ssh_ingress(env: dict[str, str], group_id: str, *, enabled: bool) -> None:
    if enabled:
        _authorize_if_missing(env, "authorize-security-group-ingress", group_id, SSH_INGRESS)
        return
    group = _aws(env, "ec2", "describe-security-groups", "--group-ids", group_id)["SecurityGroups"][0]
    matching = [
        permission
        for permission in group.get("IpPermissions", [])
        if permission.get("IpProtocol") == SSH_INGRESS["IpProtocol"]
        and permission.get("FromPort") == SSH_INGRESS["FromPort"]
        and permission.get("ToPort") == SSH_INGRESS["ToPort"]
    ]
    if matching:
        _aws(
            env,
            "ec2",
            "revoke-security-group-ingress",
            "--group-id",
            group_id,
            "--ip-permissions",
            json.dumps(matching),
        )


def _set_security_group_cloudflare_egress(env: dict[str, str], group_id: str, *, enabled: bool) -> None:
    if enabled:
        for egress in CLOUDFLARE_TUNNEL_EGRESS:
            _authorize_if_missing(env, "authorize-security-group-egress", group_id, egress)
        return
    group = _aws(env, "ec2", "describe-security-groups", "--group-ids", group_id)["SecurityGroups"][0]
    matching = [
        permission
        for permission in group.get("IpPermissionsEgress", [])
        if any(_same_permission_shape(permission, egress) for egress in CLOUDFLARE_TUNNEL_EGRESS)
    ]
    if matching:
        _aws(
            env,
            "ec2",
            "revoke-security-group-egress",
            "--group-id",
            group_id,
            "--ip-permissions",
            json.dumps(matching),
        )


def _same_permission_shape(permission: dict[str, Any], expected: dict[str, Any]) -> bool:
    return (
        permission.get("IpProtocol") == expected["IpProtocol"]
        and permission.get("FromPort") == expected["FromPort"]
        and permission.get("ToPort") == expected["ToPort"]
    )


def _reset_security_group_rules(env: dict[str, str], group_id: str) -> None:
    group = _aws(env, "ec2", "describe-security-groups", "--group-ids", group_id)["SecurityGroups"][0]
    if group.get("IpPermissions"):
        _aws(env,
            "ec2",
            "revoke-security-group-ingress",
            "--group-id",
            group_id,
            "--ip-permissions",
            json.dumps(group["IpPermissions"]),
        )
    if group.get("IpPermissionsEgress"):
        _aws(env,
            "ec2",
            "revoke-security-group-egress",
            "--group-id",
            group_id,
            "--ip-permissions",
            json.dumps(group["IpPermissionsEgress"]),
        )


def _authorize_if_missing(env: dict[str, str], command: str, group_id: str, permission: dict[str, Any]) -> None:
    try:
        _aws(env, "ec2", command, "--group-id", group_id, "--ip-permissions", json.dumps([permission]))
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode()
        if "InvalidPermission.Duplicate" not in stderr:
            raise


def _ubuntu_ami(env: dict[str, str]) -> str:
    response = _aws(env,
        "ssm",
        "get-parameter",
        "--name",
        "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id",
    )
    return response["Parameter"]["Value"]


def _tag_spec(resource_type: str, agent_name: str, *, target_version: str | None = None) -> str:
    tags = [
        f"{{Key={INSTANCE_TAG_KEY},Value={agent_name}}}",
        f"{{Key={OWNER_TAG_KEY},Value=true}}",
        f"{{Key=Name,Value=trustyclaw-host-{agent_name}}}",
    ]
    if resource_type == "instance" and target_version is not None:
        tags.append(f"{{Key={VERSION_TAG_KEY},Value={target_version}}}")
    return f"ResourceType={resource_type},Tags=[{','.join(tags)}]"


def _volume_tag_spec(agent_name: str, role: str) -> str:
    return (
        "ResourceType=volume,Tags=["
        f"{{Key={INSTANCE_TAG_KEY},Value={agent_name}}},"
        f"{{Key={OWNER_TAG_KEY},Value=true}},"
        f"{{Key={VOLUME_ROLE_TAG_KEY},Value={role}}},"
        f"{{Key=Name,Value=trustyclaw-host-{agent_name}-{role}}}"
        "]"
    )


def _wait_for_instance(instance_id: str, env: dict[str, str]) -> dict[str, Any]:
    _aws(env, "ec2", "wait", "instance-running", "--instance-ids", instance_id)
    response = _aws(env, "ec2", "describe-instances", "--instance-ids", instance_id)
    return response["Reservations"][0]["Instances"][0]
