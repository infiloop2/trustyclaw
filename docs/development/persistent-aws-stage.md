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

The stage test takes a `--suite` argument selecting which checks run: `claude`,
`codex`, or `github` run that provider's checks plus the shared preamble, and
`all` (the default) additionally runs the cross-runtime checks. Whichever suite
is selected, the run first performs a configuration preflight that verifies the
one-time operator setup the suite needs is present, failing fast and listing
every missing item at once before any long-running check. The preflight is
scoped to the selection: `codex`/`claude` require that provider's runtime to be
active; `github` requires the GitHub credential and a sandbox write repository
(and needs no provider login); `all` requires all three. If something is
missing, the test prints a configuration snapshot and a manual-setup message
instead of starting an OAuth flow.

GitHub is the exception to "manual setup": when the `STAGE_GITHUB_*` stage
secrets are set (a GitHub App id, installation id, private key, and
`owner/repo`), the run installs that App credential and makes the `owner/repo`
the sole GitHub write repository, fully replacing any repo already on the host,
before the preflight. So a `github` or `all` run needs no manual GitHub
configuration, and the write end-to-end always targets the sandbox repo from the
secrets. The provider OAuth logins still cannot be automated and remain one-time
admin-UI setup.

After the preflight, the test resets the active network policy to the
enforcement baseline and kills or cancels any leftover active tasks. The shared
preamble (health, admin UI, admin auth, agent file explorer) runs for every
suite.

The `all` suite covers the runtime checks omitted from fresh smoke: Codex
account guards and real web-search task traffic, Claude bearer-token guards and
real task traffic, mixed Codex/Claude concurrency, steering, kill and thread
survival, persisted thread recall, runtime deactivation behavior, host reboot
recovery, the network event prune race, and the GitHub write end-to-end. A
single-provider suite runs only that provider's guard, task, steering, and kill
checks; `github` runs only the GitHub write end-to-end.

## One-time AWS and GitHub setup for stage

Besides the AWS/IAM setup below, the stage host needs Codex and Claude OAuth
logins completed once through its admin UI (these cannot be automated). GitHub
is configured from the `STAGE_GITHUB_*` repository secrets instead (see the
secrets table below), so it needs no admin-UI setup; if those secrets are absent
the run falls back to a write-capable GitHub credential and sandbox write
repository configured through the admin UI (see the GitHub write end-to-end
check below).

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
| `TRUSTYCLAW_STAGE_GITHUB_WRITE_REPO` | Sandbox write repo as `owner/repo` (branches are pushed and deleted there, so use a dedicated sandbox, never a real repo). |
| `TRUSTYCLAW_STAGE_GITHUB_APP_ID` | Numeric GitHub App id whose installation can push to the sandbox repo. |
| `TRUSTYCLAW_STAGE_GITHUB_APP_INSTALLATION_ID` | Numeric installation id of that App on the sandbox repo. |
| `TRUSTYCLAW_STAGE_GITHUB_APP_PRIVATE_KEY` | The GitHub App PEM private key. |

The four `TRUSTYCLAW_STAGE_GITHUB_*` secrets are optional: set all four to have a
`github` or `all` run auto-configure GitHub, or none to configure a credential
and write repo manually through the admin UI. Setting only some fails the run.

The stage account also needs a default VPC with a public default subnet in
`us-east-1`.

## Running stage

`.github/workflows/trustyclaw-stage.yml` can only be started manually with
`workflow_dispatch` from the `main` branch. Do not run stage from pull request
comments, pull request branches, or temporary feature branches: stage is a
persistent environment, so upgrades must use the stable mainline version and
mainline migration path.

The `workflow_dispatch` form takes a **suite** input (`all`, `claude`, `codex`,
or `github`; default `all`) that maps to the test's `--suite` argument. Use a
single-provider suite to exercise or debug just that integration, for example
`github` once the GitHub credential and sandbox write repo are configured, or
`codex`/`claude` while GitHub is still being set up. Each suite still runs its
scoped configuration preflight first.

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

Every run that includes GitHub (the `github` or `all` suite) exercises the
GitHub write paths end to end. This depends on GitHub being configured: from the
`TRUSTYCLAW_STAGE_GITHUB_*` secrets (auto-installed before the preflight) or,
absent those, from a manually configured credential and write repo. The
preflight fails with instructions until one of those is in place. Required, both through
the admin UI (Internet Access and Tools): a **write-capable GitHub
credential** stored, and **at least one sandbox write repository** in the
policy that the credential can push to (real branches are pushed and deleted
there, so use a dedicated sandbox repo — never a real one). The configured
write repositories survive the run's baseline policy reset: the harness
captures them before the reset and merges them into every policy it
publishes. The check clones, pushes a uniquely named branch, reads it back
and deletes it through authenticated `gh api`, and verifies a push to an
unlisted repo is denied by the proxy. The fresh smoke covers every
unauthenticated GitHub guard branch automatically; stage covers what only a
real credential can.

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
