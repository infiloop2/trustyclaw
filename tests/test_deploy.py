from __future__ import annotations

import copy
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from host.config import ConfigError, parse_input_config
from host import deploy
from host.runtime import read_network_state, update_network_policy, update_provider_account
from host.runtime.network_policy import load_policy, load_policy_updated_at, save_policy
from host.runtime.state import (
    append_network_event,
    read_proxy_claude_account,
    read_proxy_openai_account_id,
)


def sample_config() -> dict[str, object]:
    return {
        "agent_name": "trustyclaw-test",
        "aws_region": "us-east-1",
        "aws_access_key_id_env": "TEST_AWS_ACCESS_KEY_ID",
        "aws_secret_access_key_env": "TEST_AWS_SECRET_ACCESS_KEY",
        "ssh_public_key": "ssh-ed25519 AAAATEST operator@example",
        "ssh_port_opened": True,
    }


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

        with patch("host.deploy._aws", side_effect=responses):
            self.assertEqual(deploy._default_network(config, {}), ("vpc-1", "subnet-public"))

    def test_default_network_rejects_default_vpc_without_public_subnet(self) -> None:
        config = parse_input_config(sample_config())
        responses = [
            {"Vpcs": [{"VpcId": "vpc-1"}]},
            {"Subnets": [{"SubnetId": "subnet-private"}]},
            {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1", "State": "active"}]}]},
        ]

        with patch("host.deploy._aws", side_effect=responses):
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

        with patch("host.deploy._aws", side_effect=responses):
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

        with patch("host.deploy._aws", side_effect=fake_aws):
            self.assertEqual(deploy._ensure_security_group(config, {}, "vpc-1"), "sg-1")

        # SSH ingress stays open because SSH tunneling is currently the only
        # supported way to access the admin API and UI.
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
        # Egress is pinned to HTTP, HTTPS, and NTP — never all-protocol.
        egress_ports = sorted(json.loads(call[-1])[0]["FromPort"] for call in egress)
        self.assertEqual(egress_ports, [80, 123, 443])
        self.assertNotIn('"IpProtocol": "-1"', " ".join(call[-1] for call in egress))

    def test_existing_instance_lookup_requires_trustyclaw_owner_tag(self) -> None:
        config = parse_input_config(sample_config())
        calls: list[tuple[str, ...]] = []

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            return {"Reservations": [{"Instances": [{"InstanceId": "i-owned"}]}]}

        with patch("host.deploy._aws", side_effect=fake_aws):
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

        with patch("host.deploy._aws", side_effect=fake_aws):
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

        with patch("host.deploy._aws", side_effect=fake_aws):
            volumes, created = deploy._ensure_storage_volumes(
                config,
                {},
                instance_id="i-123",
                availability_zone="us-east-1a",
            )

        self.assertEqual(volumes, {"admin": "vol-admin", "agent": "vol-agent"})
        self.assertEqual(created, ["vol-admin", "vol-agent"])
        create_calls = [call for call in calls if call[:2] == ("ec2", "create-volume")]
        self.assertEqual(len(create_calls), 2)
        self.assertIn("--availability-zone", create_calls[0])
        self.assertIn("us-east-1a", create_calls[0])
        self.assertIn("--encrypted", create_calls[0])
        self.assertEqual(create_calls[0][create_calls[0].index("--size") + 1], "8")
        self.assertEqual(create_calls[1][create_calls[1].index("--size") + 1], "8")
        attach_calls = [call for call in calls if call[:2] == ("ec2", "attach-volume")]
        self.assertEqual(len(attach_calls), 2)
        self.assertIn("/dev/sdf", attach_calls[0])
        self.assertIn("/dev/sdg", attach_calls[1])

    def test_storage_volume_lookup_rejects_attached_or_duplicate_state(self) -> None:
        config = parse_input_config(sample_config())

        with patch(
            "host.deploy._aws",
            return_value={"Volumes": [{"VolumeId": "vol-admin", "State": "in-use", "AvailabilityZone": "us-east-1a"}]},
        ):
            with self.assertRaisesRegex(ConfigError, "is in-use"):
                deploy._find_available_storage_volume(config, {}, "admin", "us-east-1a")

        with patch(
            "host.deploy._aws",
            return_value={
                "Volumes": [
                    {"VolumeId": "vol-admin-a", "State": "available", "AvailabilityZone": "us-east-1a"},
                    {"VolumeId": "vol-admin-b", "State": "available", "AvailabilityZone": "us-east-1a"},
                ]
            },
        ):
            with self.assertRaisesRegex(ConfigError, "multiple TrustyClaw admin volumes"):
                deploy._find_available_storage_volume(config, {}, "admin", "us-east-1a")

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

        with patch("host.deploy._aws", side_effect=fake_aws):
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
        with patch("host.deploy._aws", side_effect=responses):
            self.assertEqual(deploy._existing_storage_volume_availability_zone(config, {}), "us-east-1a")

        responses = [
            {"Volumes": [{"VolumeId": "vol-admin", "State": "available", "AvailabilityZone": "us-east-1a"}]},
            {"Volumes": [{"VolumeId": "vol-agent", "State": "available", "AvailabilityZone": "us-east-1b"}]},
        ]
        with patch("host.deploy._aws", side_effect=responses):
            with self.assertRaisesRegex(ConfigError, "split across availability zones"):
                deploy._existing_storage_volume_availability_zone(config, {})

    def test_main_validates_storage_volumes_before_terminating_existing_host(self) -> None:
        calls: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(sample_config()))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(os.environ, {"TEST_AWS_ACCESS_KEY_ID": "AKIATEST", "TEST_AWS_SECRET_ACCESS_KEY": "secret"}), \
                        patch("host.deploy._find_existing_instances", side_effect=lambda *_args: calls.append("find_instances") or ["i-old"]), \
                        patch("host.deploy._existing_storage_volume_availability_zone", side_effect=lambda *_args: calls.append("validate_storage") or "us-east-1a"), \
                        patch("host.deploy._confirm_upgrade", side_effect=lambda *_args, **_kwargs: calls.append("confirm")), \
                        patch("host.deploy._default_network", side_effect=lambda *_args, **_kwargs: calls.append("preflight_network") or ("vpc-1", "subnet-1")), \
                        patch("host.deploy._terminate_instances", side_effect=lambda *_args: calls.append("terminate")), \
                        patch("host.deploy._generate_deploy_key", side_effect=lambda workdir: Path(workdir) / "deploy_key"), \
                        patch("host.deploy._launch_instance", return_value=("i-123", "sg-1")) as launch_instance, \
                        patch(
                            "host.deploy._wait_for_instance",
                            return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                        ), \
                        patch("host.deploy._ensure_storage_volumes", return_value=({"admin": "vol-admin", "agent": "vol-agent"}, [])), \
                        patch("host.deploy._provision_over_ssh"), \
                        patch("host.deploy._aws", return_value={}), \
                        patch("sys.stdout", _StringOutput()):
                    self.assertEqual(deploy.main(["--config", str(config_path)]), 0)
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
            config_path.write_text(json.dumps(sample_config()))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(os.environ, {"TEST_AWS_ACCESS_KEY_ID": "AKIATEST", "TEST_AWS_SECRET_ACCESS_KEY": "secret"}), \
                        patch("host.deploy._find_existing_instances", side_effect=lambda *_args: calls.append("find_instances") or ["i-old"]), \
                        patch("host.deploy._existing_storage_volume_availability_zone", side_effect=lambda *_args: calls.append("validate_storage") or "us-east-1a"), \
                        patch("host.deploy._confirm_upgrade", side_effect=lambda *_args, **_kwargs: calls.append("confirm")), \
                        patch("host.deploy._default_network", side_effect=ConfigError("AWS default VPC has no public subnet in us-east-1a")), \
                        patch("host.deploy._terminate_instances", side_effect=lambda *_args: calls.append("terminate")), \
                        patch("host.deploy._launch_instance", side_effect=AssertionError("_launch_instance should not run")), \
                        patch("sys.stdout", _StringOutput()), \
                        patch("sys.stderr", _StringOutput()):
                    self.assertEqual(deploy.main(["--config", str(config_path)]), 2)
            finally:
                os.chdir(cwd)

        self.assertEqual(calls, ["validate_storage", "find_instances", "confirm"])

    def test_main_allow_upgrade_skips_prompt_and_can_use_stable_admin_password(self) -> None:
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
                        patch("host.deploy._find_existing_instances", return_value=["i-old"]), \
                        patch("host.deploy._existing_storage_volume_availability_zone", return_value="us-east-1a"), \
                        patch("host.deploy._default_network", return_value=("vpc-1", "subnet-1")), \
                        patch("host.deploy._terminate_instances"), \
                        patch("host.deploy._generate_deploy_key", side_effect=lambda workdir: Path(workdir) / "deploy_key"), \
                        patch("host.deploy._launch_instance", return_value=("i-123", "sg-1")), \
                        patch(
                            "host.deploy._wait_for_instance",
                            return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                        ), \
                        patch("host.deploy._ensure_storage_volumes", return_value=({"admin": "vol-admin", "agent": "vol-agent"}, [])), \
                        patch("host.deploy._provision_over_ssh") as provision, \
                        patch("host.deploy._aws", return_value={}), \
                        patch("builtins.input", side_effect=AssertionError("input should not be called")), \
                        patch("sys.stdout", _StringOutput()):
                    self.assertEqual(
                        deploy.main(
                            [
                                "--config",
                                str(config_path),
                                "--allow-upgrade-or-recover",
                                "--admin-password-env",
                                "STAGE_ADMIN_PASSWORD",
                            ]
                        ),
                        0,
                    )
            finally:
                os.chdir(cwd)

        self.assertEqual(provision.call_args.args[1], "stable-admin")

    def test_reset_storage_dangerous_delete_deletes_available_data_volumes(self) -> None:
        config = parse_input_config(sample_config())
        calls: list[tuple[str, ...]] = []
        volumes = {
            "admin": {"VolumeId": "vol-admin", "State": "available", "AvailabilityZone": "us-east-1a"},
            "agent": {"VolumeId": "vol-agent", "State": "available", "AvailabilityZone": "us-east-1a"},
        }

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:2] == ("ec2", "describe-volumes"):
                role = next(value.split("=")[-1] for value in args if value.startswith("Name=tag:trustyclaw-host-volume-role"))
                return {"Volumes": [volumes[role]]}
            return {}

        with patch("host.deploy._aws", side_effect=fake_aws):
            self.assertEqual(deploy._delete_storage_volumes(config, {}), ["vol-admin", "vol-agent"])
        deletes = [call for call in calls if call[:2] == ("ec2", "delete-volume")]
        waits = [call for call in calls if call[:3] == ("ec2", "wait", "volume-deleted")]
        self.assertEqual(deletes, [
            ("ec2", "delete-volume", "--volume-id", "vol-admin"),
            ("ec2", "delete-volume", "--volume-id", "vol-agent"),
        ])
        self.assertEqual(waits, [
            ("ec2", "wait", "volume-deleted", "--volume-ids", "vol-admin"),
            ("ec2", "wait", "volume-deleted", "--volume-ids", "vol-agent"),
        ])

    def test_user_data_contains_only_account_setup_and_no_secrets(self) -> None:
        config = parse_input_config(sample_config())
        user_data = deploy._render_user_data(config, "ssh-ed25519 AAAADEPLOY trustyclaw-deploy")

        self.assertLess(len(user_data.encode()), 4096)
        self.assertIn("useradd --create-home --shell /bin/bash trustyclaw-operator", user_data)
        self.assertIn("ssh-ed25519 AAAATEST operator@example", user_data)
        self.assertIn("ssh-ed25519 AAAADEPLOY trustyclaw-deploy", user_data)
        self.assertIn("trustyclaw-operator ALL=(ALL) NOPASSWD:ALL", user_data)
        self.assertIn("gpasswd -d ubuntu sudo", user_data)
        self.assertNotIn("password", user_data.lower())

    def test_rendered_bootstrap_contains_privilege_boundary(self) -> None:
        config = parse_input_config(sample_config())
        bootstrap = deploy._render_bootstrap(config)

        self.assertIn("ADMIN_MOUNT=/mnt/trustyclaw-admin", bootstrap)
        self.assertIn("ADMIN_STATE_DIR=/mnt/trustyclaw-admin/admin-state", bootstrap)
        self.assertIn("PROXY_STATE_DIR=/mnt/trustyclaw-admin/proxy-state", bootstrap)
        self.assertIn("AGENT_MOUNT=/mnt/trustyclaw-agent", bootstrap)
        self.assertIn("AGENT_HOME_PATH=/mnt/trustyclaw-agent/agent-home", bootstrap)
        self.assertIn("admin_volume_id=\"$(payload_value storage_volumes.admin)\"", bootstrap)
        self.assertIn("prepare_volume \"$admin_volume_id\" \"$ADMIN_MOUNT\" TRUSTYCLAW_ADMIN", bootstrap)
        self.assertIn("prepare_volume \"$agent_volume_id\" \"$AGENT_MOUNT\" TRUSTYCLAW_AGENT", bootstrap)
        self.assertIn("TRUSTYCLAW_ADMIN_UID=47741", bootstrap)
        self.assertIn("TRUSTYCLAW_PROXY_UID=47742", bootstrap)
        self.assertIn("TRUSTYCLAW_AGENT_UID=47743", bootstrap)
        self.assertIn("ensure_group trustyclaw-admin \"$TRUSTYCLAW_ADMIN_GID\"", bootstrap)
        self.assertIn("ensure_user trustyclaw-agent \"$TRUSTYCLAW_AGENT_UID\" trustyclaw-agent /mnt/trustyclaw-agent/agent-home", bootstrap)
        self.assertNotIn("remap_preserved_tree_owner", bootstrap)
        self.assertNotIn("-uid \"$old_uid\"", bootstrap)
        self.assertIn("def ensure_regular_file_slot(path: Path) -> None:", bootstrap)
        self.assertIn("if stat.S_ISLNK(mode):", bootstrap)
        self.assertIn("os.unlink(path)", bootstrap)
        self.assertIn('proxy_state / "network_controls.json"', bootstrap)
        self.assertIn('recreate_directory(proxy_state / "generated-certs")', bootstrap)
        self.assertIn("mkfs.ext4 -F -L \"$label\" \"$device\"", bootstrap)
        self.assertIn("UUID=${uuid} ${mount_point} ext4 defaults,nofail 0 2", bootstrap)
        self.assertNotIn("/var/lib/trustyclaw-host", bootstrap)
        self.assertNotIn("ln -s", bootstrap)
        self.assertIn('useradd --uid "$uid" --gid "$group" $extra_args --home-dir "$home" --no-create-home --shell /usr/sbin/nologin "$name"', bootstrap)
        self.assertIn("ensure_user trustyclaw-proxy \"$TRUSTYCLAW_PROXY_UID\" trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state", bootstrap)
        self.assertNotIn("usermod -a -G trustyclaw-admin trustyclaw-proxy", bootstrap)
        self.assertNotIn("--groups trustyclaw-admin", bootstrap)
        self.assertIn('--no-create-home --shell /usr/sbin/nologin "$name"', bootstrap)
        self.assertIn("trustyclaw-admin ALL=(root) NOPASSWD: /usr/local/lib/trustyclaw-host/reboot-host", bootstrap)
        self.assertIn("/usr/local/lib/trustyclaw-host/read-codex-account-id", bootstrap)
        self.assertIn("/usr/local/lib/trustyclaw-host/run-claude-code", bootstrap)
        self.assertIn("/usr/local/lib/trustyclaw-host/read-claude-account", bootstrap)
        self.assertIn("/usr/local/lib/trustyclaw-host/update-network-policy", bootstrap)
        self.assertIn("/usr/local/lib/trustyclaw-host/read-network-state", bootstrap)
        self.assertIn("/usr/local/lib/trustyclaw-host/update-provider-account", bootstrap)
        self.assertIn("chmod 755 /usr/local/lib/trustyclaw-host", bootstrap)
        self.assertIn("User=trustyclaw-admin", bootstrap)
        self.assertIn("User=trustyclaw-proxy", bootstrap)
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
        self.assertIn("rm -f /home/trustyclaw-operator/.ssh/authorized_keys2", bootstrap)
        # A reused admin volume keeps its existing policy; a fresh volume starts
        # with an empty policy through the same validated proxy-owned writer the
        # admin API uses. Network status is derived by the proxy/admin from
        # policy validity and proxy liveness.
        self.assertIn('proxy_state / "network_controls.json"', bootstrap)
        self.assertIn("if [ -f /mnt/trustyclaw-admin/proxy-state/network_controls.json ]; then", bootstrap)
        self.assertIn("else", bootstrap)
        self.assertIn("cat > /tmp/trustyclaw_initial_policy.json <<'JSON'", bootstrap)
        self.assertIn('"managed_ai_provider_network_access": {},', bootstrap)
        self.assertIn('"allowed_network_access": {}', bootstrap)
        self.assertIn(
            "/usr/local/lib/trustyclaw-host/update-network-policy < /tmp/trustyclaw_initial_policy.json >/dev/null",
            bootstrap,
        )
        self.assertNotIn("network_status.json", bootstrap)
        self.assertLess(
            bootstrap.index("/usr/local/lib/trustyclaw-host/update-network-policy < /tmp/trustyclaw_initial_policy.json"),
            bootstrap.index("systemctl enable --now trustyclaw-network-proxy.service"),
        )
        self.assertNotIn("network_policy.write_text", bootstrap)
        # Provider account files are pre-created in the admin-owned metadata
        # directory and the proxy-owned guard directory; neither service user
        # gets direct group access to the other directory.
        self.assertIn("'openai_account.json':", bootstrap)
        self.assertIn("'claude_account.json':", bootstrap)
        self.assertIn("proxy_pin_files = {", bootstrap)
        self.assertIn("if not path.exists():", bootstrap)
        self.assertIn("path.write_text(content)", bootstrap)
        self.assertIn("chown root:root /mnt/trustyclaw-admin", bootstrap)
        self.assertIn("chown trustyclaw-admin:trustyclaw-admin /mnt/trustyclaw-admin/admin-state", bootstrap)
        self.assertIn("chmod 700 /mnt/trustyclaw-admin/admin-state", bootstrap)
        self.assertIn("chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state", bootstrap)
        self.assertIn("chmod 700 /mnt/trustyclaw-admin/proxy-state", bootstrap)
        self.assertIn("chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state/generated-certs", bootstrap)
        self.assertIn(
            "chown trustyclaw-admin:trustyclaw-admin /mnt/trustyclaw-admin/admin-state/config.json",
            bootstrap,
        )
        self.assertIn("/mnt/trustyclaw-admin/admin-state/openai_account.json", bootstrap)
        self.assertIn("/mnt/trustyclaw-admin/admin-state/claude_account.json", bootstrap)
        self.assertIn("/mnt/trustyclaw-admin/proxy-state/openai_account.json", bootstrap)
        self.assertIn("/mnt/trustyclaw-admin/proxy-state/claude_account.json", bootstrap)
        self.assertIn("chmod 600 /mnt/trustyclaw-admin/admin-state/config.json", bootstrap)
        self.assertIn("chmod 600 /mnt/trustyclaw-admin/admin-state/config.json /mnt/trustyclaw-admin/admin-state/state.json /mnt/trustyclaw-admin/admin-state/openai_account.json", bootstrap)
        self.assertNotIn("chmod 640 /mnt/trustyclaw-admin/admin-state/openai_account.json", bootstrap)
        self.assertNotIn("trustyclaw-proxy:trustyclaw-admin", bootstrap)
        self.assertIn("touch /mnt/trustyclaw-admin/proxy-state/.network_policy.lock", bootstrap)
        self.assertIn("chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state/.network_policy.lock", bootstrap)
        self.assertIn("chmod 600 /mnt/trustyclaw-admin/proxy-state/.network_policy.lock", bootstrap)
        self.assertIn("if [ -f /mnt/trustyclaw-admin/proxy-state/network_controls.json ]; then", bootstrap)
        self.assertIn("chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state/network_controls.json", bootstrap)
        self.assertIn("chmod 600 /mnt/trustyclaw-admin/proxy-state/network_controls.json", bootstrap)
        self.assertNotIn("for path in \\", bootstrap)
        self.assertIn("/mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key", bootstrap)
        self.assertIn("chown trustyclaw-agent:trustyclaw-agent /mnt/trustyclaw-agent/agent-home", bootstrap)
        self.assertNotIn("chown -R trustyclaw-admin:trustyclaw-admin /mnt/trustyclaw-admin/admin-state", bootstrap)
        self.assertNotIn("chown -R trustyclaw-agent:trustyclaw-agent /mnt/trustyclaw-agent/agent-home", bootstrap)
        self.assertIn("chmod 711 /mnt/trustyclaw-agent", bootstrap)
        self.assertIn('if [ ! -f "$PROXY_STATE_DIR/network_proxy_ca.key" ]', bootstrap)
        # Managed Codex policy restricts the agent to cached web search.
        self.assertIn("/etc/codex/requirements.toml", bootstrap)
        self.assertIn('allowed_web_search_modes = ["cached"]', bootstrap)
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
        self.assertIn("chmod -R a+rX /usr/local/lib/node_modules", bootstrap)
        self.assertIn("chmod 755 /etc/codex", bootstrap)
        self.assertIn("runuser -u trustyclaw-agent -- env HOME=/mnt/trustyclaw-agent/agent-home", helper_sources)
        # The unused, world-accessible snapd socket is masked.
        self.assertIn("mask snapd.socket", bootstrap)
        # Pending security updates are applied during bootstrap.
        self.assertIn("unattended-upgrade", bootstrap)
        # Node comes from the official tarball, not the bloated apt npm package.
        self.assertIn("nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-", bootstrap)
        apt_line = next(line for line in bootstrap.splitlines() if "apt-get install" in line)
        self.assertNotIn(" npm", apt_line)
        self.assertNotIn(" nodejs", apt_line)

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
            "reboot-host",
            "update-network-policy",
            "read-network-state",
            "update-provider-account",
        ):
            script = (Path(f"host/bootstrap/helpers/{name}.sh").read_text()).replace("@PROXY_PORT@", "7445")
            with tempfile.NamedTemporaryFile("w", delete=False) as handle:
                handle.write(script)
                script_path = handle.name
            self.addCleanup(lambda path=script_path: Path(path).unlink(missing_ok=True))
            subprocess.run(["bash", "-n", script_path], check=True)

    def test_bootstrap_payload_omits_runtime_network_policy(self) -> None:
        config = parse_input_config(sample_config())
        payload = deploy._bootstrap_payload(config, "admin-password", {"admin": "vol-admin", "agent": "vol-agent"})
        self.assertEqual(payload["storage_volumes"], {"admin": "vol-admin", "agent": "vol-agent"})
        self.assertNotIn("network_controls", payload)

    def test_update_network_policy_helper_validates_and_writes_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"TRUSTYCLAW_STATE_DIR": tmp}):
                payload = {
                    "managed_ai_provider_network_access": {"openai": True},
                    "allowed_network_access": {
                        "api.example.com": {"allow_http_methods": ["GET"]}
                    },
                }
                with patch("sys.stdin", _StringInput(json.dumps(payload))), patch("sys.stdout", _StringOutput()) as stdout:
                    self.assertEqual(update_network_policy.main(), 0)

                self.assertEqual(load_policy()["allowed_network_access"]["api.example.com"]["allow_http_methods"], ["GET"])
                for domain in ("api.openai.com", "auth.openai.com", "chatgpt.com"):
                    self.assertNotIn(domain, load_policy()["allowed_network_access"])
                self.assertIsNotNone(load_policy_updated_at())
                self.assertEqual(oct((Path(tmp) / "network_controls.json").stat().st_mode & 0o777), "0o600")
                response = json.loads(stdout.value)
                self.assertNotIn("api.openai.com", response["network_controls"]["allowed_network_access"])

    def test_update_network_policy_helper_rejects_invalid_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"TRUSTYCLAW_STATE_DIR": tmp}):
                with patch("sys.stdin", _StringInput('{"bogus": true}')):
                    self.assertEqual(update_network_policy.main(), 1)
                self.assertFalse((Path(tmp) / "network_controls.json").exists())

    def test_update_network_policy_helper_lock_timeout_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(os.environ, {"TRUSTYCLAW_STATE_DIR": tmp}),
                patch("host.runtime.update_network_policy.POLICY_UPDATE_LOCK_TIMEOUT_SECONDS", 0),
                patch(
                    "host.runtime.update_network_policy.fcntl.flock",
                    side_effect=BlockingIOError(errno.EAGAIN, "busy"),
                ),
            ):
                with self.assertRaises(TimeoutError):
                    with update_network_policy._policy_update_lock():
                        pass

    def test_read_network_state_helper_returns_proxy_owned_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"TRUSTYCLAW_STATE_DIR": tmp}):
                save_policy(
                    {"managed_ai_provider_network_access": {}, "allowed_network_access": {}},
                    "2026-06-08T00:00:00Z",
                )
                append_network_event("https", "GET", "api.example.com", 443, "/", "", True)

                with patch("sys.argv", ["read-network-state", "status"]), patch("sys.stdout", _StringOutput()) as stdout:
                    self.assertEqual(read_network_state.main(), 0)
                self.assertEqual(json.loads(stdout.value), {"status": "active"})

                with patch("sys.argv", ["read-network-state", "policy"]), patch("sys.stdout", _StringOutput()) as stdout:
                    self.assertEqual(read_network_state.main(), 0)
                self.assertEqual(json.loads(stdout.value)["updated_at"], "2026-06-08T00:00:00Z")

                with patch("sys.argv", ["read-network-state", "events", "0"]), patch("sys.stdout", _StringOutput()) as stdout:
                    self.assertEqual(read_network_state.main(), 0)
                self.assertEqual(json.loads(stdout.value)["events"][0]["host"], "api.example.com")

    def test_update_provider_account_helper_writes_proxy_account_pins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"TRUSTYCLAW_STATE_DIR": tmp}):
                with patch(
                    "sys.stdin",
                    _StringInput(json.dumps({"provider": "openai", "account_id": "acct_smoke"})),
                ), patch("sys.stdout", _StringOutput()):
                    self.assertEqual(update_provider_account.main(), 0)
                self.assertEqual(read_proxy_openai_account_id(), "acct_smoke")

                account = {"access_token_sha256": "hash", "account_id": "acct"}
                with patch(
                    "sys.stdin",
                    _StringInput(json.dumps({"provider": "claude", "account": account})),
                ), patch("sys.stdout", _StringOutput()):
                    self.assertEqual(update_provider_account.main(), 0)
                self.assertEqual(read_proxy_claude_account(), account)


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
                [sys.executable, "-m", "host.deploy", "--config", str(config_path)],
                cwd=tmp_path,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

            self.assertIn("Wrote deploy result", proc.stdout)
            result_path = tmp_path / "trustyclaw-test.json"
            result = json.loads(result_path.read_text())
            self.assertEqual(result["admin_ui_local_url"], "http://127.0.0.1:7443")
            self.assertEqual(result["public_dns"], "trustyclaw.example.com")
            self.assertEqual(result["ssh_user"], "trustyclaw-operator")
            self.assertEqual(result["admin_volume_id"], "vol-admin")
            self.assertEqual(result["agent_volume_id"], "vol-agent")
            self.assertEqual(stat.S_IMODE(result_path.stat().st_mode), 0o600)

            calls = [json.loads(line) for line in log_path.read_text().splitlines()]
            run_call = next(call for call in calls if call[1:3] == ["ec2", "run-instances"])
            self.assertIn("--associate-public-ip-address", run_call)
            self.assertIn("subnet-public", run_call)
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
            self.assertEqual(volume_creates[0][volume_creates[0].index("--size") + 1], "8")
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

        with patch("host.deploy._aws", side_effect=responses):
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

        with patch("host.deploy._aws", side_effect=responses):
            self.assertFalse(deploy._subnet_has_public_ipv4_route({}, "vpc-1", "subnet-1"))


if __name__ == "__main__":
    unittest.main()
