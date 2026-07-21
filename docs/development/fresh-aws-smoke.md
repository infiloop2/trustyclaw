# Fresh AWS Smoke

The fresh smoke deploys a real host from scratch, validates the parts unit tests
cannot, then tears the host down. Before calling
`python3 -m host.cli.deploy`, it destroys any stale tagged
`trustyclaw-smoke` EC2 instance, security group, and durable data volumes so the
strict first-install command starts from empty AWS state. It supplies no Codex,
Claude, Pi, or Hermes credential; credential-dependent runtime checks live in
the persistent stage test.

The smoke covers subnet/SG/IMDSv2/SSH provisioning, bootstrap on real Ubuntu,
admin API access over the SSH tunnel, auth rejection, and the real admin UI in
headless Chrome. The browser logs in, opens Mission Pursuit, clicks its
popovers and agent settings, submits a first mission, verifies the expected
pre-provider-login failure, and switches the workspace runtime. The remaining
checks cover all four runtime status/account records and real task insertion,
the single Bedrock provider policy that governs both Pi and Hermes runtimes,
every Bedrock pre-credential denial
(foreign access-key id, cross-region signature, presigned query, session token,
and unavailable proxy credential), real Pi and Hermes launcher startup through
their systemd scopes until the proxy's local missing-credential denial, task
lifecycle edge cases, policy validation,
event pagination, concurrent policy replaces, proxy protocol edge cases, live
network enforcement, managed provider policy validation, tool-service/socket
boundaries, credential-free tool calls, and the network event prune race.

The workflow installs its pinned Playwright driver before AWS credentials are
injected. The credential-bearing step launches the hosted runner's preinstalled
Chrome, so it downloads neither code nor a browser while the credentials are
present. Playwright distinguishes browser launch, page navigation, and app
frame failures directly.

Assumptions (checked, with clear failures):

1. `aws` and `ssh` are on PATH.
2. AWS credentials with the policy in
   [`tests/smoke/iam_policy_smoke.json`](../../tests/smoke/iam_policy_smoke.json) are exported
   as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.

The smoke owns its deploy config: it deploys an agent named `trustyclaw-smoke`
into the region pinned in `tests/smoke/smoke_aws.py` (`SMOKE_REGION`, which matches
the IAM policy), and generates an ephemeral operator SSH key it discards at
teardown. So you write no config and create no key.

Cost: one `t3.small`, one 16 GiB root gp3 volume, one 16 GiB encrypted admin
volume, and one 8 GiB encrypted agent volume for a few minutes. Teardown
removes the instance root volume, both
data volumes, and the smoke security group even if deploy fails before writing a
result file. The harness launcher probes have no Bedrock credential and the
proxy rejects them before an upstream model request, so they add no inference
charge.

```
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
python3 tests/smoke/smoke_aws.py
```

## One-time AWS setup for fresh smoke

Run this once, ideally in a throwaway or low-blast-radius AWS account, to create
the least-privilege IAM user. The policy grants only the EC2/SSM actions
`deploy` uses, requires TrustyClaw tags on created resources, and limits EC2
updates and cleanup to resources tagged `trustyclaw-host=true`, i.e. only what
this tool created. It has no region condition; the deploy config selects the AWS
region.
Review [`tests/smoke/iam_policy_smoke.json`](../../tests/smoke/iam_policy_smoke.json), then:

```bash
aws iam create-policy \
  --policy-name trustyclaw-host-smoke \
  --policy-document file://tests/smoke/iam_policy_smoke.json

aws iam create-user --user-name trustyclaw-host-smoke
aws iam attach-user-policy \
  --user-name trustyclaw-host-smoke \
  --policy-arn arn:aws:iam::<account-id>:policy/trustyclaw-host-smoke

aws iam create-access-key --user-name trustyclaw-host-smoke
```

Export the returned access key id and secret as `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`, then run the smoke. The smoke uses `SMOKE_REGION`, which
is `us-east-1` by default.

The account also needs a default VPC with a public default subnet in the chosen
region (the AWS default) — `deploy` errors clearly if it cannot find one.

## Fresh smoke workflow

`.github/workflows/trustyclaw-smoke.yml` runs the same fresh smoke from GitHub
Actions. Add these repository secrets:

| Secret | Value |
| --- | --- |
| `TRUSTYCLAW_SMOKE_AWS_ACCESS_KEY_ID` | Access key id for the smoke IAM user. |
| `TRUSTYCLAW_SMOKE_AWS_SECRET_ACCESS_KEY` | Secret access key for the smoke IAM user. |

A repository admin can run it manually with `workflow_dispatch` by selecting a
branch or tag in the GitHub Run workflow UI. Anyone can request a pull request
smoke by commenting exactly this on the pull request:

```text
/smoke
```

The workflow first runs an `authorize` job that checks out trusted workflow
actions from `main`. Manual dispatches verify the triggering actor is a
repository admin. Comment-triggered runs do not require admin permission, but
they still reject fork PR heads before exposing AWS secrets. The authorize job
also rejects the request immediately if another `trustyclaw-smoke` run is
already queued or in progress, with a failing `trustyclaw-smoke` status telling
the requester to wait for the previous smoke to complete. The shared live AWS
rate limit rejects the eleventh authorized run started within a rolling
one-hour window. The smoke job also has a same-group concurrency guard as a
race-condition backstop so two fresh smoke jobs cannot run at the same time.
Comment-triggered runs execute from the default-branch workflow, so the workflow
also writes a `trustyclaw-smoke` commit status on the resolved pull request head
SHA. That status is what makes the smoke result visible in the pull request
checks area.
