from __future__ import annotations

import unittest

from tests.smoke.smoke_aws import AwsSmoke


class AwsSmokeTeardownTests(unittest.TestCase):
    def test_teardown_destroys_tagged_resources_without_deploy_result(self) -> None:
        smoke = AwsSmoke()
        calls: list[tuple[str, ...]] = []
        instances_terminated = False
        volumes_deleted = set()
        security_group_deleted = False

        def fake_aws(*args: str) -> dict:
            nonlocal instances_terminated, security_group_deleted
            calls.append(args)
            if args[:2] == ("ec2", "describe-instances"):
                states = next((arg for arg in args if arg.startswith("Name=instance-state-name,Values=")), "")
                if not instances_terminated and "shutting-down" not in states:
                    return {"Reservations": [{"Instances": [{"InstanceId": "i-smoke"}]}]}
                return {"Reservations": []}
            if args[:2] == ("ec2", "terminate-instances"):
                instances_terminated = True
                return {}
            if args[:2] == ("ec2", "describe-volumes"):
                volumes = [
                    {"VolumeId": "vol-root", "State": "available"},
                    {"VolumeId": "vol-admin", "State": "available"},
                    {"VolumeId": "vol-agent", "State": "available"},
                ]
                return {"Volumes": [volume for volume in volumes if volume["VolumeId"] not in volumes_deleted]}
            if args[:2] == ("ec2", "delete-volume"):
                volumes_deleted.add(args[args.index("--volume-id") + 1])
                return {}
            if args[:2] == ("ec2", "describe-security-groups"):
                if security_group_deleted:
                    return {"SecurityGroups": []}
                return {"SecurityGroups": [{"GroupId": "sg-smoke"}]}
            if args[:2] == ("ec2", "delete-security-group"):
                security_group_deleted = True
                return {}
            if args[:3] == ("ec2", "wait", "instance-terminated"):
                return {}
            if args[:3] == ("ec2", "wait", "volume-available"):
                return {}
            if args[:3] == ("ec2", "wait", "volume-deleted"):
                return {}
            raise AssertionError(f"unexpected AWS call: {args}")

        smoke._aws = fake_aws  # type: ignore[method-assign]
        smoke.teardown()

        self.assertIn(("ec2", "terminate-instances", "--instance-ids", "i-smoke"), calls)
        self.assertEqual(volumes_deleted, {"vol-root", "vol-admin", "vol-agent"})
        self.assertTrue(security_group_deleted)


if __name__ == "__main__":
    unittest.main()
