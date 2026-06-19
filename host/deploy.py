"""Deploy entrypoint: python3 -m host.deploy --config <config.json>

Provisioning happens in two stages:

1. EC2 user data (small, secret-free): create the ``trustyclaw-operator`` account with
   the configured SSH public key, plus a single-use deploy key generated for
   this run.
2. Over SSH with the deploy key: copy the runtime code and the bootstrap
   script to the instance and run the bootstrap as root. Bootstrap removes
   the deploy key when it finishes.

SSH port 22 must stay open because SSH tunneling is currently the only
supported access path for the admin UI and API.

The generated admin password appears only in the local deploy result file.
The host receives just its SHA-256 hash, so nothing on the instance ever
contains the cleartext.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any

from host.config import ConfigError, InputConfig, load_input_config
from host.constants import ADMIN_API_PORT, PROXY_PORT


INSTANCE_TAG_KEY = "trustyclaw-host-agent-name"
OWNER_TAG_KEY = "trustyclaw-host"
VOLUME_ROLE_TAG_KEY = "trustyclaw-host-volume-role"
SSH_USER = "trustyclaw-operator"
INSTANCE_TYPE = "t3.small"
ROOT_VOLUME_SIZE_GB = 16
ADMIN_VOLUME_SIZE_GB = 8
AGENT_VOLUME_SIZE_GB = 8
ADMIN_VOLUME_DEVICE = "/dev/sdf"
AGENT_VOLUME_DEVICE = "/dev/sdg"
SSH_WAIT_ATTEMPTS = 60
SSH_WAIT_SECONDS = 10
SSH_INGRESS = {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
TEMPLATE_DIR = Path(__file__).with_name("bootstrap")


def _load_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text()


def _log(message: str) -> None:
    print(f"[deploy] {message}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy TrustyClaw to AWS EC2")
    parser.add_argument("--config", required=True, help="Path to input config JSON")
    args = parser.parse_args(argv)

    try:
        config = load_input_config(args.config)
        admin_password = secrets.token_urlsafe(32)
        aws_env = _aws_env(config)
        _log(f"region {config.aws_region}; checking for an existing '{config.agent_name}' host")
        preferred_availability_zone = _existing_storage_volume_availability_zone(config, aws_env)
        existing = _find_existing_instances(config, aws_env)
        replacement_network: tuple[str, str] | None = None
        if existing:
            _confirm_redeploy(config.agent_name, existing)
            replacement_network = _default_network(
                config,
                aws_env,
                preferred_availability_zone=preferred_availability_zone,
            )
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
                preferred_availability_zone=preferred_availability_zone,
                network=replacement_network,
            )
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
                )
                _provision_over_ssh(config, admin_password, public_dns, deploy_key, workdir, storage_volumes)
            except BaseException:
                # A failure here leaves a running instance with SSH 22 open to
                # the world and the single-use deploy key still authorized.
                # Tear it down so a retry starts clean and nothing is exposed.
                _log(f"provisioning failed; terminating {instance_id} to avoid a half-provisioned, exposed host")
                try:
                    _terminate_instances([instance_id], aws_env)
                except Exception as cleanup_exc:  # noqa: BLE001 — best-effort cleanup
                    _log(f"warning: could not terminate {instance_id}: {cleanup_exc}")
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
            "admin_password": admin_password,
        }
        output_path = Path(f"{config.agent_name}.json")
        output_path.touch(mode=0o600)
        output_path.chmod(0o600)  # contains the admin password
        output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(f"Wrote deploy result to {output_path}")
        return 0
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
        print(f"deploy command failed: {stderr or exc}", file=sys.stderr)
        return 1


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
    response = _aws(env,
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


def _confirm_redeploy(agent_name: str, instance_ids: list[str]) -> None:
    print(f"TrustyClaw deployment {agent_name!r} already exists: {', '.join(instance_ids)}")
    answer = input("Delete and recreate it? Type the agent name to confirm: ")
    if answer != agent_name:
        raise SystemExit("aborted")


def _terminate_instances(instance_ids: list[str], env: dict[str, str]) -> None:
    _aws(env, "ec2", "terminate-instances", "--instance-ids", *instance_ids)
    _aws(env, "ec2", "wait", "instance-terminated", "--instance-ids", *instance_ids)


def _generate_deploy_key(workdir: Path) -> Path:
    key_path = workdir / "deploy_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-C", "trustyclaw-deploy", "-f", str(key_path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return key_path


def _launch_instance(
    config: InputConfig,
    deploy_key: Path,
    env: dict[str, str],
    *,
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
    user_data = _render_user_data(config, deploy_key.with_suffix(".pub").read_text().strip())
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
        _tag_spec("instance", config.agent_name),
        _tag_spec("volume", config.agent_name),
    )
    return response["Instances"][0]["InstanceId"], security_group_id


def _ensure_storage_volumes(
    config: InputConfig,
    env: dict[str, str],
    *,
    instance_id: str,
    availability_zone: str,
) -> tuple[dict[str, str], list[str]]:
    volumes: dict[str, str] = {}
    created: list[str] = []
    for role, size_gb, device in (
        ("admin", ADMIN_VOLUME_SIZE_GB, ADMIN_VOLUME_DEVICE),
        ("agent", AGENT_VOLUME_SIZE_GB, AGENT_VOLUME_DEVICE),
    ):
        existing = _find_available_storage_volume(config, env, role, availability_zone)
        if existing is None:
            volume_id = _create_storage_volume(config, env, role, size_gb, availability_zone)
            created.append(volume_id)
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
) -> str | None:
    volume = _find_storage_volume(config, env, role)
    if volume is None:
        return None
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
    # SSH stays open because SSH tunneling is currently the only supported
    # way to access the admin API and UI.
    _authorize_if_missing(env, "authorize-security-group-ingress", group_id, SSH_INGRESS)
    # Egress is pinned to HTTP, HTTPS, and NTP: bootstrap downloads and all
    # proxied agent traffic use 80/443, timesync uses UDP 123, and DNS to the
    # VPC resolver bypasses security groups. nftables enforces the per-user
    # policy on the host; this bounds even a fully compromised host.
    for egress in (
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        {"IpProtocol": "udp", "FromPort": 123, "ToPort": 123, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
    ):
        _authorize_if_missing(env, "authorize-security-group-egress", group_id, egress)
    return group_id


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


def _tag_spec(resource_type: str, agent_name: str) -> str:
    return (
        f"ResourceType={resource_type},Tags=["
        f"{{Key={INSTANCE_TAG_KEY},Value={agent_name}}},"
        f"{{Key={OWNER_TAG_KEY},Value=true}},"
        f"{{Key=Name,Value=trustyclaw-host-{agent_name}}}"
        "]"
    )


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


def _provision_over_ssh(
    config: InputConfig,
    admin_password: str,
    public_dns: str,
    deploy_key: Path,
    workdir: Path,
    storage_volumes: dict[str, str],
) -> None:
    payload = _bootstrap_payload(config, admin_password, storage_volumes)
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
    _log("running bootstrap: apt + security updates, npm, agent CLIs, services. This takes")
    _log("several minutes; the host's own output streams below.")
    print("-" * 70, flush=True)
    # Stream the bootstrap output live so progress (and any failure) is visible.
    completed = subprocess.run([*ssh, target, "sudo", "bash", "/tmp/trustyclaw_bootstrap.sh"])
    print("-" * 70, flush=True)
    if completed.returncode != 0:
        raise ConfigError(f"bootstrap failed on the host (exit {completed.returncode}); see the output above")


def _bootstrap_payload(config: InputConfig, admin_password: str, storage_volumes: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "runtime_config": {
            "agent_name": config.agent_name,
            "admin_password_sha256": hashlib.sha256(admin_password.encode()).hexdigest(),
        },
        "network_controls": config.network_controls.to_json(),
        "storage_volumes": storage_volumes or {},
    }


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
    root = Path(__file__).resolve().parents[1]

    def runtime_only(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if "__pycache__" in tarinfo.name or tarinfo.name == "host/deploy.py":
            return None  # deploy.py never runs on the host
        return tarinfo

    with tarfile.open(code_path, "w:gz") as tar:
        tar.add(root / "host", arcname="host", filter=runtime_only)


def _render_user_data(config: InputConfig, deploy_public_key: str) -> str:
    return (
        USER_DATA_TEMPLATE
        .replace("@OPERATOR_PUBLIC_KEY@", config.ssh_public_key)
        .replace("@DEPLOY_PUBLIC_KEY@", deploy_public_key)
    )


def _render_bootstrap(config: InputConfig) -> str:
    ssh_rule = "tcp dport 22 accept"
    return (
        BOOTSTRAP_TEMPLATE
        .replace("@SSH_RULE@", ssh_rule)
        .replace("@ADMIN_PORT@", str(ADMIN_API_PORT))
        .replace("@PROXY_PORT@", str(PROXY_PORT))
    )


# Stage 1, EC2 user data: trustyclaw-operator account and SSH keys only, no secrets.
# The deploy key lands in authorized_keys2 so stage 2 can delete that whole
# file to revoke it.
USER_DATA_TEMPLATE = _load_template("user_data.sh")


# Stage 2, run as root over SSH after the code and payload are copied over.
BOOTSTRAP_TEMPLATE = _load_template("bootstrap.sh")


if __name__ == "__main__":
    raise SystemExit(main())
