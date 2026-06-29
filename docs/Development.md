# TrustyClaw Development Guide

This guide covers how the code is laid out, how to run the tests, and how to
set up the live AWS smoke and stage checks. For what the system does and how it
is structured, read [`Architecture.md`](Architecture.md) first.

## Layout

The runtime is pure Python 3 standard library — no third-party dependencies.

```
host/
  deploy.py                  # deploy entrypoint; runs on the operator's machine
  config.py                  # input config + network policy validation
  constants.py               # shared admin/proxy port constants
  bootstrap/
    user_data.sh             # first-boot SSH/bootstrap access setup
    bootstrap.sh             # full host bootstrap run over SSH as root
    helpers/                 # root-owned sudo helper scripts installed on host
  runtime/
    admin_api.py             # localhost admin API
    orchestrator.py          # task worker pool + runtime session cache
    admin_ui.html            # single-page admin UI shell served at GET /
    admin_ui.css             # admin UI styling
    admin_ui.js              # admin UI behavior
    codex_app_server.py      # stdio JSON-RPC client for the Codex app-server
    claude_code.py           # Claude Code CLI adapter + OAuth login process
    network_proxy.py         # policy-enforcing HTTP(S)/WS(S) proxy
    network_policy.py        # policy files, domain matching, request decisions
    proxy_state_client.py    # admin-side client for proxy-state helpers
    read_network_state.py    # proxy-owned state read helper entrypoint
    state.py                 # admin-owned JSON/JSONL state helpers
    task_status.py           # task lifecycle transition helpers
    update_network_policy.py # proxy-owned policy writer entrypoint
    update_provider_account.py # proxy-owned provider-pin writer entrypoint
tests/                       # unit tests, local UI smoke, and live AWS tests
  smoke/                     # manual smoke tests (NOT run in CI)
  stage/                     # persistent staging tests (NOT run in CI)
  smoke-ui/                  # local admin UI mock backend + browser smoke
.github/                     # no-network CI plus admin-triggered live AWS workflows
```

Important source files and the context that runs them:

| Module | Runs as | Purpose |
| --- | --- | --- |
| `host/deploy.py` | operator's machine | Provisions EC2 and bootstraps the host. Never runs on the host. |
| `host/config.py` | operator machine and host services | Input config and network policy validation. |
| `host/bootstrap/user_data.sh` | root via EC2 user data | Minimal first-boot script: creates the operator account, installs the one-use deploy key, and opens the SSH bootstrap path. |
| `host/bootstrap/bootstrap.sh` | root via SSH deploy | Full host bootstrap: mounts volumes, installs packages and CLIs, creates users, writes state files, installs helpers, configures nftables/systemd, and creates an empty runtime network policy only when no preserved policy exists. |
| `host/bootstrap/helpers/run-codex-app-server.sh` | root via sudo helper | Root-owned launcher that demotes to `trustyclaw-agent` and starts Codex with proxy/CA environment. |
| `host/bootstrap/helpers/run-claude-code.sh` | root via sudo helper | Root-owned launcher that demotes to `trustyclaw-agent` and starts Claude Code with proxy/CA environment. |
| `host/bootstrap/helpers/read-codex-account-id.sh` | root via sudo helper | Narrow helper that reads Codex auth as `trustyclaw-agent` and prints only the inferred OpenAI account id. |
| `host/bootstrap/helpers/read-claude-account.sh` | root via sudo helper | Narrow helper that reads Claude auth as `trustyclaw-agent` and prints only account metadata plus the OAuth bearer hash. |
| `host/bootstrap/helpers/update-network-policy.sh` | root via sudo helper | Demotes to `trustyclaw-proxy` and runs the policy writer. |
| `host/bootstrap/helpers/read-network-state.sh` | root via sudo helper | Demotes to `trustyclaw-proxy` and runs the proxy-state reader. |
| `host/bootstrap/helpers/update-provider-account.sh` | root via sudo helper | Demotes to `trustyclaw-proxy` and updates proxy-owned provider account pins. |
| `host/bootstrap/helpers/reboot-host.sh` | root via sudo helper | Root-owned reboot helper used by the admin API. |
| `host/runtime/admin_api.py` | `trustyclaw-admin` | Localhost admin API on `127.0.0.1:7443`. |
| `host/runtime/orchestrator.py` | `trustyclaw-admin` | Task worker pool, runtime process cache, and runtime status poller. |
| `host/runtime/admin_ui.html` | served by admin API | Single-page admin UI shell; a thin layer over the API. |
| `host/runtime/admin_ui.css` | served by admin API | Admin UI styling. |
| `host/runtime/admin_ui.js` | served by admin API | Admin UI behavior and API calls. |
| `host/runtime/codex_app_server.py` | `trustyclaw-admin` | Stdio JSON-RPC client for the Codex app-server. |
| `host/runtime/claude_code.py` | `trustyclaw-admin` | Claude Code CLI adapter and OAuth login process management. |
| `host/runtime/network_proxy.py` | `trustyclaw-proxy` | Policy-enforcing HTTP(S)/WS(S) proxy on `127.0.0.1:7445`. |
| `host/runtime/network_policy.py` | `trustyclaw-admin` and `trustyclaw-proxy` | Policy files, domain matching, request decisions, provider guards. |
| `host/runtime/proxy_state_client.py` | `trustyclaw-admin` | Admin-side client for the proxy-state helpers. |
| `host/runtime/read_network_state.py` | `trustyclaw-proxy` via root sudo helper | Narrow read helper for proxy-owned policy and network events. |
| `host/runtime/state.py` | `trustyclaw-admin`; selected proxy helpers | JSON/JSONL state file helpers. |
| `host/runtime/task_status.py` | `trustyclaw-admin` | Shared task status transition helpers. |
| `host/runtime/update_network_policy.py` | `trustyclaw-proxy` via root sudo helper | The only writer of the network policy files. |
| `host/runtime/update_provider_account.py` | `trustyclaw-proxy` via root sudo helper | Narrow write helper for proxy-owned provider account pins. |

Develop against Python 3.11 (the Ubuntu 22.04 host runtime) to match CI.

## Test Levels

| Level | Command | Needs network? | Needs AWS? | Needs provider login? |
| --- | --- | --- | --- | --- |
| Static type checks | `python3 -m mypy --config-file mypy.ini` and `python3 -m pyright --project pyrightconfig.json` | No | No | No |
| Unit tests | `python3 -m unittest discover -s tests` | No | No | No |
| Admin UI mock smoke | `python3 tests/smoke-ui/admin_ui_smoke.py --port 3100` | No | No | No |
| Fresh AWS smoke | `python3 tests/smoke/smoke_aws.py` | Yes | Yes | No |
| Persistent AWS stage | `python3 tests/stage/stage_aws.py ...` | Yes | Yes | Yes, completed once on the stage host |

Run the static type checks and unit tests on every change; the admin UI mock
smoke runs in CI and is also useful locally while editing the files under
`host/runtime/admin_ui.*`. Run the live AWS smoke or stage test by hand when
touching an agent runtime adapter, the orchestrator, the proxy, or the
deploy/bootstrap path.

### Static type checks (run on every change, and in CI)

```bash
python3 -m mypy --config-file mypy.ini
python3 -m pyright --project pyrightconfig.json
```

The type-check configs currently target `host/`, the production deploy and host
runtime package. The live AWS harnesses under `tests/smoke/` and `tests/stage/`
are intentionally outside the type-check gate for now; they remain covered by
syntax compilation and their live workflows.

### Unit tests (run on every change, and in CI)

```
python3 -m unittest discover -s tests
```

They need `openssl` (proxy certificate tests) and `bash` (rendered-script
checks) on PATH, but **no network and no credentials**: the Codex protocol is
exercised against a scripted fake app-server, the Claude Code adapter is
exercised against scripted CLI processes, the AWS deploy against fake
`aws`/`ssh`/`scp` CLIs, and the proxy against a local TLS server. This is the
suite CI runs.

### CI: tests inside a no-network sandbox

`.github/workflows/test-all-host.yml` runs on every pull request and push to
`main`. Because a pull request can change code that the workflow then executes,
test execution is a potential data-exfiltration vector. So CI builds a minimal
Ubuntu image (`.github/ci/sandbox.Dockerfile`) and runs the compile and test
steps inside it with `--network none`, all capabilities dropped,
`no-new-privileges`, a read-only source mount, and a non-root user
(`.github/ci/run-in-sandbox.sh`). The workflow token is read-only and the
checkout does not persist credentials.

Consequently **CI can never reach the internet or any account**. The admin UI
mock smoke is safe to run there because it uses only localhost and in-memory
mock data. The live AWS smoke and stage workflows run separately and only after
a repository admin starts them.

## Admin UI mock smoke (`tests/smoke-ui/`)

For admin UI development, run the single-page UI against a deterministic local
mock backend instead of a deployed host:

```bash
python3 tests/smoke-ui/run_admin_ui_mock.py --port 3100
```

Open `http://127.0.0.1:3100/` and log in with password `dev`. The port is an
argument so multiple developers or agents can choose non-conflicting localhost
ports.

The mock backend serves `host/runtime/admin_ui.html` and implements the `/v1/*`
routes the UI uses with in-memory data. It is for UI wiring and interaction
checks only; it does not validate the real admin API, host state, sudo helpers,
agent runtimes, or network proxy.

To run the automated browser smoke, install the development-only Playwright
dependency once. If no cached Chromium is available, install the browser too:

```bash
python3 -m pip install -r tests/smoke-ui/requirements.txt
python3 -m playwright install chromium
```

Then run:

```bash
python3 tests/smoke-ui/admin_ui_smoke.py --port 3100
```

The smoke starts the mock server, opens Chromium, logs in with `dev`, creates a
task, opens the thread and task event views, edits network policy through the
GitHub preset, and checks the Codex login panel. CI installs Playwright and
Chromium during the Docker image build, then runs this smoke through
`.github/ci/run-in-sandbox.sh` with `--network none`. On development boxes with
a preinstalled Playwright browser cache, the smoke reuses the newest cached
Chromium automatically. To use a specific browser binary, set
`PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chrome`.

## Fresh AWS smoke (`tests/smoke/smoke_aws.py`)

The fresh smoke deploys a real host from scratch, validates the parts unit tests
cannot, then tears the host down. It uses `--allow-upgrade-or-recover` and
`--reset-storage-dangerous-delete`, so any stale `trustyclaw-smoke` EC2 instance
and durable data volumes are replaced before the test starts. It does not
require Codex or Claude OAuth; login-dependent runtime checks live in the
persistent stage test.

The smoke covers subnet/SG/IMDSv2/SSH provisioning, bootstrap on real Ubuntu,
admin API access over the SSH tunnel, auth rejection, task lifecycle edge cases,
idempotency, policy validation, event pagination, concurrent policy replaces,
proxy protocol edge cases, live network enforcement, managed provider policy
validation, and the network event prune race.

Assumptions (checked, with clear failures):

1. `aws` and `ssh` are on PATH.
2. AWS credentials with the policy in
   [`tests/smoke/iam_policy_smoke.json`](../tests/smoke/iam_policy_smoke.json) are exported
   as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.

The smoke owns its deploy config: it deploys an agent named `trustyclaw-smoke`
into the region pinned in `tests/smoke/smoke_aws.py` (`SMOKE_REGION`, which matches
the IAM policy), and generates an ephemeral operator SSH key it discards at
teardown. So you write no config and create no key.

Cost: one `t3.small` + one 16 GiB root gp3 volume + two 8 GiB encrypted data
gp3 volumes for a few minutes. Teardown removes the instance root volume and,
once deploy has written their ids, both data volumes.

```
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
python3 tests/smoke/smoke_aws.py
```

### One-time AWS setup for fresh smoke

Run this once, ideally in a throwaway or low-blast-radius AWS account, to create
the least-privilege IAM user. The policy grants only the EC2/SSM actions
`deploy` uses, requires TrustyClaw tags on created resources, and limits EC2
updates and cleanup to resources tagged `trustyclaw-host=true`, i.e. only what
this tool created. It has no region condition; the deploy config selects the AWS
region.
Review [`tests/smoke/iam_policy_smoke.json`](../tests/smoke/iam_policy_smoke.json), then:

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

### Admin-triggered smoke workflow

`.github/workflows/trustyclaw-smoke.yml` runs the same fresh smoke from GitHub
Actions. Add these repository secrets:

| Secret | Value |
| --- | --- |
| `TRUSTYCLAW_SMOKE_AWS_ACCESS_KEY_ID` | Access key id for the smoke IAM user. |
| `TRUSTYCLAW_SMOKE_AWS_SECRET_ACCESS_KEY` | Secret access key for the smoke IAM user. |

A repository admin can run it manually with `workflow_dispatch` by selecting a
branch or tag in the GitHub Run workflow UI, or comment exactly this on a pull
request:

```text
/trustyclaw-smoke
```

The workflow first runs an `authorize` job that checks out trusted workflow
actions from `main`, verifies the triggering actor is a repository admin,
rejects fork PR heads before exposing AWS secrets, and applies the shared live
AWS run rate limit. The smoke job only runs after that job succeeds. A
concurrency group keeps only one smoke active at a time, and the rate limit
rejects the eleventh authorized run started within a rolling one-hour window.

## Persistent AWS stage (`tests/stage/stage_aws.py`)

Stage is the long-lived environment for login-dependent checks. The workflow
upgrades or recovers one fixed host, `trustyclaw-stage` in `us-east-1`, using a
stable admin password and a persistent operator SSH key. The admin and agent
data volumes are preserved, so Codex and Claude OAuth sessions survive across
upgrades.

The stage test starts by resetting the active network policy to the enforcement
baseline and killing or canceling any leftover active tasks. It then requires
both runtimes to already be active. If Codex or Claude needs login, the test
fails with a manual-login message instead of starting an OAuth flow.

It covers the runtime checks omitted from fresh smoke: Codex account guards and
real web-search task traffic, Claude bearer-token guards and real task traffic,
mixed Codex/Claude concurrency, steering, kill and thread survival, persisted
thread recall, runtime deactivation behavior, host reboot recovery, and the
network event prune race.

### One-time AWS and GitHub setup for stage

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

### Running stage

`.github/workflows/trustyclaw-stage.yml` can be started manually with
`workflow_dispatch` by selecting a branch or tag in the GitHub Run workflow UI,
or by a repository admin commenting exactly this on a pull request:

```text
/trustyclaw-stage
```

The workflow first runs an `authorize` job that checks out trusted workflow
actions from `main`, verifies the triggering actor is a repository admin,
rejects fork PR heads before exposing stage secrets, and applies the shared
live AWS run rate limit. The stage job only runs after that job succeeds. A
concurrency group keeps two stage upgrades from racing, and the rate limit
rejects the eleventh authorized run started within a rolling one-hour window.
After the stage test step finishes, the workflow stops the EC2 instance even if
the test failed. The preserved admin and agent EBS volumes remain for the next
upgrade/recover run.

The first run, or any run after provider sessions expire, can fail because
Codex or Claude is not active. In that case, open a local SSH tunnel to the
stage admin UI, log in with `TRUSTYCLAW_STAGE_ADMIN_PASSWORD`, complete the
provider OAuth flows, then rerun the workflow. If the workflow has already
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
