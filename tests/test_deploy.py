from __future__ import annotations

import copy
from collections.abc import Iterator
import errno
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from host.config import ConfigError, parse_input_config
from host.cli import lifecycle as deploy
from host.cli import power
from host.runtime import app_platform



class FakeScandir:
    def __init__(self, entries: list[object]) -> None:
        self._entries = entries

    def __enter__(self) -> FakeScandir:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def __iter__(self) -> Iterator[object]:
        return iter(self._entries)


def sample_config() -> dict[str, object]:
    return {
        "agent_name": "trustyclaw-test",
        "aws_region": "us-east-1",
        "aws_access_key_id_env": "TEST_AWS_ACCESS_KEY_ID",
        "aws_secret_access_key_env": "TEST_AWS_SECRET_ACCESS_KEY",
        "operator_connections": [
            {
                "mode": "ssh",
                "ssh_public_key": "ssh-ed25519 AAAATEST operator@example",
            }
        ],
    }


def sample_upgrade_config() -> dict[str, object]:
    config = sample_config()
    config.pop("operator_connections")
    return config


class DeployUnitTests(unittest.TestCase):
    def test_default_network_selects_public_default_subnet(self) -> None:
        config = parse_input_config(sample_config())
        responses = [
            {"Vpcs": [{"VpcId": "vpc-1"}]},
            {
                "Subnets": [
                    {"SubnetId": "subnet-private"},
                    {"SubnetId": "subnet-public"},
                ]
            },
            {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1", "State": "active"}]}]},
            {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1", "State": "active"}]}]},
        ]

        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            self.assertEqual(deploy._default_network(config, {}), ("vpc-1", "subnet-public"))

    def test_default_network_rejects_default_vpc_without_public_subnet(self) -> None:
        config = parse_input_config(sample_config())
        responses = [
            {"Vpcs": [{"VpcId": "vpc-1"}]},
            {"Subnets": [{"SubnetId": "subnet-private"}]},
            {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1", "State": "active"}]}]},
        ]

        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            with self.assertRaisesRegex(ConfigError, "internet gateway"):
                deploy._default_network(config, {})

    def test_default_network_can_prefer_existing_volume_availability_zone(self) -> None:
        config = parse_input_config(sample_config())
        responses = [
            {"Vpcs": [{"VpcId": "vpc-1"}]},
            {
                "Subnets": [
                    {"SubnetId": "subnet-a", "AvailabilityZone": "us-east-1a"},
                    {"SubnetId": "subnet-b", "AvailabilityZone": "us-east-1b"},
                ]
            },
            {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1", "State": "active"}]}]},
        ]

        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            self.assertEqual(
                deploy._default_network(config, {}, preferred_availability_zone="us-east-1b"),
                ("vpc-1", "subnet-b"),
            )

    def test_security_group_opens_ssh_for_provisioning(self) -> None:
        config = parse_input_config(sample_config())
        calls: list[tuple[str, ...]] = []

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:2] == ("ec2", "describe-security-groups") and "--group-ids" not in args:
                return {"SecurityGroups": []}
            if args[:2] == ("ec2", "create-security-group"):
                return {"GroupId": "sg-1"}
            if args[:2] == ("ec2", "describe-security-groups") and "--group-ids" in args:
                return {"SecurityGroups": [{"IpPermissions": [], "IpPermissionsEgress": []}]}
            return {}

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            self.assertEqual(deploy._ensure_security_group(config, {}, "vpc-1"), "sg-1")

        # SSH ingress is opened for provisioning and may be revoked after
        # bootstrap if no persistent SSH endpoint is configured.
        ingress = [call for call in calls if call[:2] == ("ec2", "authorize-security-group-ingress")]
        egress = [call for call in calls if call[:2] == ("ec2", "authorize-security-group-egress")]
        create_group = next(call for call in calls if call[:2] == ("ec2", "create-security-group"))
        create_tags = [call for call in calls if call[:2] == ("ec2", "create-tags")]
        self.assertIn("--tag-specifications", create_group)
        tag_spec = create_group[create_group.index("--tag-specifications") + 1]
        self.assertIn("ResourceType=security-group", tag_spec)
        self.assertIn("Key=trustyclaw-host,Value=true", tag_spec)
        self.assertIn("Key=trustyclaw-host-agent-name,Value=trustyclaw-test", tag_spec)
        self.assertEqual(create_tags, [])
        self.assertEqual(len(ingress), 1)
        self.assertIn('"FromPort": 22', ingress[0][-1])
        # Egress is pinned to HTTP, HTTPS, NTP, and a temporary Cloudflare
        # Tunnel allowance — never all-protocol. The lifecycle CLI revokes 7844
        # after bootstrap when no cloudflare_access endpoint is configured.
        egress_ports = sorted(
            (json.loads(call[-1])[0]["IpProtocol"], json.loads(call[-1])[0]["FromPort"])
            for call in egress
        )
        self.assertEqual(egress_ports, [("tcp", 80), ("tcp", 443), ("tcp", 7844), ("udp", 123), ("udp", 7844)])
        self.assertNotIn('"IpProtocol": "-1"', " ".join(call[-1] for call in egress))

    def test_security_group_can_close_provisioning_ssh_ingress(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:2] == ("ec2", "describe-security-groups"):
                return {
                    "SecurityGroups": [
                        {
                            "IpPermissions": [
                                {
                                    "IpProtocol": "tcp",
                                    "FromPort": 22,
                                    "ToPort": 22,
                                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                                },
                                {
                                    "IpProtocol": "tcp",
                                    "FromPort": 443,
                                    "ToPort": 443,
                                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                                },
                            ],
                        }
                    ]
                }
            return {}

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            deploy._set_security_group_ssh_ingress({}, "sg-1", enabled=False)

        revoke = next(call for call in calls if call[:2] == ("ec2", "revoke-security-group-ingress"))
        revoked_permissions = json.loads(revoke[revoke.index("--ip-permissions") + 1])
        self.assertEqual(len(revoked_permissions), 1)
        self.assertEqual(revoked_permissions[0]["FromPort"], 22)

    def test_security_group_can_close_cloudflare_egress(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:2] == ("ec2", "describe-security-groups"):
                return {
                    "SecurityGroups": [
                        {
                            "IpPermissionsEgress": [
                                {
                                    "IpProtocol": "tcp",
                                    "FromPort": 443,
                                    "ToPort": 443,
                                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                                },
                                {
                                    "IpProtocol": "tcp",
                                    "FromPort": 7844,
                                    "ToPort": 7844,
                                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                                },
                                {
                                    "IpProtocol": "udp",
                                    "FromPort": 7844,
                                    "ToPort": 7844,
                                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                                },
                            ],
                        }
                    ]
                }
            return {}

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            deploy._set_security_group_cloudflare_egress({}, "sg-1", enabled=False)

        revoke = next(call for call in calls if call[:2] == ("ec2", "revoke-security-group-egress"))
        revoked_permissions = json.loads(revoke[revoke.index("--ip-permissions") + 1])
        self.assertEqual(
            sorted((permission["IpProtocol"], permission["FromPort"]) for permission in revoked_permissions),
            [("tcp", 7844), ("udp", 7844)],
        )

    def test_main_finalizes_security_group_from_bootstrap_access_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(sample_config()))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(os.environ, {"TEST_AWS_ACCESS_KEY_ID": "AKIATEST", "TEST_AWS_SECRET_ACCESS_KEY": "secret"}), \
                        patch("host.cli.lifecycle._existing_storage_volume_availability_zone", return_value=None), \
                        patch("host.cli.lifecycle._find_existing_instances", return_value=[]), \
                        patch("host.cli.lifecycle._existing_storage_roles", return_value=set()), \
                        patch("host.cli.lifecycle._generate_deploy_key", side_effect=lambda workdir: Path(workdir) / "deploy_key"), \
                        patch("host.cli.lifecycle._launch_instance", return_value=("i-123", "sg-1")), \
                        patch(
                            "host.cli.lifecycle._wait_for_instance",
                            return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                        ), \
                        patch("host.cli.lifecycle._ensure_storage_volumes", return_value=({"admin": "vol-admin", "agent": "vol-agent"}, [])), \
                        patch(
                            "host.cli.lifecycle._provision_over_ssh",
                            return_value={"ssh_enabled": False, "cloudflare_enabled": False},
                        ), \
                        patch("host.cli.lifecycle._set_security_group_ssh_ingress") as ssh_ingress, \
                        patch("host.cli.lifecycle._set_security_group_cloudflare_egress") as cloudflare_egress, \
                        patch("host.cli.lifecycle_aws._aws", return_value={}), \
                        patch("sys.stdout", _StringOutput()):
                    self.assertEqual(deploy.main_for_mode("deploy", ["--config", str(config_path)]), 0)
            finally:
                os.chdir(cwd)

        ssh_ingress.assert_called_once()
        cloudflare_egress.assert_called_once()
        self.assertEqual(ssh_ingress.call_args.args[1], "sg-1")
        self.assertEqual(cloudflare_egress.call_args.args[1], "sg-1")
        self.assertFalse(ssh_ingress.call_args.kwargs["enabled"])
        self.assertFalse(cloudflare_egress.call_args.kwargs["enabled"])

    def test_existing_instance_lookup_requires_trustyclaw_owner_tag(self) -> None:
        config = parse_input_config(sample_config())
        calls: list[tuple[str, ...]] = []

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            return {"Reservations": [{"Instances": [{"InstanceId": "i-owned"}]}]}

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            self.assertEqual(deploy._find_existing_instances(config, {}), ["i-owned"])

        filters = calls[0][calls[0].index("--filters") + 1:]
        self.assertIn("Name=tag:trustyclaw-host-agent-name,Values=trustyclaw-test", filters)
        self.assertIn("Name=tag:trustyclaw-host,Values=true", filters)

    def test_existing_security_group_without_owner_tag_is_rejected(self) -> None:
        config = parse_input_config(sample_config())

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            if args[:2] == ("ec2", "describe-security-groups") and "--group-ids" not in args:
                return {"SecurityGroups": [{"GroupId": "sg-1", "Tags": []}]}
            if args[:2] == ("ec2", "describe-security-groups") and "--group-ids" in args:
                return {"SecurityGroups": [{"IpPermissions": [], "IpPermissionsEgress": []}]}
            return {}

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            with self.assertRaisesRegex(ConfigError, "not tagged as a TrustyClaw resource"):
                deploy._ensure_security_group(config, {}, "vpc-1")

    def test_iam_policies_restrict_trustyclaw_resource_access(self) -> None:
        policy = json.loads(Path("iam_policy.json").read_text())
        smoke_policy = json.loads(Path("tests/smoke/iam_policy_smoke.json").read_text())
        stage_policy = json.loads(Path("tests/stage/iam_policy_stage.json").read_text())
        for scoped_policy, agent_name in ((smoke_policy, "trustyclaw-smoke"), (stage_policy, "trustyclaw-stage")):
            policy_without_agent_name = copy.deepcopy(scoped_policy)
            scoped_statements = {statement["Sid"]: statement for statement in policy_without_agent_name["Statement"]}
            self.assertEqual(
                scoped_statements["CreateTaggedTrustyClawResources"]["Condition"]["StringEquals"].pop(
                    "aws:RequestTag/trustyclaw-host-agent-name"
                ),
                agent_name,
            )
            self.assertEqual(
                scoped_statements["RunInstancesWithTrustyClawSecurityGroups"]["Condition"]["StringEquals"].pop(
                    "aws:ResourceTag/trustyclaw-host-agent-name"
                ),
                agent_name,
            )
            self.assertEqual(
                scoped_statements["TagOnlyDuringTrustyClawResourceCreation"]["Condition"]["StringEquals"].pop(
                    "aws:RequestTag/trustyclaw-host-agent-name"
                ),
                agent_name,
            )
            self.assertEqual(
                scoped_statements["ManageOnlyTrustyClawResources"]["Condition"]["StringEquals"].pop(
                    "aws:ResourceTag/trustyclaw-host-agent-name"
                ),
                agent_name,
            )
            self.assertEqual(policy, policy_without_agent_name)
        self.assertNotIn("aws:RequestedRegion", json.dumps(policy))
        statements = {statement["Sid"]: statement for statement in policy["Statement"]}

        discovery_actions = statements["Ec2Discovery"]["Action"]
        self.assertNotIn("ec2:RunInstances", discovery_actions)
        self.assertNotIn("ec2:CreateVolume", discovery_actions)
        self.assertNotIn("ec2:CreateSecurityGroup", discovery_actions)
        self.assertNotIn("ec2:CreateTags", discovery_actions)
        self.assertNotIn("ec2:AuthorizeSecurityGroupIngress", discovery_actions)
        self.assertNotIn("ec2:AuthorizeSecurityGroupEgress", discovery_actions)

        create_statement = statements["CreateTaggedTrustyClawResources"]
        create_conditions = create_statement["Condition"]
        self.assertEqual(
            sorted(create_statement["Action"]),
            ["ec2:CreateSecurityGroup", "ec2:CreateVolume", "ec2:RunInstances"],
        )
        self.assertEqual(create_statement["Resource"], "*")
        self.assertEqual(create_conditions["StringEquals"]["aws:RequestTag/trustyclaw-host"], "true")
        self.assertNotIn("ec2:InstanceType", create_conditions["StringEquals"])
        self.assertNotIn("aws:RequestTag/trustyclaw-host-volume-role", create_conditions["StringEquals"])
        self.assertNotIn("ForAllValues:StringEquals", create_conditions)

        dependency_statement = statements["UseEc2CreateDependencies"]
        self.assertEqual(
            sorted(dependency_statement["Action"]),
            ["ec2:CreateSecurityGroup", "ec2:RunInstances"],
        )
        self.assertEqual(
            sorted(dependency_statement["Resource"]),
            [
                "arn:aws:ec2:*:*:network-interface/*",
                "arn:aws:ec2:*:*:subnet/*",
                "arn:aws:ec2:*:*:vpc/*",
                "arn:aws:ec2:*::image/*",
            ],
        )
        self.assertNotIn("Condition", dependency_statement)

        launch_security_group_statement = statements["RunInstancesWithTrustyClawSecurityGroups"]
        self.assertEqual(launch_security_group_statement["Action"], "ec2:RunInstances")
        self.assertEqual(launch_security_group_statement["Resource"], "arn:aws:ec2:*:*:security-group/*")
        self.assertEqual(
            launch_security_group_statement["Condition"]["StringEquals"]["aws:ResourceTag/trustyclaw-host"],
            "true",
        )

        tag_statement = statements["TagOnlyDuringTrustyClawResourceCreation"]
        self.assertEqual(tag_statement["Action"], "ec2:CreateTags")
        tag_conditions = tag_statement["Condition"]
        self.assertEqual(tag_conditions["StringEquals"]["aws:RequestTag/trustyclaw-host"], "true")
        self.assertEqual(
            sorted(tag_conditions["StringEquals"]["ec2:CreateAction"]),
            ["CreateSecurityGroup", "CreateVolume", "RunInstances"],
        )
        self.assertNotIn("ForAllValues:StringEquals", tag_conditions)

        manage_statement = statements["ManageOnlyTrustyClawResources"]
        self.assertEqual(
            sorted(manage_statement["Action"]),
            [
                "ec2:AttachVolume",
                "ec2:AuthorizeSecurityGroupEgress",
                "ec2:AuthorizeSecurityGroupIngress",
                "ec2:DeleteSecurityGroup",
                "ec2:DeleteVolume",
                "ec2:GetConsoleOutput",
                "ec2:ModifyInstanceAttribute",
                "ec2:RevokeSecurityGroupEgress",
                "ec2:RevokeSecurityGroupIngress",
                "ec2:StartInstances",
                "ec2:StopInstances",
                "ec2:TerminateInstances",
            ],
        )
        self.assertEqual(manage_statement["Resource"], "*")
        self.assertEqual(manage_statement["Condition"]["StringEquals"]["aws:ResourceTag/trustyclaw-host"], "true")

        self.assertEqual(
            statements["UbuntuAmiLookup"]["Resource"],
            "arn:aws:ssm:*::parameter/aws/service/canonical/*",
        )

    def test_storage_volumes_are_created_tagged_and_attached(self) -> None:
        config = parse_input_config(sample_config())
        calls: list[tuple[str, ...]] = []

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:2] == ("ec2", "describe-volumes"):
                return {"Volumes": []}
            if args[:2] == ("ec2", "create-volume"):
                tag_spec = args[args.index("--tag-specifications") + 1]
                if "Value=admin" in tag_spec:
                    return {"VolumeId": "vol-admin"}
                if "Value=agent" in tag_spec:
                    return {"VolumeId": "vol-agent"}
                raise AssertionError(f"unexpected tag spec: {tag_spec}")
            return {}

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            created_out: list[str] = []
            volumes, created = deploy._ensure_storage_volumes(
                config,
                {},
                instance_id="i-123",
                availability_zone="us-east-1a",
                created_storage_volumes=created_out,
            )

        self.assertEqual(volumes, {"admin": "vol-admin", "agent": "vol-agent"})
        self.assertEqual(created, ["vol-admin", "vol-agent"])
        self.assertEqual(created_out, ["vol-admin", "vol-agent"])
        create_calls = [call for call in calls if call[:2] == ("ec2", "create-volume")]
        self.assertEqual(len(create_calls), 2)
        self.assertIn("--availability-zone", create_calls[0])
        self.assertIn("us-east-1a", create_calls[0])
        self.assertIn("--encrypted", create_calls[0])
        self.assertEqual(create_calls[0][create_calls[0].index("--size") + 1], "16")
        self.assertEqual(create_calls[1][create_calls[1].index("--size") + 1], "8")
        attach_calls = [call for call in calls if call[:2] == ("ec2", "attach-volume")]
        self.assertEqual(len(attach_calls), 2)
        self.assertIn("/dev/sdf", attach_calls[0])
        self.assertIn("/dev/sdg", attach_calls[1])
        preserve_calls = [call for call in calls if call[:2] == ("ec2", "modify-instance-attribute")]
        self.assertEqual(len(preserve_calls), 2)
        self.assertEqual(preserve_calls[0][preserve_calls[0].index("--instance-id") + 1], "i-123")
        self.assertEqual(preserve_calls[1][preserve_calls[1].index("--instance-id") + 1], "i-123")
        admin_mapping = json.loads(preserve_calls[0][preserve_calls[0].index("--block-device-mappings") + 1])
        agent_mapping = json.loads(preserve_calls[1][preserve_calls[1].index("--block-device-mappings") + 1])
        self.assertEqual(admin_mapping, [{"DeviceName": "/dev/sdf", "Ebs": {"DeleteOnTermination": False}}])
        self.assertEqual(agent_mapping, [{"DeviceName": "/dev/sdg", "Ebs": {"DeleteOnTermination": False}}])

    def test_storage_volume_lookup_rejects_attached_or_duplicate_state(self) -> None:
        config = parse_input_config(sample_config())

        with patch(
            "host.cli.lifecycle_aws._aws",
            return_value={"Volumes": [{"VolumeId": "vol-admin", "State": "in-use", "AvailabilityZone": "us-east-1a"}]},
        ):
            with self.assertRaisesRegex(ConfigError, "is in-use"):
                deploy._find_available_storage_volume(config, {}, "admin", "us-east-1a")

        with patch(
            "host.cli.lifecycle_aws._aws",
            return_value={
                "Volumes": [
                    {"VolumeId": "vol-admin-a", "State": "available", "AvailabilityZone": "us-east-1a"},
                    {"VolumeId": "vol-admin-b", "State": "available", "AvailabilityZone": "us-east-1a"},
                ]
            },
        ):
            with self.assertRaisesRegex(ConfigError, "multiple TrustyClaw admin volumes"):
                deploy._find_available_storage_volume(config, {}, "admin", "us-east-1a")

    def test_existing_storage_volumes_are_preserved_before_instance_termination(self) -> None:
        config = parse_input_config(sample_config())
        calls: list[tuple[str, ...]] = []

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:2] == ("ec2", "describe-volumes"):
                role = next(arg for arg in args if arg.startswith("Name=tag:trustyclaw-host-volume-role,Values="))
                if role.endswith("admin"):
                    return {"Volumes": [{"VolumeId": "vol-admin", "State": "in-use", "AvailabilityZone": "us-east-1a"}]}
                return {"Volumes": [{"VolumeId": "vol-agent", "State": "in-use", "AvailabilityZone": "us-east-1a"}]}
            if args[:2] == ("ec2", "describe-instances"):
                return {
                    "Reservations": [
                        {
                            "Instances": [
                                {
                                    "InstanceId": "i-old",
                                    "BlockDeviceMappings": [
                                        {"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-root"}},
                                        {"DeviceName": "/dev/sdf", "Ebs": {"VolumeId": "vol-admin"}},
                                        {"DeviceName": "/dev/sdg", "Ebs": {"VolumeId": "vol-agent"}},
                                    ],
                                }
                            ]
                        }
                    ]
                }
            return {}

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            deploy._preserve_existing_storage_volumes_on_instance_termination(config, {}, ["i-old"])

        preserve_calls = [call for call in calls if call[:2] == ("ec2", "modify-instance-attribute")]
        self.assertEqual(len(preserve_calls), 2)
        mappings = [
            json.loads(call[call.index("--block-device-mappings") + 1])
            for call in preserve_calls
        ]
        self.assertIn([{"DeviceName": "/dev/sdf", "Ebs": {"DeleteOnTermination": False}}], mappings)
        self.assertIn([{"DeviceName": "/dev/sdg", "Ebs": {"DeleteOnTermination": False}}], mappings)
        self.assertNotIn("/dev/sda1", json.dumps(mappings))

    def test_storage_volume_lookup_can_wait_for_detach_after_replacing_instance(self) -> None:
        config = parse_input_config(sample_config())
        calls: list[tuple[str, ...]] = []
        describe_count = 0

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            nonlocal describe_count
            calls.append(args)
            if args[:2] == ("ec2", "describe-volumes"):
                describe_count += 1
                state = "in-use" if describe_count == 1 else "available"
                return {"Volumes": [{"VolumeId": "vol-admin", "State": state, "AvailabilityZone": "us-east-1a"}]}
            if args[:3] == ("ec2", "wait", "volume-available"):
                return {}
            raise AssertionError(f"unexpected AWS call: {args}")

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            volume_id = deploy._find_available_storage_volume(
                config,
                {},
                "admin",
                "us-east-1a",
                wait_for_detach=True,
            )

        self.assertEqual(volume_id, "vol-admin")
        self.assertIn(("ec2", "wait", "volume-available", "--volume-ids", "vol-admin"), calls)
        self.assertEqual(describe_count, 2)

    def test_existing_storage_volume_az_steers_redeploy_and_detects_split_volumes(self) -> None:
        config = parse_input_config(sample_config())
        responses = [
            {"Volumes": [{"VolumeId": "vol-admin", "State": "available", "AvailabilityZone": "us-east-1a"}]},
            {"Volumes": [{"VolumeId": "vol-agent", "State": "available", "AvailabilityZone": "us-east-1a"}]},
        ]
        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            self.assertEqual(deploy._existing_storage_volume_availability_zone(config, {}), "us-east-1a")

        responses = [
            {"Volumes": [{"VolumeId": "vol-admin", "State": "available", "AvailabilityZone": "us-east-1a"}]},
            {"Volumes": [{"VolumeId": "vol-agent", "State": "available", "AvailabilityZone": "us-east-1b"}]},
        ]
        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            with self.assertRaisesRegex(ConfigError, "split across availability zones"):
                deploy._existing_storage_volume_availability_zone(config, {})

    def test_main_validates_storage_volumes_before_terminating_existing_host(self) -> None:
        calls: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(sample_upgrade_config()))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(os.environ, {"TEST_AWS_ACCESS_KEY_ID": "AKIATEST", "TEST_AWS_SECRET_ACCESS_KEY": "secret"}), \
                        patch("host.cli.lifecycle._find_existing_instances", side_effect=lambda *_args: calls.append("find_instances") or ["i-old"]), \
                        patch("host.cli.lifecycle._existing_storage_volume_availability_zone", side_effect=lambda *_args: calls.append("validate_storage") or "us-east-1a"), \
                        patch("host.cli.lifecycle._existing_storage_roles", return_value={"admin", "agent"}), \
                        patch("host.cli.lifecycle._default_network", side_effect=lambda *_args, **_kwargs: calls.append("preflight_network") or ("vpc-1", "subnet-1")), \
                        patch("host.cli.lifecycle._terminate_instances", side_effect=lambda *_args: calls.append("terminate")), \
                        patch("host.cli.lifecycle._generate_deploy_key", side_effect=lambda workdir: Path(workdir) / "deploy_key"), \
                        patch("host.cli.lifecycle._launch_instance", return_value=("i-123", "sg-1")) as launch_instance, \
                        patch(
                            "host.cli.lifecycle._wait_for_instance",
                            return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                        ), \
                        patch("host.cli.lifecycle._ensure_storage_volumes", return_value=({"admin": "vol-admin", "agent": "vol-agent"}, [])), \
                        patch(
                            "host.cli.lifecycle._provision_over_ssh",
                            return_value={"ssh_enabled": True, "cloudflare_enabled": False},
                        ), \
                        patch("host.cli.lifecycle._set_security_group_ssh_ingress"), \
                        patch("host.cli.lifecycle._set_security_group_cloudflare_egress"), \
                        patch("host.cli.lifecycle_aws._aws", return_value={}), \
                        patch("sys.stdout", _StringOutput()):
                    self.assertEqual(deploy.main_for_mode("upgrade", ["--config", str(config_path)]), 0)
            finally:
                os.chdir(cwd)

        self.assertLess(calls.index("validate_storage"), calls.index("terminate"))
        self.assertLess(calls.index("preflight_network"), calls.index("terminate"))
        launch_instance.assert_called_once()
        self.assertEqual(launch_instance.call_args.kwargs["preferred_availability_zone"], "us-east-1a")
        self.assertEqual(launch_instance.call_args.kwargs["network"], ("vpc-1", "subnet-1"))

    def test_main_does_not_terminate_existing_host_when_replacement_network_fails(self) -> None:
        calls: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(sample_upgrade_config()))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(os.environ, {"TEST_AWS_ACCESS_KEY_ID": "AKIATEST", "TEST_AWS_SECRET_ACCESS_KEY": "secret"}), \
                        patch("host.cli.lifecycle._find_existing_instances", side_effect=lambda *_args: calls.append("find_instances") or ["i-old"]), \
                        patch("host.cli.lifecycle._existing_storage_volume_availability_zone", side_effect=lambda *_args: calls.append("validate_storage") or "us-east-1a"), \
                        patch("host.cli.lifecycle._existing_storage_roles", return_value={"admin", "agent"}), \
                        patch("host.cli.lifecycle._check_existing_version_hints"), \
                        patch(
                            "host.cli.lifecycle._default_network",
                            side_effect=lambda *_args, **_kwargs: calls.append("preflight_network")
                            or (_ for _ in ()).throw(ConfigError("AWS default VPC has no public subnet in us-east-1a")),
                        ), \
                        patch("host.cli.lifecycle._terminate_instances", side_effect=lambda *_args: calls.append("terminate")), \
                        patch("host.cli.lifecycle._launch_instance", side_effect=AssertionError("_launch_instance should not run")), \
                        patch("sys.stdout", _StringOutput()), \
                        patch("sys.stderr", _StringOutput()):
                    self.assertEqual(deploy.main_for_mode("upgrade", ["--config", str(config_path)]), 2)
            finally:
                os.chdir(cwd)

        self.assertEqual(calls, ["validate_storage", "find_instances", "preflight_network"])

    def test_main_refuses_to_replace_instance_when_tagged_version_would_fail_bootstrap(self) -> None:
        current_version = deploy.repo_version()
        cases = [
            ("upgrade", current_version, [], "upgrade requires preserved state older"),
            ("upgrade", "99.0.0", [], "upgrade requires preserved state older"),
            ("reconfigure", "99.0.0", [], "reconfigure requires preserved state to match"),
            ("upgrade", "0.4.9", [], "predates the Postgres storage"),
        ]
        for mode, tagged_version, extra_args, message in cases:
            with self.subTest(mode=mode, tagged_version=tagged_version, extra_args=extra_args):
                calls: list[str] = []

                with tempfile.TemporaryDirectory() as tmp:
                    config_path = Path(tmp) / "config.json"
                    config_path.write_text(json.dumps(sample_config() if mode == "reconfigure" else sample_upgrade_config()))
                    cwd = os.getcwd()
                    os.chdir(tmp)
                    try:
                        with patch.dict(os.environ, {"TEST_AWS_ACCESS_KEY_ID": "AKIATEST", "TEST_AWS_SECRET_ACCESS_KEY": "secret"}), \
                                patch("host.cli.lifecycle._find_existing_instances", side_effect=lambda *_args: calls.append("find_instances") or ["i-old"]), \
                                patch("host.cli.lifecycle._existing_storage_volume_availability_zone", side_effect=lambda *_args: calls.append("validate_storage") or "us-east-1a"), \
                                patch("host.cli.lifecycle._existing_storage_roles", return_value={"admin", "agent"}), \
                                patch(
                                    "host.cli.lifecycle_aws._aws",
                                    return_value={
                                        "Reservations": [
                                            {
                                                "Instances": [
                                                    {
                                                        "InstanceId": "i-old",
                                                        "Tags": [
                                                            {"Key": "trustyclaw-host-version", "Value": tagged_version},
                                                        ],
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                ), \
                                patch("host.cli.lifecycle._terminate_instances", side_effect=lambda *_args: calls.append("terminate")), \
                                patch("host.cli.lifecycle._default_network", side_effect=lambda *_args, **_kwargs: calls.append("preflight_network") or ("vpc-1", "subnet-1")), \
                                patch("host.cli.lifecycle._launch_instance", side_effect=AssertionError("_launch_instance should not run")), \
                                patch("sys.stdout", _StringOutput()), \
                                patch("sys.stderr", _StringOutput()) as stderr:
                            self.assertEqual(deploy.main_for_mode(mode, ["--config", str(config_path), *extra_args]), 2)
                    finally:
                        os.chdir(cwd)

                self.assertEqual(calls, ["validate_storage", "find_instances"])
                self.assertIn(message, stderr.value)

    def test_version_tag_guard_allows_compatible_tags(self) -> None:
        config = parse_input_config(sample_config())
        command = deploy.LifecycleCommand(mode="recover", config_path="config.json", allow_upgrade=True)
        response = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-same",
                            "Tags": [{"Key": "trustyclaw-host-version", "Value": "0.6.0"}],
                        },
                        {
                            "InstanceId": "i-older",
                            "Tags": [{"Key": "trustyclaw-host-version", "Value": "0.5.0"}],
                        },
                    ]
                }
            ]
        }
        with patch("host.cli.lifecycle_aws._aws", return_value=response), patch("sys.stdout", _StringOutput()) as stdout:
            deploy._check_existing_version_hints(command, config, {}, ["i-same", "i-older"], "0.6.0")
        self.assertIn("i-older=0.5.0", stdout.value)

    def test_version_tag_guard_rejects_mode_specific_bootstrap_failures_before_replacement(self) -> None:
        config = parse_input_config(sample_config())
        cases = [
            (deploy.LifecycleCommand(mode="upgrade", config_path="config.json"), "0.6.0", "older than local VERSION"),
            (deploy.LifecycleCommand(mode="recover", config_path="config.json"), "0.5.0", "match local VERSION"),
            (
                deploy.LifecycleCommand(mode="reconfigure", config_path="config.json"),
                "0.5.0",
                "reconfigure requires preserved state to match",
            ),
            (
                deploy.LifecycleCommand(mode="recover", config_path="config.json", allow_upgrade=True),
                "0.7.0",
                "cannot move preserved state backward",
            ),
            # Pre-Postgres state is refused for every mode, before the running
            # host would be replaced.
            (
                deploy.LifecycleCommand(mode="upgrade", config_path="config.json"),
                "0.4.1",
                "predates the Postgres storage",
            ),
            (
                deploy.LifecycleCommand(mode="recover", config_path="config.json", allow_upgrade=True),
                "0.4.1",
                "predates the Postgres storage",
            ),
        ]
        for command, tagged_version, message in cases:
            with self.subTest(command=command, tagged_version=tagged_version):
                response = {
                    "Reservations": [
                        {
                            "Instances": [
                                {
                                    "InstanceId": "i-tagged",
                                    "Tags": [{"Key": "trustyclaw-host-version", "Value": tagged_version}],
                                }
                            ]
                        }
                    ]
                }
                with patch("host.cli.lifecycle_aws._aws", return_value=response):
                    with self.assertRaisesRegex(ConfigError, message):
                        deploy._check_existing_version_hints(command, config, {}, ["i-tagged"], "0.6.0")

    def test_version_tag_guard_rejects_invalid_tags_before_replacement(self) -> None:
        config = parse_input_config(sample_config())
        command = deploy.LifecycleCommand(mode="recover", config_path="config.json", allow_upgrade=True)
        response = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-invalid",
                            "Tags": [{"Key": "trustyclaw-host-version", "Value": "not-a-version"}],
                        }
                    ]
                }
            ]
        }
        with patch("host.cli.lifecycle_aws._aws", return_value=response):
            with self.assertRaisesRegex(ConfigError, "invalid trustyclaw-host-version tag"):
                deploy._check_existing_version_hints(command, config, {}, ["i-invalid"], "0.1.0")

    def test_reconfigure_can_use_stable_admin_password_and_operator_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(sample_config()))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(
                    os.environ,
                    {
                        "TEST_AWS_ACCESS_KEY_ID": "AKIATEST",
                        "TEST_AWS_SECRET_ACCESS_KEY": "secret",
                        "STAGE_ADMIN_PASSWORD": "stable-admin",
                    },
                ), \
                        patch("host.cli.lifecycle._find_existing_instances", return_value=["i-old"]), \
                        patch("host.cli.lifecycle._existing_storage_volume_availability_zone", return_value="us-east-1a"), \
                        patch("host.cli.lifecycle._existing_storage_roles", return_value={"admin", "agent"}), \
                        patch("host.cli.lifecycle._default_network", return_value=("vpc-1", "subnet-1")), \
                        patch("host.cli.lifecycle._terminate_instances"), \
                        patch("host.cli.lifecycle._generate_deploy_key", side_effect=lambda workdir: Path(workdir) / "deploy_key"), \
                        patch("host.cli.lifecycle._launch_instance", return_value=("i-123", "sg-1")), \
                        patch(
                            "host.cli.lifecycle._wait_for_instance",
                            return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                        ), \
                        patch("host.cli.lifecycle._ensure_storage_volumes", return_value=({"admin": "vol-admin", "agent": "vol-agent"}, [])), \
                        patch(
                            "host.cli.lifecycle._provision_over_ssh",
                            return_value={"ssh_enabled": True, "cloudflare_enabled": False},
                        ) as provision, \
                        patch("host.cli.lifecycle._set_security_group_ssh_ingress"), \
                        patch("host.cli.lifecycle._set_security_group_cloudflare_egress"), \
                        patch("host.cli.lifecycle_aws._aws", return_value={}), \
                        patch("builtins.input", side_effect=AssertionError("input should not be called")), \
                        patch("sys.stdout", _StringOutput()):
                    self.assertEqual(
                        deploy.main_for_mode(
                            "reconfigure",
                            [
                                "--config",
                                str(config_path),
                                "--admin-password-env",
                                "STAGE_ADMIN_PASSWORD",
                            ],
                        ),
                        0,
                    )
            finally:
                os.chdir(cwd)

            self.assertEqual(provision.call_args.args[1], "stable-admin")
            self.assertEqual(
                provision.call_args.args[2],
                deploy.runtime_operator_connections_from_input(parse_input_config(sample_config()).operator_connections, os.environ),
            )
            result = json.loads((Path(tmp) / "trustyclaw-test-reconfigure.json").read_text())
            self.assertEqual(result["admin_password"], "stable-admin")

    def test_reconfigure_generates_admin_password_when_env_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(sample_config()))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(
                    os.environ,
                    {
                        "TEST_AWS_ACCESS_KEY_ID": "AKIATEST",
                        "TEST_AWS_SECRET_ACCESS_KEY": "secret",
                    },
                ), \
                        patch("host.cli.lifecycle._find_existing_instances", return_value=["i-old"]), \
                        patch("host.cli.lifecycle._existing_storage_volume_availability_zone", return_value="us-east-1a"), \
                        patch("host.cli.lifecycle._existing_storage_roles", return_value={"admin", "agent"}), \
                        patch("host.cli.lifecycle._default_network", return_value=("vpc-1", "subnet-1")), \
                        patch("host.cli.lifecycle._terminate_instances"), \
                        patch("host.cli.lifecycle._generate_deploy_key", side_effect=lambda workdir: Path(workdir) / "deploy_key"), \
                        patch("host.cli.lifecycle._launch_instance", return_value=("i-123", "sg-1")), \
                        patch(
                            "host.cli.lifecycle._wait_for_instance",
                            return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                        ), \
                        patch("host.cli.lifecycle._ensure_storage_volumes", return_value=({"admin": "vol-admin", "agent": "vol-agent"}, [])), \
                        patch(
                            "host.cli.lifecycle._provision_over_ssh",
                            return_value={"ssh_enabled": True, "cloudflare_enabled": False},
                        ) as provision, \
                        patch("host.cli.lifecycle._set_security_group_ssh_ingress"), \
                        patch("host.cli.lifecycle._set_security_group_cloudflare_egress"), \
                        patch("host.cli.lifecycle_aws._aws", return_value={}), \
                        patch("sys.stdout", _StringOutput()):
                    self.assertEqual(deploy.main_for_mode("reconfigure", ["--config", str(config_path)]), 0)
            finally:
                os.chdir(cwd)

            generated_password = provision.call_args.args[1]
            self.assertIsInstance(generated_password, str)
            self.assertGreater(len(generated_password), 20)
            result = json.loads((Path(tmp) / "trustyclaw-test-reconfigure.json").read_text())
            self.assertEqual(result["admin_password"], generated_password)

    def test_reconfigure_requires_full_operator_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(sample_upgrade_config()))
            with (
                patch.dict(os.environ, {"TEST_AWS_ACCESS_KEY_ID": "AKIATEST", "TEST_AWS_SECRET_ACCESS_KEY": "secret"}),
                patch("sys.stderr", _StringOutput()) as stderr,
            ):
                self.assertEqual(deploy.main_for_mode("reconfigure", ["--config", str(config_path)]), 2)
        self.assertIn("operator_connections", stderr.value)

    def test_access_summary_detects_cloudflare_operator_connection(self) -> None:
        self.assertTrue(deploy._access_summary_includes_cloudflare({"cloudflare_enabled": True}))
        self.assertFalse(deploy._access_summary_includes_cloudflare({"cloudflare_enabled": False}))
        self.assertFalse(deploy._access_summary_includes_cloudflare({"operator_connections": "ssh"}))

    def test_start_existing_instance_writes_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(sample_upgrade_config()))
            result_path = Path(tmp) / "stage.json"
            calls: list[tuple[str, ...]] = []
            describe_instance_count = 0

            def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
                nonlocal describe_instance_count
                calls.append(args)
                if args[:2] == ("ec2", "describe-instances") and "--instance-ids" not in args:
                    return {"Reservations": [{"Instances": [{"InstanceId": "i-stage"}]}]}
                if args[:2] == ("ec2", "describe-instances") and "--instance-ids" in args:
                    describe_instance_count += 1
                    state = "stopped" if describe_instance_count == 1 else "running"
                    return {
                        "Reservations": [
                            {
                                "Instances": [
                                    {
                                        "InstanceId": "i-stage",
                                        "State": {"Name": state},
                                        "PublicDnsName": "stage.example.com",
                                        "PublicIpAddress": "203.0.113.10",
                                    }
                                ]
                            }
                        ]
                    }
                if args[:2] == ("ec2", "describe-volumes"):
                    role = "admin" if "admin" in " ".join(args) else "agent"
                    return {"Volumes": [{"VolumeId": f"vol-{role}", "State": "in-use", "AvailabilityZone": "us-east-1a"}]}
                return {}

            with (
                patch.dict(os.environ, {"TEST_AWS_ACCESS_KEY_ID": "AKIATEST", "TEST_AWS_SECRET_ACCESS_KEY": "secret"}),
                patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws),
                patch("host.cli.power._aws", side_effect=fake_aws),
                patch("sys.stdout", _StringOutput()),
            ):
                self.assertEqual(
                    power.main_for_power_mode("start", ["--config", str(config_path), "--result-file", str(result_path)]),
                    0,
                )

            self.assertIn(("ec2", "start-instances", "--instance-ids", "i-stage"), calls)
            self.assertIn(("ec2", "wait", "instance-running", "--instance-ids", "i-stage"), calls)
            result = json.loads(result_path.read_text())
            self.assertEqual(result["agent_name"], "trustyclaw-test")
            self.assertEqual(result["instance_id"], "i-stage")
            self.assertEqual(result["state"], "running")
            self.assertEqual(result["public_dns"], "stage.example.com")

    def test_stop_existing_instance_writes_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(sample_upgrade_config()))
            result_path = Path(tmp) / "stage-stop.json"
            calls: list[tuple[str, ...]] = []
            describe_instance_count = 0

            def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
                nonlocal describe_instance_count
                calls.append(args)
                if args[:2] == ("ec2", "describe-instances") and "--instance-ids" not in args:
                    return {"Reservations": [{"Instances": [{"InstanceId": "i-stage"}]}]}
                if args[:2] == ("ec2", "describe-instances") and "--instance-ids" in args:
                    describe_instance_count += 1
                    state = "running" if describe_instance_count == 1 else "stopped"
                    return {
                        "Reservations": [
                            {
                                "Instances": [
                                    {
                                        "InstanceId": "i-stage",
                                        "State": {"Name": state},
                                    }
                                ]
                            }
                        ]
                    }
                if args[:2] == ("ec2", "describe-volumes"):
                    role = "admin" if "admin" in " ".join(args) else "agent"
                    return {"Volumes": [{"VolumeId": f"vol-{role}", "State": "in-use", "AvailabilityZone": "us-east-1a"}]}
                return {}

            with (
                patch.dict(os.environ, {"TEST_AWS_ACCESS_KEY_ID": "AKIATEST", "TEST_AWS_SECRET_ACCESS_KEY": "secret"}),
                patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws),
                patch("host.cli.power._aws", side_effect=fake_aws),
                patch("sys.stdout", _StringOutput()),
            ):
                self.assertEqual(
                    power.main_for_power_mode("stop", ["--config", str(config_path), "--result-file", str(result_path)]),
                    0,
                )

            self.assertIn(("ec2", "stop-instances", "--instance-ids", "i-stage"), calls)
            self.assertIn(("ec2", "wait", "instance-stopped", "--instance-ids", "i-stage"), calls)
            result = json.loads(result_path.read_text())
            self.assertEqual(result["state"], "stopped")

    def test_power_commands_reject_operator_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(sample_config()))
            with patch("sys.stderr", _StringOutput()) as stderr:
                self.assertEqual(power.main_for_power_mode("start", ["--config", str(config_path)]), 2)

        self.assertIn("unsupported fields: operator_connections", stderr.value)

    def test_upgrade_does_not_overwrite_existing_deploy_result_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps(sample_upgrade_config()))
            deploy_result_path = tmp_path / "trustyclaw-test-deploy.json"
            deploy_result = {
                "agent_name": "trustyclaw-test",
                "admin_password": "original-password",
                "admin_volume_id": "vol-admin",
                "agent_volume_id": "vol-agent",
            }
            deploy_result_path.write_text(json.dumps(deploy_result))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(os.environ, {"TEST_AWS_ACCESS_KEY_ID": "AKIATEST", "TEST_AWS_SECRET_ACCESS_KEY": "secret"}), \
                        patch("host.cli.lifecycle._find_existing_instances", return_value=["i-old"]), \
                        patch("host.cli.lifecycle._existing_storage_volume_availability_zone", return_value="us-east-1a"), \
                        patch("host.cli.lifecycle._existing_storage_roles", return_value={"admin", "agent"}), \
                        patch("host.cli.lifecycle._check_existing_version_hints"), \
                        patch("host.cli.lifecycle._default_network", return_value=("vpc-1", "subnet-1")), \
                        patch("host.cli.lifecycle._terminate_instances"), \
                        patch("host.cli.lifecycle._generate_deploy_key", side_effect=lambda workdir: Path(workdir) / "deploy_key"), \
                        patch("host.cli.lifecycle._launch_instance", return_value=("i-123", "sg-1")), \
                        patch(
                            "host.cli.lifecycle._wait_for_instance",
                            return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                        ), \
                        patch("host.cli.lifecycle._ensure_storage_volumes", return_value=({"admin": "vol-admin", "agent": "vol-agent"}, [])), \
                        patch(
                            "host.cli.lifecycle._provision_over_ssh",
                            return_value={"ssh_enabled": True, "cloudflare_enabled": False},
                        ), \
                        patch("host.cli.lifecycle._set_security_group_ssh_ingress"), \
                        patch("host.cli.lifecycle._set_security_group_cloudflare_egress"), \
                        patch("host.cli.lifecycle_aws._aws", return_value={}), \
                        patch("sys.stdout", _StringOutput()) as stdout:
                    self.assertEqual(deploy.main_for_mode("upgrade", ["--config", str(config_path)]), 0)
            finally:
                os.chdir(cwd)

            self.assertEqual(json.loads(deploy_result_path.read_text()), deploy_result)
            upgrade_result_path = tmp_path / "trustyclaw-test-upgrade.json"
            upgrade_result = json.loads(upgrade_result_path.read_text())
            self.assertEqual(upgrade_result["agent_name"], "trustyclaw-test")
            self.assertEqual(upgrade_result["version"], deploy.repo_version())
            self.assertNotIn("admin_password", upgrade_result)
            self.assertIn("Wrote upgrade result to trustyclaw-test-upgrade.json", stdout.value)

    def test_result_file_override_can_write_fixed_automation_path(self) -> None:
        command = deploy.LifecycleCommand(mode="upgrade", config_path="config.json", result_file="stage.json")
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                Path("trustyclaw-test-deploy.json").write_text("{}")
                self.assertEqual(deploy._result_path("trustyclaw-test", command), Path("stage.json"))
            finally:
                os.chdir(cwd)

    def test_failed_deploy_reports_created_data_volumes_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(sample_config()))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                created_storage_volumes: list[str] | None = None

                def fake_ensure_storage(*_args, **kwargs):  # type: ignore[no-untyped-def]
                    nonlocal created_storage_volumes
                    created_storage_volumes = kwargs["created_storage_volumes"]
                    created_storage_volumes.extend(["vol-admin", "vol-agent"])
                    return {"admin": "vol-admin", "agent": "vol-agent"}, ["vol-admin", "vol-agent"]

                with patch.dict(os.environ, {"TEST_AWS_ACCESS_KEY_ID": "AKIATEST", "TEST_AWS_SECRET_ACCESS_KEY": "secret"}), \
                        patch("host.cli.lifecycle._find_existing_instances", return_value=[]), \
                        patch("host.cli.lifecycle._existing_storage_volume_availability_zone", return_value=None), \
                        patch("host.cli.lifecycle._existing_storage_roles", return_value=set()), \
                        patch("host.cli.lifecycle._check_existing_version_hints"), \
                        patch("host.cli.lifecycle._generate_deploy_key", side_effect=lambda workdir: Path(workdir) / "deploy_key"), \
                        patch("host.cli.lifecycle._launch_instance", return_value=("i-123", "sg-1")), \
                        patch(
                            "host.cli.lifecycle._wait_for_instance",
                            return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                        ), \
                        patch("host.cli.lifecycle._ensure_storage_volumes", side_effect=fake_ensure_storage), \
                        patch("host.cli.lifecycle._provision_over_ssh", side_effect=ConfigError("bootstrap failed")), \
                        patch("host.cli.lifecycle._terminate_instances") as terminate, \
                        patch("host.cli.lifecycle_aws._aws", return_value={}), \
                        patch("sys.stdout", _StringOutput()) as stdout, \
                        patch("sys.stderr", _StringOutput()):
                    self.assertEqual(deploy.main_for_mode("deploy", ["--config", str(config_path)]), 2)
            finally:
                os.chdir(cwd)

        terminate.assert_called_once()
        self.assertEqual(created_storage_volumes, ["vol-admin", "vol-agent"])
        self.assertIn("vol-admin, vol-agent", stdout.value)
        self.assertIn("delete the tagged volumes before retrying deploy", stdout.value)

    def test_preflight_deploy_rejects_preserved_resources(self) -> None:
        config = parse_input_config(sample_config())
        command = deploy.LifecycleCommand(mode="deploy", config_path="config.json")
        with self.assertRaisesRegex(ConfigError, "no existing TrustyClaw instance"):
            deploy._validate_command_preflight(command, config, ["i-old"], set())
        with self.assertRaisesRegex(ConfigError, "no existing TrustyClaw data volumes"):
            deploy._validate_command_preflight(command, config, [], {"admin"})
        with self.assertRaisesRegex(ConfigError, "previous first-time deploy failed"):
            deploy._validate_command_preflight(command, config, [], {"admin", "agent"})

    def test_preflight_reconfigure_requires_existing_instance(self) -> None:
        config = parse_input_config(sample_config())
        command = deploy.LifecycleCommand(mode="reconfigure", config_path="config.json")
        with self.assertRaisesRegex(ConfigError, "reconfigure requires an existing TrustyClaw instance"):
            deploy._validate_command_preflight(command, config, [], {"admin", "agent"})
        deploy._validate_command_preflight(command, config, ["i-old"], {"admin", "agent"})

    def test_user_data_contains_only_account_setup_and_no_secrets(self) -> None:
        user_data = deploy._render_user_data("ssh-ed25519 AAAADEPLOY trustyclaw-deploy")

        self.assertLess(len(user_data.encode()), 4096)
        self.assertIn("useradd --create-home --shell /bin/bash trustyclaw-operator", user_data)
        self.assertNotIn("ssh-ed25519 AAAATEST operator@example", user_data)
        self.assertNotIn("@OPERATOR_PUBLIC_KEY@", user_data)
        self.assertIn("ssh-ed25519 AAAADEPLOY trustyclaw-deploy", user_data)
        self.assertIn("trustyclaw-operator ALL=(ALL) NOPASSWD:ALL", user_data)
        self.assertIn("gpasswd -d ubuntu sudo", user_data)
        self.assertNotIn("password", user_data.lower())

    def test_rendered_bootstrap_contains_privilege_boundary(self) -> None:
        config = parse_input_config(sample_config())
        bootstrap = deploy._render_bootstrap(config)

        self.assertIn("ADMIN_MOUNT=/mnt/trustyclaw-admin", bootstrap)
        self.assertIn('PGDATA_DIR="/mnt/trustyclaw-admin/postgres/${PG_MAJOR}/main"', bootstrap)
        self.assertIn("PROXY_STATE_DIR=/mnt/trustyclaw-admin/proxy-state", bootstrap)
        self.assertIn("AGENT_MOUNT=/mnt/trustyclaw-agent", bootstrap)
        self.assertIn("AGENT_HOME_PATH=/mnt/trustyclaw-agent/agent-home", bootstrap)
        self.assertIn("admin_volume_id=\"$(payload_value storage_volumes.admin)\"", bootstrap)
        self.assertIn("prepare_volume \"$admin_volume_id\" \"$ADMIN_MOUNT\" TRUSTYCLAW_ADMIN", bootstrap)
        self.assertIn("prepare_volume \"$agent_volume_id\" \"$AGENT_MOUNT\" TRUSTYCLAW_AGENT", bootstrap)
        self.assertIn("TRUSTYCLAW_ADMIN_UID=47741", bootstrap)
        self.assertIn("TRUSTYCLAW_PROXY_UID=47742", bootstrap)
        self.assertIn("TRUSTYCLAW_AGENT_UID=47743", bootstrap)
        self.assertIn("CLOUDFLARED_UID=47744", bootstrap)
        self.assertIn("CLOUDFLARED_GID=47744", bootstrap)
        # The postgres uid is pinned before the packages install: preserved
        # 0600/0700 cluster files on the admin volume must keep a valid owner
        # across root-volume replacement.
        self.assertIn("POSTGRES_UID=47745", bootstrap)
        self.assertIn("Installed app service users use the reserved static range 48000-48099", bootstrap)
        self.assertIn("TRUSTYCLAW_APP_AGENT_CHAT_UID=48000", bootstrap)
        self.assertIn("TRUSTYCLAW_APP_AGENT_CHAT_GID=48000", bootstrap)
        self.assertIn("App ports are host-owned static assignments", bootstrap)
        self.assertIn("APP_AGENT_CHAT_PORT=7450", bootstrap)
        self.assertIn("ensure_user postgres \"$POSTGRES_UID\" postgres /var/lib/postgresql", bootstrap)
        self.assertLess(
            bootstrap.index('ensure_user postgres "$POSTGRES_UID"'),
            bootstrap.index('apt-get install -y -qq "postgresql-${PG_MAJOR}"'),
        )
        self.assertIn("ensure_group trustyclaw-admin \"$TRUSTYCLAW_ADMIN_GID\"", bootstrap)
        self.assertIn("ensure_user trustyclaw-agent \"$TRUSTYCLAW_AGENT_UID\" trustyclaw-agent /mnt/trustyclaw-agent/agent-home", bootstrap)
        self.assertNotIn("remap_preserved_tree_owner", bootstrap)
        self.assertNotIn("-uid \"$old_uid\"", bootstrap)
        self.assertIn("def ensure_regular_file_slot(path: Path) -> None:", bootstrap)
        self.assertIn("if stat.S_ISLNK(mode):", bootstrap)
        self.assertIn("os.unlink(path)", bootstrap)
        self.assertIn('proxy_state / "network_proxy_ca.key"', bootstrap)
        self.assertIn('recreate_directory(proxy_state / "generated-certs")', bootstrap)
        self.assertIn("mkfs.ext4 -F -L \"$label\" \"$device\"", bootstrap)
        self.assertIn("UUID=${uuid} ${mount_point} ext4 defaults,nofail 0 2", bootstrap)
        self.assertNotIn("/var/lib/trustyclaw-host", bootstrap)
        self.assertNotIn("ln -s", bootstrap)
        self.assertIn('useradd --uid "$uid" --gid "$group" $extra_args --home-dir "$home" --no-create-home --shell /usr/sbin/nologin "$name"', bootstrap)
        self.assertIn("ensure_user trustyclaw-proxy \"$TRUSTYCLAW_PROXY_UID\" trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state", bootstrap)
        self.assertIn("ensure_user cloudflared \"$CLOUDFLARED_UID\" cloudflared /nonexistent", bootstrap)
        self.assertIn("ensure_user trustyclaw-app-agent_chat \"$TRUSTYCLAW_APP_AGENT_CHAT_UID\" trustyclaw-app-agent_chat /nonexistent", bootstrap)
        self.assertNotIn("usermod -a -G trustyclaw-admin trustyclaw-proxy", bootstrap)
        self.assertNotIn("--groups trustyclaw-admin", bootstrap)
        self.assertIn('--no-create-home --shell /usr/sbin/nologin "$name"', bootstrap)
        self.assertIn("trustyclaw-admin ALL=(root) NOPASSWD: /usr/local/lib/trustyclaw-host/reboot-host", bootstrap)
        self.assertIn("/usr/local/lib/trustyclaw-host/read-codex-account-id", bootstrap)
        self.assertIn("/usr/local/lib/trustyclaw-host/run-claude-code", bootstrap)
        self.assertIn("/usr/local/lib/trustyclaw-host/read-claude-account", bootstrap)
        self.assertIn("/usr/local/lib/trustyclaw-host/clear-agent-auth", bootstrap)
        self.assertIn("/usr/local/lib/trustyclaw-host/read-agent-file", bootstrap)
        # Network policy, provider pins, and network events live in the
        # database now (proxy role read-only for policy/pins); the three sudo
        # helpers that bridged the old file boundary are gone.
        self.assertNotIn("update-network-policy", bootstrap)
        self.assertNotIn("read-network-state", bootstrap)
        self.assertNotIn("update-provider-account", bootstrap)
        self.assertIn("chmod 755 /usr/local/lib/trustyclaw-host", bootstrap)
        self.assertIn("User=trustyclaw-admin", bootstrap)
        self.assertIn("RuntimeDirectory=trustyclaw-admin-api", bootstrap)
        self.assertIn("RuntimeDirectoryMode=0755", bootstrap)
        self.assertIn("User=trustyclaw-proxy", bootstrap)
        self.assertIn("User=cloudflared", bootstrap)
        self.assertIn("User=trustyclaw-app-agent_chat", bootstrap)
        self.assertIn("Slice=trustyclaw_app.slice", bootstrap)
        self.assertIn("trustyclaw-app-agent_chat.service", bootstrap)
        self.assertIn("python3 -m host.runtime.app_migrate pending agent_chat", bootstrap)
        self.assertIn(
            "runuser -u trustyclaw-app-agent_chat -- env PYTHONPATH=/opt/trustyclaw-host "
            'python3 -m host.runtime.app_migrate apply-sql agent_chat "$app_migration_version"',
            bootstrap,
        )
        self.assertIn(
            "runuser -u trustyclaw-admin -- env PYTHONPATH=/opt/trustyclaw-host "
            'python3 -m host.runtime.app_migrate record agent_chat "$app_migration_version"',
            bootstrap,
        )
        self.assertNotIn('GRANT \\"trustyclaw-app-agent_chat\\" TO \\"trustyclaw-admin\\"', bootstrap)
        self.assertIn("CREATE SCHEMA IF NOT EXISTS app_agent_chat AUTHORIZATION", bootstrap)
        self.assertIn("trustyclaw-app-agent_chat", bootstrap)
        # Credential carry-over (payload for deploy/reconfigure, stored config
        # for upgrade/recover) lives in host.runtime.write_config now; bootstrap
        # passes the operation mode through and stages the effective config for
        # the root-only steps. Behavior is covered by tests/test_write_config.py.
        self.assertIn("python3 -m host.runtime.write_config > /tmp/trustyclaw_effective_config.json", bootstrap)
        self.assertIn("json.dumps({'mode': payload['operation']['mode'], 'runtime_config': payload['runtime_config']})", bootstrap)
        self.assertIn("chmod 600 /tmp/trustyclaw_effective_config.json", bootstrap)
        self.assertIn("pathlib.Path('/tmp/trustyclaw_effective_config.json').read_text()", bootstrap)
        self.assertNotIn("admin-state/config.json", bootstrap)
        self.assertIn("rm -f /tmp/trustyclaw_payload.json /tmp/trustyclaw_effective_config.json", bootstrap)
        self.assertIn("meta skuid 0 accept", bootstrap)
        self.assertIn('meta skuid "trustyclaw-proxy" udp dport 53 accept', bootstrap)
        self.assertIn('meta skuid "trustyclaw-proxy" tcp dport 53 accept', bootstrap)
        self.assertIn('meta skuid "trustyclaw-proxy" tcp dport { 80, 443 } accept', bootstrap)
        self.assertIn("tcp dport 22 accept", bootstrap)
        # The agent must not reach the local DNS stub, while systemd-resolved
        # itself can still query upstream.
        self.assertIn('udp dport 53 meta skuid != 0 drop', bootstrap)
        self.assertIn('meta skuid "systemd-resolve" udp dport 53 accept', bootstrap)
        # The agent must only reach the loopback proxy port; every other local
        # listener stays outside its network boundary.
        self.assertIn('oif lo tcp dport 7445 meta skuid "trustyclaw-agent" accept', bootstrap)
        self.assertIn('oif lo meta skuid "trustyclaw-agent" drop', bootstrap)
        # App services may answer the host admin API reverse proxy but cannot
        # open arbitrary loopback connections, including to the unauthenticated
        # network proxy or browser-facing admin API.
        self.assertIn('oif lo meta skuid "trustyclaw-app-agent_chat" drop', bootstrap)
        self.assertNotIn('oif lo tcp dport 7443 meta skuid "trustyclaw-app-agent_chat" accept', bootstrap)
        self.assertNotIn('oif lo tcp dport 7445 meta skuid "trustyclaw-app-agent_chat" accept', bootstrap)
        # App backend TCP listeners are loopback-only and reachable only from
        # the admin API service uid. Other local users hit the explicit drop
        # before the broad loopback accept.
        self.assertIn('oif lo tcp dport $APP_AGENT_CHAT_PORT meta skuid "trustyclaw-admin" accept', bootstrap)
        self.assertIn("oif lo tcp dport $APP_AGENT_CHAT_PORT drop", bootstrap)
        self.assertLess(
            bootstrap.index('oif lo tcp dport $APP_AGENT_CHAT_PORT meta skuid "trustyclaw-admin" accept'),
            bootstrap.index("oif lo tcp dport $APP_AGENT_CHAT_PORT drop"),
        )
        self.assertLess(
            bootstrap.index("oif lo tcp dport $APP_AGENT_CHAT_PORT drop"),
            bootstrap.index("oif lo accept"),
        )
        self.assertIn("pathlib.Path('/tmp/trustyclaw_cloudflare_rules').write_text(", bootstrap)
        self.assertIn("if cloudflare_enabled else ''", bootstrap)
        self.assertIn("$(cat /tmp/trustyclaw_cloudflare_rules)", bootstrap)
        self.assertIn("rm -f /tmp/trustyclaw_ssh_rule /tmp/trustyclaw_cloudflare_rules", bootstrap)
        self.assertIn("TRUSTYCLAW_BOOTSTRAP_ACCESS_SUMMARY", bootstrap)
        self.assertIn("'ssh_enabled': any(connection.get('mode') == 'ssh' for connection in connections),", bootstrap)
        self.assertIn("'cloudflare_enabled': any(connection.get('mode') == 'cloudflare_access' for connection in connections),", bootstrap)
        self.assertIn("rm -f /home/trustyclaw-operator/.ssh/authorized_keys2", bootstrap)
        self.assertIn("CLOUDFLARED_VERSION=2026.6.1", bootstrap)
        self.assertIn("trustyclaw-cloudflared.service", bootstrap)
        self.assertIn("--token-file /etc/trustyclaw/cloudflared.token", bootstrap)
        self.assertIn("install -m 0750 -o root -g cloudflared -d /etc/trustyclaw", bootstrap)
        self.assertIn("chown root:cloudflared /etc/trustyclaw/cloudflared.token", bootstrap)
        self.assertIn("chmod 640 /etc/trustyclaw/cloudflared.token", bootstrap)
        self.assertNotIn("--token ${TUNNEL_TOKEN}", bootstrap)
        self.assertNotIn("EnvironmentFile=/etc/trustyclaw/cloudflared.env", bootstrap)
        self.assertIn("Cloudflare Access probe", bootstrap)
        self.assertNotIn("curl -k", bootstrap)
        self.assertIn('meta skuid "cloudflared" udp dport 7844 accept', bootstrap)
        # Network policy and provider pins live in the database: no policy
        # file seeding, no pin files, no seeded initial policy (a missing
        # policy row is the fail-closed empty default).
        self.assertNotIn("network_controls.json", bootstrap)
        self.assertNotIn("trustyclaw_initial_policy", bootstrap)
        self.assertNotIn("network_status.json", bootstrap)
        self.assertNotIn("proxy_pin_files", bootstrap)
        self.assertNotIn("proxy-state/openai_account.json", bootstrap)
        self.assertNotIn("proxy-state/claude_account.json", bootstrap)
        self.assertNotIn(".network_policy.lock", bootstrap)
        self.assertIn("chown root:root /mnt/trustyclaw-admin", bootstrap)
        self.assertIn("chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state", bootstrap)
        self.assertIn("chmod 700 /mnt/trustyclaw-admin/proxy-state", bootstrap)
        self.assertIn("chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state/generated-certs", bootstrap)
        # Admin state lives in Postgres now; admin-state/ keeps only the
        # deploy-plane version.json, no runtime JSON state files.
        self.assertIn("chown trustyclaw-admin:trustyclaw-admin /mnt/trustyclaw-admin/admin-state/version.json", bootstrap)
        self.assertNotIn("admin-state/state.json", bootstrap)
        self.assertNotIn("admin-state/events.jsonl", bootstrap)
        self.assertNotIn("admin-state/openai_account.json", bootstrap)
        self.assertNotIn("admin-state/claude_account.json", bootstrap)
        self.assertNotIn("trustyclaw-proxy:trustyclaw-admin", bootstrap)
        self.assertNotIn("for path in \\", bootstrap)
        self.assertIn("/mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key", bootstrap)
        self.assertIn("chown trustyclaw-agent:trustyclaw-agent /mnt/trustyclaw-agent/agent-home", bootstrap)
        self.assertNotIn("chown -R trustyclaw-admin:trustyclaw-admin /mnt/trustyclaw-admin/admin-state", bootstrap)
        self.assertNotIn("chown -R trustyclaw-agent:trustyclaw-agent /mnt/trustyclaw-agent/agent-home", bootstrap)
        self.assertIn("chmod 711 /mnt/trustyclaw-agent", bootstrap)
        self.assertIn('agent_home / "AGENTS.md"', bootstrap)
        self.assertIn('agent_home / "CLAUDE.md"', bootstrap)
        self.assertIn('agent_home / ".codex" / "config.toml"', bootstrap)
        self.assertIn('agent_home / ".claude" / "settings.json"', bootstrap)
        self.assertIn("AGENT_HOME_SOURCE_DIR=/opt/trustyclaw-host/host/bootstrap/agent-home", bootstrap)
        self.assertIn("install -m 0644 -o root -g root", bootstrap)
        self.assertIn("chattr -f -i", bootstrap)
        self.assertIn("chattr +i", bootstrap)
        agent_instructions = Path("host/bootstrap/agent-home/agents_claude.md").read_text()
        codex_config = Path("host/bootstrap/agent-home/.codex/config.toml").read_text()
        claude_settings = Path("host/bootstrap/agent-home/.claude/settings.json").read_text()
        self.assertIn("You are runnign with full permissions", agent_instructions)
        self.assertIn("Do not prompt the operator for local approvals", agent_instructions)
        self.assertIn("TrustyClaw network policy proxy", agent_instructions)
        self.assertIn("github_push_queued_for_approval", agent_instructions)
        self.assertIn("queued for approval as push-<id>", agent_instructions)
        self.assertIn("github_dot_github_rest_write_denied", agent_instructions)
        self.assertIn("TrustyClaw admin UI", agent_instructions)
        self.assertIn("GitHub GraphQL requests are denied by policy", agent_instructions)
        self.assertIn("equivalent REST endpoint with `gh api`", agent_instructions)
        self.assertNotIn("alternate transports", agent_instructions)
        self.assertIn('"$AGENT_HOME_SOURCE_DIR/agents_claude.md" "$AGENT_HOME_PATH/AGENTS.md"', bootstrap)
        self.assertIn('"$AGENT_HOME_SOURCE_DIR/agents_claude.md" "$AGENT_HOME_PATH/CLAUDE.md"', bootstrap)
        self.assertIn('approval_policy = "never"', codex_config)
        self.assertIn('sandbox_mode = "danger-full-access"', codex_config)
        self.assertIn('"defaultMode": "bypassPermissions"', claude_settings)
        self.assertIn('"skipDangerousModePermissionPrompt": true', claude_settings)
        self.assertIn('if [ ! -f "$PROXY_STATE_DIR/network_proxy_ca.key" ]', bootstrap)
        # Managed Codex policy restricts the agent to cached web search and
        # disables Codex-hosted app/plugin connector surfaces.
        self.assertIn("/etc/codex/requirements.toml", bootstrap)
        self.assertIn('allowed_web_search_modes = ["cached"]', bootstrap)
        self.assertIn("[features]", bootstrap)
        self.assertIn("apps = false", bootstrap)
        self.assertIn("plugins = false", bootstrap)
        self.assertIn("tool_search = false", bootstrap)
        self.assertIn("tool_suggest = false", bootstrap)
        # The bootstrap runs with umask 077: the npm-installed CLI and the
        # managed config directory must be opened up so the agent can use
        # them, and the deploy verifies the agent can actually run both CLIs.
        self.assertIn("CODEX_CLI_VERSION=0.140.0", bootstrap)
        self.assertIn("CLAUDE_CODE_VERSION=2.1.177", bootstrap)
        self.assertIn('"@openai/codex@${CODEX_CLI_VERSION}"', bootstrap)
        self.assertIn('"@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"', bootstrap)
        self.assertIn('"codex-cli ${CODEX_CLI_VERSION}"', bootstrap)
        self.assertIn('"${CLAUDE_CODE_VERSION} (Claude Code)"', bootstrap)
        helper_sources = "\n".join(path.read_text() for path in Path("host/bootstrap/helpers").glob("*.sh"))
        self.assertIn("NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/trustyclaw-network-proxy.crt", helper_sources)
        self.assertNotIn("NODE_EXTRA_CA_CERTS=/mnt/trustyclaw-admin/proxy-state/network_proxy_ca.crt", helper_sources)
        self.assertIn('os.environ.get("CLAUDE_CONFIG_DIR"', helper_sources)
        self.assertIn('config_dir / ".credentials.json"', helper_sources)
        self.assertIn('config.get("oauthAccount")', helper_sources)
        self.assertIn('credentials.get("claudeAiOauth")', helper_sources)
        self.assertIn('tokens.get("accessToken")', helper_sources)
        self.assertNotIn('oauth.get("billingType")', helper_sources)
        self.assertNotIn('tokens.get("subscriptionType")', helper_sources)
        self.assertNotIn('config.get("claudeCodeFirstTokenDate")', helper_sources)
        self.assertIn("chmod -R a+rX /usr/local/lib/node_modules", bootstrap)
        self.assertIn("chmod 755 /etc/codex", bootstrap)
        self.assertIn("runuser -u trustyclaw-agent -- env HOME=/mnt/trustyclaw-agent/agent-home", helper_sources)
        self.assertNotIn("-c 'approval_policy=", helper_sources)
        self.assertNotIn("-c 'sandbox_mode=", helper_sources)
        # Agent runtimes are resource-limited: bootstrap installs a slice with
        # CPU weight, memory bounds, and a task cap, and both launch helpers
        # start the runtime in a transient scope under it, so agent processes
        # leave the admin API's service cgroup and the host services keep
        # guaranteed CPU, memory, and PIDs under contention. The slice name
        # must stay a single dash-free component: dashes encode slice nesting,
        # and a nested slice's weight would not compare against system.slice.
        self.assertIn("/etc/systemd/system/trustyclaw_agent.slice", bootstrap)
        self.assertNotIn("trustyclaw-agent.slice", bootstrap)
        self.assertIn("CPUWeight=50", bootstrap)
        self.assertIn("MemoryHigh=70%", bootstrap)
        self.assertIn("MemoryMax=80%", bootstrap)
        # The swap cap is absolute (no percentage form in systemd 249) and
        # must stay below the swapfile bootstrap creates.
        self.assertIn("MemorySwapMax=5G", bootstrap)
        self.assertIn("fallocate -l 6G /swapfile", bootstrap)
        self.assertIn("TasksMax=4096", bootstrap)
        for launch_helper in ("run-codex-app-server", "run-claude-code"):
            launch_source = Path(f"host/bootstrap/helpers/{launch_helper}.sh").read_text()
            self.assertIn(
                "exec systemd-run --quiet --collect --scope --slice=trustyclaw_agent.slice",
                launch_source,
            )
            # The scope must not outlive the admin API: stopping, restarting,
            # or crashing the admin service stops the agent scopes with it.
            self.assertIn("--property=BindsTo=trustyclaw-admin-api.service", launch_source)
        # App backends are long-running services, so bootstrap creates a
        # separate top-level slice and each generated app service joins it.
        # The lower CPU weight is soft: apps can use idle cores, but the admin
        # API and other host services in system.slice stay prioritized under
        # contention.
        self.assertIn("/etc/systemd/system/trustyclaw_app.slice", bootstrap)
        self.assertNotIn("trustyclaw-app.slice", bootstrap)
        self.assertIn(
            "\n".join([
                "cat > /etc/systemd/system/trustyclaw_app.slice <<'UNIT'",
                "[Unit]",
                "Description=TrustyClaw App Backends",
                "",
                "[Slice]",
                "CPUWeight=50",
                "UNIT",
            ]),
            bootstrap,
        )
        self.assertIn("Slice=trustyclaw_app.slice", bootstrap)
        # The unused, world-accessible snapd socket is masked.
        self.assertIn("mask snapd.socket", bootstrap)
        # Pending security updates are applied during bootstrap.
        self.assertIn("unattended-upgrade", bootstrap)
        # Node comes from the official tarball, not the bloated apt npm package.
        self.assertIn("nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-", bootstrap)
        apt_line = next(line for line in bootstrap.splitlines() if "apt-get install" in line)
        self.assertNotIn(" npm", apt_line)
        self.assertNotIn(" nodejs", apt_line)
        # git and gh back the managed GitHub integration; both come from the
        # Ubuntu archive (no third-party apt repo) and ride unattended-upgrades.
        self.assertIn(" git ", apt_line)
        self.assertIn(" gh ", apt_line)
        self.assertIn("requires existing admin state version file", bootstrap)
        self.assertNotIn("legacy/unversioned", bootstrap)
        self.assertNotIn("LEGACY_UNVERSIONED_STATE_VERSION", bootstrap)

    def test_rendered_bootstrap_provisions_every_installed_app(self) -> None:
        bootstrap = deploy._render_bootstrap(parse_input_config(sample_config()))

        apps = app_platform.installed_apps()
        self.assertGreaterEqual(len(apps), 1)
        for app in apps:
            env_prefix = f"TRUSTYCLAW_APP_{app.id.upper()}"
            with self.subTest(app_id=app.id):
                self.assertIn(f"{env_prefix}_UID=", bootstrap)
                self.assertIn(f"{env_prefix}_GID=", bootstrap)
                self.assertIn(f"ensure_group {app.linux_user}", bootstrap)
                self.assertIn(f"ensure_user {app.linux_user} \"${env_prefix}_UID\" {app.linux_user} /nonexistent", bootstrap)
                self.assertIn(f"local  trustyclaw_admin  {app.db_role}  peer", bootstrap)
                self.assertIn(f"rolname = '{app.db_role}'", bootstrap)
                self.assertIn(f'CREATE ROLE "{app.db_role}" LOGIN;', bootstrap)
                self.assertIn(f'CREATE SCHEMA IF NOT EXISTS {app.db_schema} AUTHORIZATION \\"{app.db_role}\\";', bootstrap)
                self.assertIn(f'GRANT CONNECT ON DATABASE trustyclaw_admin TO \\"{app.db_role}\\";', bootstrap)
                self.assertIn(f"python3 -m host.runtime.app_migrate pending {app.id}", bootstrap)
                self.assertIn(
                    f"runuser -u {app.linux_user} -- env PYTHONPATH=/opt/trustyclaw-host "
                    f'python3 -m host.runtime.app_migrate apply-sql {app.id} "$app_migration_version"',
                    bootstrap,
                )
                self.assertIn(
                    f"runuser -u trustyclaw-admin -- env PYTHONPATH=/opt/trustyclaw-host "
                    f'python3 -m host.runtime.app_migrate record {app.id} "$app_migration_version"',
                    bootstrap,
                )
                self.assertIn(f'oif lo ct state established,related meta skuid "{app.linux_user}" accept', bootstrap)
                self.assertIn(f'meta skuid "{app.linux_user}" drop', bootstrap)
                port_var = f"$APP_{app.id.upper()}_PORT"
                self.assertIn(f'oif lo tcp dport {port_var} meta skuid "trustyclaw-admin" accept', bootstrap)
                self.assertIn(f"oif lo tcp dport {port_var} drop", bootstrap)
                self.assertIn(f"cat > /etc/systemd/system/{app.service_name} <<'UNIT'", bootstrap)
                self.assertIn(f"User={app.linux_user}", bootstrap)
                self.assertIn("Slice=trustyclaw_app.slice", bootstrap)
                self.assertIn("Environment=TRUSTYCLAW_APP_ADMIN_API_SOCKET=/run/trustyclaw-admin-api/app-backend.sock", bootstrap)
                self.assertIn(f"Environment=TRUSTYCLAW_APP_PORT={app.port}", bootstrap)
                backend_entrypoint = app.backend_entrypoint.relative_to(app.package_dir)
                self.assertIn(
                    f"ExecStart=/usr/bin/python3 /opt/trustyclaw-host/host/apps/{app.id}/{backend_entrypoint}",
                    bootstrap,
                )
                self.assertIn(f"systemctl enable {app.service_name}", bootstrap)
                self.assertIn(f"systemctl start {app.service_name}", bootstrap)

    def test_rendered_bootstrap_uses_manifest_backend_entrypoint_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            app_dir = Path(temp) / "custom_app"
            app_dir.mkdir()
            backend = app_dir / "server.py"
            backend.write_text("")
            migrations = app_dir / "migrations"
            migrations.mkdir()
            ui = app_dir / "ui"
            ui.mkdir()
            app = app_platform.AppManifest(
                id="custom_app",
                title="Custom $(touch /tmp/unsafe)",
                package_dir=app_dir,
                backend_entrypoint=backend,
                migrations_dir=migrations,
                ui_dir=ui,
                port=7457,
                allocation=app_platform.AppAllocation(uid=48007, gid=48007, port_offset=7),
            )

            with (
                patch("host.cli.lifecycle_bootstrap.app_platform.installed_apps", return_value=[app]),
                patch(
                    "host.cli.lifecycle_bootstrap.app_platform.app_registry",
                    return_value={"custom_app": app_platform.AppAllocation(uid=48007, gid=48007, port_offset=7)},
                ),
            ):
                bootstrap = deploy._render_bootstrap(parse_input_config(sample_config()))

        self.assertIn("cat > /etc/systemd/system/trustyclaw-app-custom_app.service <<'UNIT'", bootstrap)
        self.assertIn("Description=TrustyClaw App: Custom $(touch /tmp/unsafe)", bootstrap)
        self.assertIn("Slice=trustyclaw_app.slice", bootstrap)
        self.assertIn("Environment=TRUSTYCLAW_APP_PORT=7457", bootstrap)
        self.assertIn("ExecStart=/usr/bin/python3 /opt/trustyclaw-host/host/apps/custom_app/server.py", bootstrap)
        self.assertNotIn("/opt/trustyclaw-host/host/apps/custom_app/backend.py", bootstrap)

    def test_rendered_bootstrap_pins_every_app_uid_in_reserved_range(self) -> None:
        bootstrap = deploy._render_bootstrap(parse_input_config(sample_config()))
        expected_app_uids = {
            "agent_chat": 48000,
        }
        apps = app_platform.installed_apps()
        self.assertEqual(set(expected_app_uids), {app.id for app in apps})
        seen_uids: set[int] = set()

        for app in apps:
            env_prefix = f"TRUSTYCLAW_APP_{app.id.upper()}"
            with self.subTest(app_id=app.id):
                uid_match = re.search(rf"^{env_prefix}_UID=(\d+)$", bootstrap, re.MULTILINE)
                gid_match = re.search(rf"^{env_prefix}_GID=(\d+)$", bootstrap, re.MULTILINE)
                self.assertIsNotNone(uid_match)
                self.assertIsNotNone(gid_match)
                uid = int(uid_match.group(1))
                gid = int(gid_match.group(1))
                self.assertEqual(uid, expected_app_uids[app.id])
                self.assertEqual(gid, uid)
                self.assertGreaterEqual(uid, 48000)
                self.assertLessEqual(uid, 48099)
                self.assertNotIn(uid, seen_uids)
                seen_uids.add(uid)

    def test_rendered_bootstrap_provisions_admin_state_postgres(self) -> None:
        bootstrap = deploy._render_bootstrap(parse_input_config(sample_config()))

        # The Debian default cluster (root volume) is disabled before the
        # server package installs; the real data directory lives on the
        # durable admin volume, versioned by Postgres major.
        self.assertIn("create_main_cluster = false", bootstrap)
        self.assertIn('apt-get install -y -qq "postgresql-${PG_MAJOR}"', bootstrap)
        self.assertIn("PG_MAJOR=14", bootstrap)
        self.assertLess(
            bootstrap.index("create_main_cluster = false"),
            bootstrap.index('apt-get install -y -qq "postgresql-${PG_MAJOR}"'),
        )
        self.assertIn('runuser -u postgres -- "$PG_BIN/initdb" -D "$PGDATA_DIR"', bootstrap)
        # Unix-socket only, peer auth: no TCP listener, admin and superuser
        # roles only, and an explicit reject for everyone else (the agent user
        # has no role and no pg_hba rule that admits it).
        self.assertIn("listen_addresses = ''", bootstrap)
        # Modest server cap by choice; the services bound their own active
        # sessions below it (db.MAX_ACTIVE_CONNECTIONS) and queue bursts
        # client-side, so a proxied request never loses its event insert to
        # connection exhaustion.
        self.assertIn("max_connections = 50", bootstrap)
        self.assertIn("local  trustyclaw_admin  trustyclaw-admin  peer", bootstrap)
        self.assertIn("local  all               postgres          peer", bootstrap)
        self.assertIn("local  all               all               reject", bootstrap)
        self.assertIn('CREATE ROLE "trustyclaw-admin" LOGIN;', bootstrap)
        self.assertIn("createdb --owner=trustyclaw-admin trustyclaw_admin", bootstrap)
        self.assertIn("REVOKE ALL ON DATABASE trustyclaw_admin FROM PUBLIC;", bootstrap)
        # The PUBLIC revoke strips the proxy role's inherited CONNECT; without
        # the explicit grant the fail-closed proxy loses its event log and
        # fails every agent request.
        self.assertIn('GRANT CONNECT ON DATABASE trustyclaw_admin TO \\"trustyclaw-proxy\\";', bootstrap)
        # PG14 leaves the public schema creatable by PUBLIC; only the
        # schema-owning admin role may create objects.
        self.assertIn("REVOKE CREATE ON SCHEMA public FROM PUBLIC;", bootstrap)
        self.assertIn('GRANT CREATE ON SCHEMA public TO \\"trustyclaw-admin\\";', bootstrap)
        # Pre-Postgres (0.4.x) preserved state is refused before anything is
        # modified: this release is deliberately breaking, without in-place
        # legacy import.
        self.assertIn('MIN_STATE_VERSION = "0.5.0"', bootstrap)
        self.assertIn("cannot be upgraded in place", bootstrap)
        # The database runs under its own unit and the admin API waits for it.
        self.assertIn("/etc/systemd/system/trustyclaw-postgres.service", bootstrap)
        self.assertIn("systemctl enable --now trustyclaw-postgres.service", bootstrap)
        self.assertIn(
            "After=network-online.target trustyclaw-network-proxy.service trustyclaw-postgres.service",
            bootstrap,
        )
        # Schema migrations and config seeding run as trustyclaw-admin, after
        # the cluster is up and before the admin API starts.
        migrate_up = "python3 -m host.runtime.migrate up"
        self.assertIn("runuser -u trustyclaw-admin -- env PYTHONPATH=/opt/trustyclaw-host " + migrate_up, bootstrap)
        self.assertIn("python3 -m host.runtime.write_config", bootstrap)
        self.assertLess(
            bootstrap.index("systemctl enable --now trustyclaw-postgres.service"),
            bootstrap.index(migrate_up),
        )
        self.assertLess(
            bootstrap.index(migrate_up),
            bootstrap.index("python3 -m host.runtime.write_config"),
        )
        self.assertLess(
            bootstrap.index("python3 -m host.runtime.write_config"),
            bootstrap.index("systemctl enable --now trustyclaw-admin-api.service"),
        )
        self.assertLess(
            bootstrap.index("rm -f /tmp/trustyclaw_payload.json /tmp/trustyclaw_effective_config.json"),
            bootstrap.index("systemctl enable trustyclaw-app-agent_chat.service"),
        )
        self.assertLess(
            bootstrap.index("systemctl enable trustyclaw-app-agent_chat.service"),
            bootstrap.index("systemctl start trustyclaw-app-agent_chat.service"),
        )
        self.assertLess(
            bootstrap.index("systemctl start trustyclaw-app-agent_chat.service"),
            bootstrap.index("TRUSTYCLAW_TARGET_VERSION"),
        )
        # No database driver anywhere: the runtime speaks the wire protocol
        # itself (host/runtime/pgclient.py).
        self.assertNotIn("psycopg2", bootstrap)
        # Root rewrites the managed database config inside the postgres-owned
        # data directory; those slots (and every data-dir path component) are
        # sanitized against planted symlinks first.
        self.assertIn('pgdata / "postgresql.conf",', bootstrap)
        self.assertIn('pgdata / "pg_hba.conf",', bootstrap)
        self.assertIn('pgdata = admin_mount / "postgres" / os.environ["PG_MAJOR"] / "main"', bootstrap)
        self.assertLess(
            bootstrap.index('pgdata / "postgresql.conf"'),
            bootstrap.index('cat > "$PGDATA_DIR/postgresql.conf"'),
        )

    def test_bootstrap_renders_shared_port_constants(self) -> None:
        from host.constants import PROXY_PORT

        bootstrap = deploy._render_bootstrap(parse_input_config(sample_config()))
        # No placeholders left unrendered, and the rendered ports are the shared
        # constants — so a port change in one place cannot silently drift.
        self.assertNotIn("@PROXY_PORT@", bootstrap)
        self.assertNotIn("@ADMIN_PORT@", bootstrap)
        self.assertIn(f"PROXY_PORT={PROXY_PORT}", bootstrap)
        self.assertIn(f'oif lo tcp dport {PROXY_PORT} meta skuid "trustyclaw-agent" accept', bootstrap)
        helper = (Path("host/bootstrap/helpers/run-codex-app-server.sh").read_text()).replace(
            "@PROXY_PORT@", str(PROXY_PORT)
        )
        self.assertIn(f"HTTPS_PROXY=http://127.0.0.1:{PROXY_PORT}", helper)

    def test_rendered_helper_scripts_have_valid_shell_syntax(self) -> None:
        for name in (
            "run-codex-app-server",
            "read-codex-account-id",
            "run-claude-code",
            "read-claude-account",
            "clear-agent-auth",
            "read-agent-file",
            "reboot-host",
        ):
            script = (Path(f"host/bootstrap/helpers/{name}.sh").read_text()).replace("@PROXY_PORT@", "7445")
            with tempfile.NamedTemporaryFile("w", delete=False) as handle:
                handle.write(script)
                script_path = handle.name
            self.addCleanup(lambda path=script_path: Path(path).unlink(missing_ok=True))
            subprocess.run(["bash", "-n", script_path], check=True)

    def test_agent_file_helper_skips_entries_that_disappear_during_listing(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            stable = home_path / "stable.txt"
            stable.write_text("stable")
            namespace = self._agent_file_helper_namespace(home_path)
            test_case = self

            class StableEntry:
                name = "stable.txt"

                def is_symlink(self) -> bool:
                    return False

                def stat(self, *, follow_symlinks: bool = False) -> os.stat_result:
                    test_case.assertFalse(follow_symlinks)
                    return stable.stat()

            class VanishedEntry:
                name = "vanished.txt"

                def is_symlink(self) -> bool:
                    return False

                def stat(self, *, follow_symlinks: bool = False) -> os.stat_result:
                    test_case.assertFalse(follow_symlinks)
                    raise FileNotFoundError("vanished")

            output = io.StringIO()
            with patch("os.scandir", return_value=FakeScandir([VanishedEntry(), StableEntry()])), patch("sys.stdout", output):
                namespace["list_path"]("/")  # type: ignore[index, operator]
            listed = json.loads(output.getvalue())
            self.assertEqual(listed["path"], "/")
            self.assertEqual([entry["name"] for entry in listed["entries"]], ["stable.txt"])

    def test_agent_file_helper_bounds_directory_scan_work(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            namespace = self._agent_file_helper_namespace(home_path)
            test_case = self

            class StableEntry:
                def __init__(self, name: str) -> None:
                    self.name = name

                def is_symlink(self) -> bool:
                    return False

                def stat(self, *, follow_symlinks: bool = False) -> os.stat_result:
                    test_case.assertFalse(follow_symlinks)
                    path = home_path / self.name
                    path.write_text("stable")
                    return path.stat()

            class ExplodingEntry:
                name = "should-not-be-touched.txt"

                def is_symlink(self) -> bool:
                    raise AssertionError("listing inspected past the scan cap")

            entries = [StableEntry(f"file-{index:04d}.txt") for index in range(1000)] + [ExplodingEntry()]
            output = io.StringIO()
            with patch("os.scandir", return_value=FakeScandir(entries)), patch("sys.stdout", output):
                namespace["list_path"]("/")  # type: ignore[index, operator]
            listed = json.loads(output.getvalue())
            self.assertTrue(listed["truncated"])
            self.assertEqual(len(listed["entries"]), 1000)
            self.assertNotIn("should-not-be-touched.txt", {entry["name"] for entry in listed["entries"]})

    def test_agent_file_helper_opens_files_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            namespace = self._agent_file_helper_namespace(home_path)
            calls: list[tuple[object, int, int | None]] = []

            def fake_open(path: object, flags: int, *_, dir_fd: int | None = None) -> int:
                calls.append((path, flags, dir_fd))
                if path == home_path:
                    return 10
                if path == "fifo":
                    raise OSError(errno.ENXIO, "no writer")
                raise AssertionError(f"unexpected open path: {path!r}")

            with patch("os.open", side_effect=fake_open), patch("os.close"):
                with self.assertRaises(OSError):
                    namespace["read_path"]("/fifo")  # type: ignore[index, operator]
            file_open = next(call for call in calls if call[0] == "fifo")
            self.assertNotEqual(file_open[1] & namespace["NONBLOCK"], 0)  # type: ignore[index, operator]

    def test_agent_file_helper_rejects_directory_symlink_as_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as outside:
            home_path = Path(home)
            (home_path / "outside-dir-link").symlink_to(outside, target_is_directory=True)
            namespace = self._agent_file_helper_namespace(home_path)

            output = io.StringIO()
            with patch("sys.stdout", output), self.assertRaises(SystemExit) as exc:
                namespace["list_path"]("/outside-dir-link")  # type: ignore[index, operator]

            self.assertEqual(exc.exception.code, 3)
            self.assertIn("symlinks are not supported", output.getvalue())

    def _agent_file_helper_namespace(self, home_path: Path) -> dict[str, object]:
        helper = Path("host/bootstrap/helpers/read-agent-file.sh").read_text()
        body = helper.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        namespace: dict[str, object] = {"__name__": "read_agent_file_test"}
        exec(
            compile(
                body.replace('Path("/mnt/trustyclaw-agent/agent-home")', f"Path({str(home_path)!r})"),
                "read-agent-file.py",
                "exec",
            ),
            namespace,
        )
        return namespace

    def test_bootstrap_payload_omits_runtime_network_policy(self) -> None:
        config = parse_input_config(sample_config())
        payload = deploy._bootstrap_payload(
            config,
            "admin-password",
            deploy.runtime_operator_connections_from_input(config.operator_connections or (), {}),
            {"admin": "vol-admin", "agent": "vol-agent"},
            mode="deploy",
            target_version="0.1.0",
        )
        self.assertEqual(payload["storage_volumes"], {"admin": "vol-admin", "agent": "vol-agent"})
        self.assertEqual(payload["operation"], {"mode": "deploy", "target_version": "0.1.0", "allow_upgrade": False})
        self.assertEqual(payload["runtime_config"]["agent_name"], "trustyclaw-test")
        self.assertIn("admin_password_sha256", payload["runtime_config"])
        self.assertEqual(
            payload["runtime_config"]["operator_connections"],
            [{"mode": "ssh", "ssh_public_key": "ssh-ed25519 AAAATEST operator@example"}],
        )
        self.assertNotIn("network_controls", payload)

    def test_upgrade_bootstrap_payload_omits_replacement_operator_connections(self) -> None:
        config = parse_input_config(
            {
                "agent_name": "trustyclaw-test",
                "aws_region": "us-east-1",
                "aws_access_key_id_env": "TEST_AWS_ACCESS_KEY_ID",
                "aws_secret_access_key_env": "TEST_AWS_SECRET_ACCESS_KEY",
            },
            require_operator_connections=False,
        )
        payload = deploy._bootstrap_payload(
            config,
            None,
            None,
            {"admin": "vol-admin", "agent": "vol-agent"},
            mode="upgrade",
            target_version="0.1.0",
        )

        self.assertEqual(payload["runtime_config"], {"agent_name": "trustyclaw-test"})

    def test_runtime_code_archive_excludes_cli_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "trustyclaw-host-code.tar.gz"
            deploy._write_runtime_code_archive(archive)

            with tarfile.open(archive, "r:gz") as tar:
                names = set(tar.getnames())

        self.assertIn("host/runtime/admin_api.py", names)
        self.assertIn("host/version.py", names)
        self.assertIn("host/bootstrap/agent-home/agents_claude.md", names)
        self.assertNotIn("host/bootstrap/agent-home/AGENTS.md", names)
        self.assertNotIn("host/bootstrap/agent-home/CLAUDE.md", names)
        self.assertIn("host/bootstrap/agent-home/.codex/config.toml", names)
        self.assertIn("host/bootstrap/agent-home/.claude/settings.json", names)
        self.assertNotIn("host/cli", names)
        self.assertFalse(any(name.startswith("host/cli/") for name in names))


class FakeCliIntegrationTests(unittest.TestCase):
    def test_deploy_provisions_over_ssh_and_writes_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            log_path = tmp_path / "cli_calls.jsonl"
            for name in ("aws", "ssh", "scp", "ssh-keygen"):
                fake = fake_bin / name
                fake.write_text(_fake_cli_script(name, log_path))
                fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps(sample_config()))
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                    "TEST_AWS_ACCESS_KEY_ID": "access",
                    "TEST_AWS_SECRET_ACCESS_KEY": "secret",
                }
            )

            proc = subprocess.run(
                [sys.executable, "-m", "host.cli.deploy", "--config", str(config_path)],
                cwd=tmp_path,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

            self.assertIn("Wrote deploy result", proc.stdout)
            result_path = tmp_path / "trustyclaw-test-deploy.json"
            result = json.loads(result_path.read_text())
            self.assertEqual(result["admin_ui_local_url"], "http://127.0.0.1:7443")
            self.assertEqual(result["public_dns"], "trustyclaw.example.com")
            self.assertEqual(result["ssh_user"], "trustyclaw-operator")
            self.assertEqual(result["admin_volume_id"], "vol-admin")
            self.assertEqual(result["agent_volume_id"], "vol-agent")
            self.assertEqual(result["version"], deploy.repo_version())
            self.assertEqual(stat.S_IMODE(result_path.stat().st_mode), 0o600)

            calls = [json.loads(line) for line in log_path.read_text().splitlines()]
            run_call = next(call for call in calls if call[1:3] == ["ec2", "run-instances"])
            self.assertIn("--associate-public-ip-address", run_call)
            self.assertIn("subnet-public", run_call)
            self.assertTrue(any(f"Key=trustyclaw-host-version,Value={deploy.repo_version()}" in str(item) for item in run_call))
            # User data is passed as fileb:// so the AWS CLI base64-encodes the raw
            # bytes (a raw string would be base64-decoded under cli_binary_format=base64
            # and corrupt the cloud-init script). Content is covered by the render test.
            user_data = run_call[run_call.index("--user-data") + 1]
            self.assertTrue(user_data.startswith("fileb://"))

            volume_creates = [call for call in calls if call[1:3] == ["ec2", "create-volume"]]
            self.assertEqual(len(volume_creates), 2)
            self.assertIn("--volume-type", volume_creates[0])
            self.assertIn("gp3", volume_creates[0])
            self.assertIn("--encrypted", volume_creates[0])
            self.assertEqual(volume_creates[0][volume_creates[0].index("--size") + 1], "16")
            self.assertEqual(volume_creates[1][volume_creates[1].index("--size") + 1], "8")
            volume_attaches = [call for call in calls if call[1:3] == ["ec2", "attach-volume"]]
            self.assertEqual(len(volume_attaches), 2)
            self.assertIn("vol-admin", volume_attaches[0])
            self.assertIn("vol-agent", volume_attaches[1])

            scp_call = next(call for call in calls if call[0] == "scp")
            copied = " ".join(scp_call)
            self.assertIn("trustyclaw_payload.json", copied)
            self.assertIn("trustyclaw_bootstrap.sh", copied)
            self.assertIn("trustyclaw-host-code.tar.gz", copied)

            bootstrap_call = next(call for call in calls if call[0] == "ssh" and "sudo" in call)
            self.assertIn("bash", bootstrap_call)
            self.assertIn("/tmp/trustyclaw_bootstrap.sh", bootstrap_call)


def _fake_cli_script(name: str, log_path: Path) -> str:
    return f"""#!/usr/bin/env python3
import json
import pathlib
import sys

args = sys.argv[1:]
with open({str(log_path)!r}, "a") as log:
    log.write(json.dumps([{name!r}] + args) + "\\n")

def emit(value):
    print(json.dumps(value))

if {name!r} == "ssh-keygen":
    key = pathlib.Path(args[args.index("-f") + 1])
    key.write_text("fake private key\\n")
    key.with_suffix(".pub").write_text("ssh-ed25519 AAAADEPLOY trustyclaw-deploy\\n")
elif {name!r} == "ssh" and "bash" in args and "/tmp/trustyclaw_bootstrap.sh" in args:
    print('TRUSTYCLAW_BOOTSTRAP_ACCESS_SUMMARY {{"cloudflare_enabled": false, "ssh_enabled": true}}')
elif {name!r} in ("ssh", "scp"):
    pass
elif args[:2] == ["ec2", "describe-instances"] and "--instance-ids" not in args:
    emit({{"Reservations": []}})
elif args[:2] == ["ec2", "describe-instances"] and "--instance-ids" in args:
    emit({{"Reservations": [{{"Instances": [{{"InstanceId": "i-123", "PublicDnsName": "trustyclaw.example.com", "Placement": {{"AvailabilityZone": "us-east-1a"}}}}]}}]}})
elif args[:2] == ["ec2", "describe-volumes"]:
    emit({{"Volumes": []}})
elif args[:2] == ["ec2", "create-volume"]:
    tag_spec = args[args.index("--tag-specifications") + 1]
    if "Value=admin" in tag_spec:
        emit({{"VolumeId": "vol-admin"}})
    elif "Value=agent" in tag_spec:
        emit({{"VolumeId": "vol-agent"}})
    else:
        emit({{"VolumeId": "vol-unknown"}})
elif args[:2] == ["ec2", "attach-volume"]:
    pass
elif args[:2] == ["ec2", "describe-vpcs"]:
    emit({{"Vpcs": [{{"VpcId": "vpc-1"}}]}})
elif args[:2] == ["ec2", "describe-subnets"]:
    emit({{"Subnets": [{{"SubnetId": "subnet-public", "AvailabilityZone": "us-east-1a"}}]}})
elif args[:2] == ["ec2", "describe-route-tables"]:
    emit({{"RouteTables": [{{"Routes": [{{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1", "State": "active"}}]}}]}})
elif args[:2] == ["ec2", "describe-security-groups"] and "--group-ids" not in args:
    emit({{"SecurityGroups": []}})
elif args[:2] == ["ec2", "create-security-group"]:
    emit({{"GroupId": "sg-1"}})
elif args[:2] == ["ec2", "describe-security-groups"] and "--group-ids" in args:
    emit({{"SecurityGroups": [{{"IpPermissions": [], "IpPermissionsEgress": []}}]}})
elif args[:2] == ["ssm", "get-parameter"]:
    emit({{"Parameter": {{"Value": "ami-123"}}}})
elif args[:2] == ["ec2", "run-instances"]:
    emit({{"Instances": [{{"InstanceId": "i-123"}}]}})
elif args[:2] == ["ec2", "wait"]:
    pass
else:
    emit({{}})
"""

class _StringInput:
    def __init__(self, value: str) -> None:
        self.value = value

    def read(self, *args):  # type: ignore[no-untyped-def]
        return self.value


class _StringOutput:
    def __init__(self) -> None:
        self.value = ""

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args):  # type: ignore[no-untyped-def]
        return None

    def write(self, value: str) -> int:
        self.value += value
        return len(value)

    def flush(self) -> None:
        return None


class DeployNetworkTests(unittest.TestCase):
    def test_subnet_requires_active_internet_gateway_default_route(self) -> None:
        responses = [
            {
                "RouteTables": [
                    {
                        "Routes": [
                            {
                                "DestinationCidrBlock": "0.0.0.0/0",
                                "GatewayId": "igw-123",
                                "State": "active",
                            }
                        ]
                    }
                ]
            }
        ]

        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            self.assertTrue(deploy._subnet_has_public_ipv4_route({}, "vpc-1", "subnet-1"))

    def test_subnet_rejects_nat_default_route(self) -> None:
        responses = [
            {
                "RouteTables": [
                    {
                        "Routes": [
                            {
                                "DestinationCidrBlock": "0.0.0.0/0",
                                "NatGatewayId": "nat-123",
                                "State": "active",
                            }
                        ]
                    }
                ]
            }
        ]

        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            self.assertFalse(deploy._subnet_has_public_ipv4_route({}, "vpc-1", "subnet-1"))


if __name__ == "__main__":
    unittest.main()
