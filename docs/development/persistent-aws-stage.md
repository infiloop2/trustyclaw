# Persistent AWS Stage

Stage is the long-lived environment for login-dependent checks. The workflow
upgrades or recreates one fixed host, `trustyclaw-stage` in `us-east-1`, using a
stable admin password and a persistent operator SSH endpoint. The admin and
agent data volumes are preserved, so Codex and Claude OAuth sessions and the
validated shared AWS Bedrock credential survive across upgrades.

The stage workflow uses the lifecycle commands in this order:

1. `python3 -m host.cli.upgrade --agent-name trustyclaw-stage > trustyclaw-stage.json`
2. If upgrade fails only because the preserved state is already at the repo
   `VERSION`, the workflow starts the tagged EC2 instance with
   `python3 -m host.cli.start --agent-name trustyclaw-stage > trustyclaw-stage.json`,
   without changing password or operator access.
3. If the instance is missing but preserved volumes exist,
   `python3 -m host.cli.recover --agent-name trustyclaw-stage --allow-upgrade > trustyclaw-stage.json`
4. If this is the first-ever stage run and no preserved volumes exist,
   `python3 -m host.cli.deploy --agent-name trustyclaw-stage --operator-ssh-public-key "$TRUSTYCLAW_STAGE_SSH_PUBLIC_KEY" --admin-password-sha256 "$(printf %s "$TRUSTYCLAW_STAGE_ADMIN_PASSWORD" | sha256sum | cut -d' ' -f1)" > trustyclaw-stage.json`

Normal release runs should take the upgrade path, which preserves the existing
admin password and operator endpoints from admin state. Same-version reruns
start the existing instance and test it as-is. First deploy installs the
configured stage SSH endpoint. Upgrade, recovery, and power commands
intentionally take no operator endpoints and the CLI only ever sees the
password hash. The stage test receives `TRUSTYCLAW_STAGE_ADMIN_PASSWORD`
through its own `--admin-password-env` flag for admin API auth, and the
workflow passes the generated stage SSH key path through `--ssh-key-env
TRUSTYCLAW_STAGE_SSH_KEY`.

The stage test takes a `--suite` argument selecting which checks run: `claude`,
`codex`, `pi`, `hermes`, or `github` run that integration's checks plus the shared preamble, every
bundled tool id runs that one tool's live check, and `all` (the default) runs
the complete integration matrix. Every run performs its credential preflight
before any integration test. A focused suite fails when its selected
integration is unavailable. `all` records each unavailable integration as
skipped, then runs every available integration independently, so a missing
secret, unconfigured OAuth account, expired provider session, or failed live
integration cannot hide the results for the rest.

The preflight checks live Codex, Claude Code, Pi, and Hermes runtime status, GitHub's
credential validation plus its sandbox write repository, and every bundled
tool's enablement, config, and OAuth connection state. A tool credential that
looks configured but is rejected by its first provider request is reclassified
as unavailable and skipped in `all`; the same result fails a focused tool run.
The test never starts an interactive OAuth flow.

Pi and Hermes are two runtime suites backed by one AWS Bedrock provider row and
one long-term IAM access key pair. When the two `STAGE_BEDROCK_AWS_*`
environment values are present, stage submits that pair once. The endpoint
synchronously checks STS identity before returning
`accepted`; a failed candidate is deleted. Stage then enables Bedrock in
`us-east-1`, which activates both harnesses. If the values are absent, the test
uses an already validated credential stored on the persistent host.

GitHub is the exception to "manual setup": when the `STAGE_GITHUB_*` stage
secrets are set (a GitHub App id, installation id, private key, and
`owner/repo`), the run installs that App credential and makes the `owner/repo`
the sole GitHub write repository, fully replacing any repo already on the host,
before the preflight. So a `github` or `all` run needs no manual GitHub
configuration, and the write end-to-end always targets the sandbox repo from the
secrets. Provider OAuth tool config and login are never supplied by stage
secrets; both remain one-time admin-UI setup on the persistent host.

After the preflight, the test resets the active network policy to the
enforcement baseline and kills or cancels any leftover active tasks. The shared
preamble (health, admin UI, admin auth, agent file explorer) runs for every
suite.

The `all` suite covers the runtime checks omitted from fresh smoke: Codex
account guards and real web-search task traffic, Claude bearer-token guards and
real task traffic, Pi and Hermes credential-boundary checks and real Bedrock
Converse traffic, mixed Codex/Claude concurrency, steering, kill and thread
survival, persisted thread recall, shared Bedrock disable/re-enable behavior,
runtime deactivation behavior, host reboot
recovery, the network event prune race, the live bundled-tool checks against
real third-party APIs (see below), and the GitHub write
end-to-end. Cross-runtime checks run when all four runtimes pass. A
single-runtime suite runs only that runtime's guard, task,
steering, and kill checks; `github` runs only the GitHub write end-to-end; and
a bundled tool id runs only that tool's live provider check.

The live concurrency scenario uses three Codex and three Claude tasks. The
provider-neutral orchestrator tests separately prove the same three-task cap
for Pi and Hermes, avoiding six additional paid Bedrock turns merely to repeat
the scheduler invariant.

All agent tasks use the least expensive exposed options: Codex uses
`gpt-5.6-luna` with `high` effort, and Claude Code uses `sonnet` with `high`
effort; Pi and Hermes both use `qwen.qwen3-coder-next` with `high` effort.
This includes concurrency,
steering, recovery, and the MCP catalog check. Every runtime asks its agent to
call `list_bundled_tools` and verify the dynamically discovered catalog:
Codex and Claude Code reach the shim through their own MCP clients, Hermes
through its MCP client wired by the managed config, and Pi through the
root-owned `pi_tools_bridge.js` extension.

## Harness layout and failure diagnostics

`tests/stage/stage_aws.py` is the CLI, suite orchestration, and shared host
lifecycle. `stage_support.py` owns integration selection, result reporting, and
the GitHub Actions summary. `stage_integration_checks.py` owns Codex, Claude
Code, and GitHub checks. `stage_bedrock_checks.py` owns the shared credential,
Pi, and Hermes checks. `stage_tool_checks.py` owns bundled-tool preflight and
the deterministic live MCP scenarios. New tool coverage belongs in the tool
module; adding a bundled tool does not require editing suite registration.

Each integration prints its start, outcome, and elapsed time. Provider checks
print runtime state, guard outcomes, task id/model/effort/status, and allowed or
denied network-event counts. Tool checks print every MCP action with bounded,
credential-redacted arguments, elapsed time, result status and keys, approval
decisions, conditional-coverage counts, and the resulting audit actions. An
unexpected failure also prints its Python traceback; the run ends with the
runtime/tool configuration snapshot and recent network diagnostics. Routine
diagnostics never print stored credentials, successful agent prompts or output,
or complete provider response bodies.

## Tool credentials for stage

Every bundled tool is an individual suite, discovered from `host/tools/`. A
dedicated tool suite requires that tool to be enabled and fully configured; an
OAuth tool must also show `connected` in `/v1/tools`. The `all` suite checks
that state for every tool, skips unavailable tools, and runs the complete live
matrix for the rest.

Enable-only tool config secrets follow one rule: a manifest config key `KEY` is
supplied as the repository secret `TRUSTYCLAW_STAGE_KEY`. The workflow maps the
current enable-only manifest keys into the test environment, and the test writes
present values through the admin API before preflight. Values already stored on
the persistent host remain usable when the matching repository secret is absent.
This covers API-key tools and multi-field integrations such as IBKR without
tool-specific test code.

OAuth tool configuration and login are persistent operator state, not CI
secrets. Add the callback URI `http://127.0.0.1:7443/oauth/callback`, set the app
values in the stage admin UI, enable the tool, and connect a dedicated stage
account once. The stage test never writes OAuth config or enables an OAuth tool.
Tokens persist on the admin volume. The Gmail and Google Calendar checks
exercise full approval round trips by creating and deleting a draft or event;
do not connect a personal account. Other non-media write actions are proposed
and denied, so nothing is published. Instagram Reel publishing remains covered
by fresh-smoke schema and approval tests because a live proposal would leave a
staged binary on the persistent host when denied. Reads use minimum page sizes,
result-derived ids cover dependent actions where providers return usable data,
and Runway authenticates with a missing-task probe so stage spends no generation
credits.

The deterministic checks send MCP `tools/call` requests through the real agent
shim as the `trustyclaw-agent` OS user. They therefore use the same tool host,
credential store, approval system, audit log, and provider adapters as an agent;
they do not bypass TrustyClaw with direct provider HTTP requests. Individual
tool suites use only this deterministic path. Agent-to-MCP wiring is tested
once per available provider by the catalog check instead of spending another
model turn for every tool.

## Live calls and expected cost for one `all` run

Only integrations that pass preflight make live calls. Every call below goes
through the MCP shim unless explicitly described as a local approval decision.
"Conditional" means the preceding bounded response must contain the provider
id needed by the next action.

| Integration | Live calls in one run | Side effect | Expected metered usage |
| --- | --- | --- | --- |
| Brave Search | `search_web` once | None | One Brave Search request. |
| Gmail | `search_messages`, `list_labels`, and `list_drafts`; conditional `read_message` and `read_thread`; denied send, message-change, and label proposals; approved draft create and delete | One temporary draft, deleted in the same check | Three to five reads and two writes against the connected account; denied proposals make no write. |
| Google Calendar | `read_events`; approved event create; local approval-status check; approved event delete | One temporary event, deleted in the same check | One read and two writes against the connected account. |
| Interactive Brokers | `get_positions`, `get_account_summary`, and one-day `get_trades` | None | Three read actions; no orders or market-data subscriptions. |
| Instagram | `get_profile`, one-item `get_recent_media`, and `get_publishing_limit` | None | Three bounded Meta reads; no Reel is staged or published. |
| Instagram Discovery | One-item `search_reels`, `get_trending_reels`, and `search_hashtag`; conditional one-item `get_reels_by_audio` and `get_reel_details` | None | Three fixed ScrapeCreators credits, up to five when both dependent reads run. |
| LinkedIn | `get_profile`; denied `create_post` proposal | None | One profile read; the denied proposal does not publish. |
| LinkedIn Discovery | One-item `search_posts` | None | One Serper search. |
| Polymarket | `list_markets`, `list_events`, `search`, `get_market`, `get_order_book`, and `price_history` | None | Six public read requests; no trading or authenticated spend. |
| Runway | `get_task` for a deliberately missing task id | None | One authenticated lookup and zero generation credits. |
| X | Minimum-10 `search_tweets`, one global trend, one personalized trend set; conditional `read_tweet` and maximum-5 `user_tweets`; denied `post_tweet` proposal | None | Search is billed for up to 10 returned posts, plus up to one post read and five user posts; trend endpoint charges follow the connected X plan. |

Codex and Claude Code each make one short `list_bundled_tools` agent call when
their provider is available. That catalog call is local and has no third-party
tool charge, but it consumes model tokens. The rest of the provider and
cross-runtime stage tasks also consume subscription quota; their token total is
not fixed because steering, recall, and tool output vary. GitHub writes and
deletes a temporary branch in the configured sandbox repository. AWS starts
the existing stage instance for the run and stops it afterward. Dollar totals
therefore depend on the operator's model plans, X plan, API-key plans, and EC2
runtime; the table states the fixed request or credit units the harness controls.

Pi and Hermes each make a short Bedrock task, a session-resume turn, an MCP
catalog turn, a running task that stage kills, and a post-kill recovery turn.
Pi also makes a steering task. While the Hermes kill target is running, stage
proves that steering is rejected; this denial invokes no additional model
turn. Both harnesses use Qwen
3 Coder Next, the least expensive of the three exposed Bedrock models for short
stage prompts. The model turns are paid Bedrock inference calls;
the exact token cost varies with model output and runtime behavior. Credential
setup itself makes only an STS call, never a paid model invocation.

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

Create a separate IAM user for the shared Pi/Hermes Bedrock connection. The
host-stage IAM user above manages EC2 and is not exposed to inference:

```bash
aws iam create-policy \
  --policy-name trustyclaw-bedrock-stage \
  --policy-document file://tests/stage/iam_policy_bedrock.json

aws iam create-user --user-name trustyclaw-stage-bedrock
aws iam attach-user-policy \
  --user-name trustyclaw-stage-bedrock \
  --policy-arn arn:aws:iam::<account-id>:policy/trustyclaw-bedrock-stage
aws iam create-access-key --user-name trustyclaw-stage-bedrock
```

The Bedrock policy permits model invocation only. Invocation
uses `Resource: "*"` because cross-region inference profiles can route to
foundation-model resources in multiple US regions. The agent processes receive
only TrustyClaw's fixed dummy SDK credential; the proxy injects this operator
credential after enforcing the Bedrock route and signature.

Generate the persistent operator SSH key locally:

```bash
install -m 0700 -d ~/.ssh/trustyclaw
ssh-keygen -t ed25519 -f ~/.ssh/trustyclaw/stage_operator -C trustyclaw-stage -N ''
```

Generate a stable admin password and keep it in your password manager:

```bash
openssl rand -base64 32
```

Add these repository secrets and variables:

| Name | Value |
| --- | --- |
| `TRUSTYCLAW_STAGE_AWS_ACCESS_KEY_ID` | Access key id for the stage IAM user. |
| `TRUSTYCLAW_STAGE_AWS_SECRET_ACCESS_KEY` | Secret access key for the stage IAM user. |
| `TRUSTYCLAW_STAGE_SSH_PRIVATE_KEY` | Full contents of `~/.ssh/trustyclaw/stage_operator`. |
| `TRUSTYCLAW_STAGE_ADMIN_PASSWORD` | Stable password generated above. |
| `TRUSTYCLAW_STAGE_BEDROCK_AWS_ACCESS_KEY_ID` | Access key id for one dedicated Bedrock IAM user shared by Pi and Hermes. |
| `TRUSTYCLAW_STAGE_BEDROCK_AWS_SECRET_ACCESS_KEY` | Secret access key paired with the shared Bedrock access key id. |
| `TRUSTYCLAW_STAGE_GITHUB_WRITE_REPO` | Sandbox write repo as `owner/repo` (branches are pushed and deleted there, so use a dedicated sandbox, never a real repo). |
| `TRUSTYCLAW_STAGE_GITHUB_APP_ID` | Numeric GitHub App id whose installation can push to the sandbox repo. |
| `TRUSTYCLAW_STAGE_GITHUB_APP_INSTALLATION_ID` | Numeric installation id of that App on the sandbox repo. |
| `TRUSTYCLAW_STAGE_GITHUB_APP_PRIVATE_KEY` | The GitHub App PEM private key. |
| `TRUSTYCLAW_STAGE_<CONFIG_KEY>` | Enable-only tool config value, where `<CONFIG_KEY>` is exactly a key declared by that bundled tool manifest. OAuth tool config is intentionally absent from repository secrets and is stored once through the stage UI. |

The four `TRUSTYCLAW_STAGE_GITHUB_*` secrets are optional: set all four to have a
`github` or `all` run auto-configure GitHub, or none to configure a credential
and write repo manually through the admin UI. A partial secret set makes GitHub
unavailable: `all` reports and skips it, while a focused `github` run fails.

The two `TRUSTYCLAW_STAGE_BEDROCK_AWS_*` secrets are also optional as a pair.
When set, each `pi`, `hermes`, or `all` run validates and installs them before
preflight. When absent, the run uses the shared credential already stored on
the host. A partial pair makes Pi and Hermes unavailable; `all` skips both,
while either focused harness suite fails.

The stage account also needs a default VPC with a public default subnet in
`us-east-1`.

## Running stage

`.github/workflows/trustyclaw-stage.yml` can only be started manually with
`workflow_dispatch` from the `main` branch. Do not run stage from pull request
comments, pull request branches, or temporary feature branches: stage is a
persistent environment, so upgrades must use the stable mainline version and
mainline migration path.

The `workflow_dispatch` form takes a **suite** input (`all`, each bundled tool
id, `claude`, `codex`, `pi`, `hermes`, or `github`; default `all`) that maps to the test's
`--suite` argument. Use a single-provider suite to exercise or debug just that
integration, for example `github` once the GitHub credential and sandbox write
repo are configured, `brave_search` once its key secret is set, or `codex`/`claude`
while GitHub is still being set up. Use `pi` or `hermes` to isolate one harness
while retaining the same shared Bedrock provider. Each suite still runs its scoped
configuration preflight first. The GitHub Actions summary ends with one row per
integration showing credential availability, pass/fail/skip, and a concise
detail. Available failures make the workflow fail; unavailable integrations do
not make an `all` run fail.

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

On the first run, or after an OAuth provider session expires, `all` reports and skips
that provider. A focused `codex` or `claude` run fails. To restore OAuth coverage,
open a local SSH tunnel to the stage admin UI, log in with
`TRUSTYCLAW_STAGE_ADMIN_PASSWORD`, complete the provider OAuth flow, then rerun
the workflow. If the stage workflow has already stopped the instance, run
`.github/workflows/trustyclaw-stage-start.yml` from `main` first so the tunnel
target exists. That workflow can only be dispatched by a repository admin from
`main`; it starts the existing tagged `trustyclaw-stage` EC2 instance and
prints the SSH tunnel command.

If the shared Bedrock credential is missing or no longer passes STS and Cost
Explorer validation, `all` skips both Pi and Hermes and either focused suite
fails. Restore it by setting both Bedrock repository secrets, or by entering
the shared credential once in the AWS Bedrock row in the admin UI.

Every focused `github` run, and each `all` run where GitHub is available,
exercises the GitHub write paths end to end. This depends on GitHub being
configured from the `TRUSTYCLAW_STAGE_GITHUB_*` secrets (auto-installed before
the preflight) or, absent those, from a manually configured credential and
write repo. The preflight reports exactly what is absent. Manual setup requires
both of these through the admin UI (Internet Access and Tools): a
**write-capable GitHub credential** stored, and **at least one sandbox write
repository** in the policy that the credential can push to (real branches are
pushed and deleted there, so use a dedicated sandbox repo, never a real one).
The configured write repositories survive the run's baseline policy reset: the harness
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
