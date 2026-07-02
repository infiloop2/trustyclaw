# IAM Policy Notes

`iam_policy.json` is valid AWS IAM JSON, so it cannot include inline comments.
This file explains why each statement exists and why its scope is shaped that
way.

| Sid | Why It Is Needed | Scope Rationale |
| --- | --- | --- |
| `Ec2Discovery` | Lets deploy find the default VPC, public subnet, route tables, existing TrustyClaw instances, security groups, volumes, and instance attributes. | EC2 describe APIs do not support meaningful resource-level scoping, so they use `Resource: "*"` and stay read-only. |
| `CreateTaggedTrustyClawResources` | Lets deploy create the EC2 instance, data volumes, and security group only when the request includes the TrustyClaw ownership tag. | Uses `Resource: "*"` because these create APIs authorize multiple resource checks, but requires `aws:RequestTag/trustyclaw-host=true` so created resources must be tagged at creation. |
| `UseEc2CreateDependencies` | Lets `RunInstances` and `CreateSecurityGroup` pass EC2's checks for referenced resources such as the Ubuntu AMI, subnet, network interface, and VPC. | These dependencies are not newly created TrustyClaw resources and cannot be bounded by the TrustyClaw request tag, so they are listed by resource type and have no tag condition. |
| `RunInstancesWithTrustyClawSecurityGroups` | Lets `RunInstances` use the selected existing security group. | The security group must already have `aws:ResourceTag/trustyclaw-host=true`, which prevents launching with arbitrary security groups. |
| `TagOnlyDuringTrustyClawResourceCreation` | Lets AWS apply tag specifications during `RunInstances`, `CreateVolume`, and `CreateSecurityGroup`. | Requires `aws:RequestTag/trustyclaw-host=true` and `ec2:CreateAction` so the permission only covers tag-on-create, not standalone tagging of arbitrary existing resources. |
| `ManageOnlyTrustyClawResources` | Lets deploy read console output, attach volumes, mark durable data volumes not to delete on instance termination, update security group rules, start, stop, or terminate instances, and delete cleanup resources. | Uses `Resource: "*"` because these EC2 APIs have mixed resource behavior, but requires `aws:ResourceTag/trustyclaw-host=true` so only TrustyClaw-owned resources can be managed. |
| `UbuntuAmiLookup` | Lets deploy resolve the Canonical Ubuntu SSM parameter used to find the base AMI. | Scoped to Canonical public SSM parameters; it does not grant broad SSM parameter access. |

The policy intentionally uses both tag condition types:

- `aws:RequestTag/trustyclaw-host` controls tags supplied in a create request.
- `aws:ResourceTag/trustyclaw-host` controls existing resources that already
  carry the TrustyClaw ownership tag.
