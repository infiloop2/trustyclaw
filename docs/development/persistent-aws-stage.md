# Persistent AWS Stage

Stage is the long-lived environment for login-dependent checks. The workflow
upgrades or recreates one fixed host, `trustyclaw-stage` in `us-east-1`, using a
stable admin password and a persistent operator SSH endpoint. The admin and
agent data volumes are preserved, so Codex and Claude OAuth sessions survive
across upgrades.

The stage workflow uses the lifecycle commands in this order:

1. `python3 -m host.cli.upgrade --config stage_upgrade_config.json --result-file trustyclaw-stage.json`
2. If upgrade fails only because the preserved state is already at the repo
   `VERSION`, the workflow starts the tagged EC2 instance with
   `python3 -m host.cli.start --config stage_upgrade_config.json --result-file trustyclaw-stage.json`,
   without changing password or operator access.
3. If the instance is missing but preserved volumes exist,
   `python3 -m host.cli.recover --config stage_upgrade_config.json --allow-upgrade --result-file trustyclaw-stage.json`
4. If this is the first-ever stage run and no preserved volumes exist,
   `python3 -m host.cli.deploy --config stage_deploy_config.json --admin-password-env TRUSTYCLAW_STAGE_ADMIN_PASSWORD --result-file trustyclaw-stage.json`

Normal release runs should take the upgrade path, which preserves the existing
admin password and operator endpoints from admin state. Same-version reruns
start the existing instance and test it as-is. First deploy installs the
configured stage SSH endpoint. Because upgrade, recovery, and power configs
intentionally omit operator endpoints and upgrade/start result files omit the
preserved admin password, the stage workflow keeps separate upgrade/recovery
and deploy config files. The stage test receives
`TRUSTYCLAW_STAGE_ADMIN_PASSWORD` through `--admin-password-env`, and the
workflow passes the generated stage SSH key path through `--ssh-key-env
TRUSTYCLAW_STAGE_SSH_KEY`.

The stage test starts by resetting the active network policy to the enforcement
baseline and killing or canceling any leftover active tasks. It then requires
both runtimes to already be active. If Codex or Claude needs login, the test
fails with a manual-login message instead of starting an OAuth flow.

It covers the runtime checks omitted from fresh smoke: Codex account guards and
real web-search task traffic, Claude bearer-token guards and real task traffic,
mixed Codex/Claude concurrency, steering, kill and thread survival, persisted
thread recall, runtime deactivation behavior, host reboot recovery, and the
network event prune race.

## One-time AWS and GitHub setup for stage

Create a separate IAM user for stage, with the stage-scoped policy:

```bash
aws iam create-policy \
  --policy-name trustyclaw-host-stage \
  --policy-document file://tests/stage/iam_policy_stage.json

aws iam create-user --user-name trustyclaw-host-stage
aws iam attach-user-policy \
  --user-name trustyclaw-host-stage \
  --policy-arn arn:aws:iam::<account-id>:policy/trustyclaw-host-stage

aws iam create-access-key --user-name trustyclaw-host-stage
```

Generate the persistent operator SSH key locally:

```bash
install -m 0700 -d ~/.ssh/trustyclaw
ssh-keygen -t ed25519 -f ~/.ssh/trustyclaw/stage_operator -C trustyclaw-stage -N ''
```

Generate a stable admin password and keep it in your password manager:

```bash
openssl rand -base64 32
```

Add these repository secrets:

| Secret | Value |
| --- | --- |
| `TRUSTYCLAW_STAGE_AWS_ACCESS_KEY_ID` | Access key id for the stage IAM user. |
| `TRUSTYCLAW_STAGE_AWS_SECRET_ACCESS_KEY` | Secret access key for the stage IAM user. |
| `TRUSTYCLAW_STAGE_SSH_PRIVATE_KEY` | Full contents of `~/.ssh/trustyclaw/stage_operator`. |
| `TRUSTYCLAW_STAGE_ADMIN_PASSWORD` | Stable password generated above. |

The stage account also needs a default VPC with a public default subnet in
`us-east-1`.

## Running stage

`.github/workflows/trustyclaw-stage.yml` can only be started manually with
`workflow_dispatch` from the `main` branch. Do not run stage from pull request
comments, pull request branches, or temporary feature branches: stage is a
persistent environment, so upgrades must use the stable mainline version and
mainline migration path.

The workflow first runs an `authorize` job that checks out trusted workflow
actions from `main`, verifies the triggering actor is a repository admin,
rejects non-main dispatches before exposing stage secrets, and applies the
shared live AWS run rate limit. The stage job only runs after that job
succeeds. A concurrency group keeps two stage operations from racing, and the
rate limit rejects the eleventh authorized run started within a rolling
one-hour window.
After the stage test step finishes, the workflow stops the EC2 instance even if
the test failed. The preserved admin and agent EBS volumes remain for the next
stage operation.

The first run, or any run after provider sessions expire, can fail because
Codex or Claude is not active. In that case, open a local SSH tunnel to the
stage admin UI, log in with `TRUSTYCLAW_STAGE_ADMIN_PASSWORD`, complete the
provider OAuth flows, then rerun the workflow. If the stage workflow has already
stopped the instance, run `.github/workflows/trustyclaw-stage-start.yml` from
`main` first so the tunnel target exists. That workflow can only be dispatched
by a repository admin from `main`; it starts the existing tagged
`trustyclaw-stage` EC2 instance and prints the SSH tunnel command.

To stop stage manually after inspection, run
`.github/workflows/trustyclaw-stage-stop.yml` from `main`. It is also
admin-only, shares the same stage concurrency group, and stops the existing
tagged `trustyclaw-stage` EC2 instance without deleting the preserved EBS
volumes.

Use this helper to discover the current public DNS and forward the admin UI/API
and proxy ports:

```bash
public_dns="$(
  aws ec2 describe-instances \
    --region us-east-1 \
    --filters \
      'Name=tag:trustyclaw-host-agent-name,Values=trustyclaw-stage' \
      'Name=tag:trustyclaw-host,Values=true' \
      'Name=instance-state-name,Values=running' \
    --query 'Reservations[0].Instances[0].PublicDnsName' \
    --output text
)"

ssh -i ~/.ssh/trustyclaw/stage_operator \
  -o ExitOnForwardFailure=yes \
  -N \
  -L 7443:127.0.0.1:7443 \
  -L 7445:127.0.0.1:7445 \
  "trustyclaw-operator@${public_dns}"
```

Then open `http://127.0.0.1:7443/`.
