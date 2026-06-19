# TrustyClaw Development Guide

This guide covers how the code is laid out, how to run the tests, and how to
run the live smoke test (including the AWS setup it needs). For what the
system does and how it is structured, read [`Architecture.md`](Architecture.md)
first.

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
    admin_ui.html            # single-page admin UI served at GET /
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
tests/                       # unit tests and manual smoke tests
  smoke/                     # manual smoke tests (NOT run in CI)
.github/                     # CI workflow + no-network sandbox
```

Important source files and the context that runs them:

| Module | Runs as | Purpose |
| --- | --- | --- |
| `host/deploy.py` | operator's machine | Provisions EC2 and bootstraps the host. Never runs on the host. |
| `host/config.py` | operator machine and host services | Input config and network policy validation. |
| `host/bootstrap/user_data.sh` | root via EC2 user data | Minimal first-boot script: creates the operator account, installs the one-use deploy key, and opens the SSH bootstrap path. |
| `host/bootstrap/bootstrap.sh` | root via SSH deploy | Full host bootstrap: mounts volumes, installs packages and CLIs, creates users, writes state files, installs helpers, configures nftables/systemd, and applies the initial network policy. |
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
| `host/runtime/admin_ui.html` | served by admin API | Single-page admin UI; a thin layer over the API. |
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

## Tests, at two levels

| Level | Command | Needs network? | Needs AWS? | Needs provider login? |
| --- | --- | --- | --- | --- |
| Unit tests | `python3 -m unittest discover -s tests` | No | No | No |
| Smoke test | `python3 tests/smoke/smoke_aws.py ...` | Yes | Yes | Yes (Codex OAuth for the live task steps) |

Run the unit tests on every change; run the smoke test by hand when touching
an agent runtime adapter, the orchestrator, the proxy, or the deploy/bootstrap
path.

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

Consequently **CI can never reach the internet or any account**, which is why
the smoke tests below are not — and must not be — wired into it. They are
manual, run from a trusted developer machine.

## The smoke test (`tests/smoke/smoke_aws.py`)

Deploys a real host, validates everything the unit tests cannot — subnet/SG/
IMDSv2/SSH provisioning, bootstrap on real Ubuntu, the admin API over the SSH
tunnel, live network enforcement (the agent reaches an allowed domain only
through the proxy, a denied path is blocked, and a direct connection is dropped
by nftables), and the live web search guard (a `web_search` payload to
chatgpt.com is denied while the cached variant is allowed). It also checks the
managed provider policy schema, including rejecting user-authored OpenAI and
Claude managed domains. It then **always tears the host down**.

On the same deployed host it also exercises the admin API contract edge cases
(auth rejection, the task lifecycle and its 4xx responses, idempotency
replay/conflicts, policy validation, event pagination), concurrency (parallel
task creation, a same-key idempotency storm, concurrent policy replaces,
parallel proxy traffic with consistent event sequencing), and proxy protocol
edge cases (CONNECT port pinning, unknown hosts, Host header mismatch,
percent-encoded paths against path guards, wildcard domain rules, plain-HTTP
proxying).

The interactive Codex and Claude Code steps cover the live runtime behavior the
host depends on: a real Codex web search task under the guard, a real Claude
Code task through the Anthropic guard, six mixed Codex/Claude tasks running at
the total cap of 6 and per-runtime cap of 3 (peak concurrency proven from
`task.started`/`task.completed` event timestamps), steering a running task
mid-turn, killing a running task and resuming its thread, thread context
surviving warm server reuse/session resume and pool eviction, and a host reboot
at the end (services restart enabled; task history, provider logins, and thread
maps survive).

Run it directly:

```bash
python3 tests/smoke/smoke_aws.py
```

It enables both managed provider bundles, checks asymmetric provider activation,
checks Anthropic API fail-closed behavior before the account token hash is
known, runs the Claude OAuth browser-code flow, verifies missing/wrong
bearer-token denials after login, runs a real Claude task through the proxy,
verifies that disabling providers fails running Codex and Claude tasks, then
runs mixed parallelism, steering, kill, thread recall, reboot, and event-prune
checks against both runtime paths.

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
gp3 volumes for a few minutes (~1 US cent). Teardown removes the instance root
volume and, once deploy has written their ids, both data volumes. The Codex
steps are interactive: the smoke prints a Codex device code to approve in a
browser, so a logged-in run attendant is required.

```
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
python3 tests/smoke/smoke_aws.py
```

### One-time AWS setup for the e2e smoke

Run this once, ideally in a throwaway or low-blast-radius AWS account, to create
the least-privilege IAM user. The policy grants only the EC2/SSM actions
`deploy` uses, restricts everything to one region, and — importantly — limits
the destructive actions (terminate instance, delete/modify security group) to
resources tagged `trustyclaw-host=true`, i.e. only what this tool created.
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
`AWS_SECRET_ACCESS_KEY`, then run the smoke. The policy and `SMOKE_REGION` both
use `us-east-1`; to use another region, change both together. To tighten the
policy further: the destructive statement is already tag-scoped; you can also
restrict `RunInstances` by instance type, or scope `ssm:GetParameter` to the
exact Ubuntu 22.04 parameter ARN.

The account also needs a default VPC with a public default subnet in the chosen
region (the AWS default) — `deploy` errors clearly if it cannot find one.
