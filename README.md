# TrustyClaw

TrustyClaw is a controlled AI agent host with strong network activity gating.
It lets you run Codex and Claude Code on infrastructure you own while keeping
the agent behind an explicit, auditable network policy.

The host runs on an AWS EC2 instance and exposes an admin UI/API through one or
more operator access endpoints: SSH tunneling, Cloudflare Access, or both. The
admin process, network proxy, optional Cloudflare Tunnel connector, and agent
runtime run as separate Linux users with separate storage and secrets so the
agent can work autonomously without getting direct network access or broad
access to host state.

## Why Use TrustyClaw

- **Runs in the cloud by default:** keep long-running agents active without
  keeping your laptop open.
- **No permission prompts:** the agent runs autonomously in auto-approve mode
  inside a secure sandbox, while network controls prevent unapproved data leaks
  and unexpected internet actions.
- **Coming soon: controlled tools:** connect to third-party services like Gmail
  through deterministic data paths, with approvals for sensitive actions such
  as sending email or making payments.
- **Coming soon: workflow apps:** install purpose-built apps with richer UX than
  a terminal chat loop.

## Configure

To deploy TrustyClaw, you need:

1. An AWS account where the host will run.
2. The AWS CLI and Python 3.11 installed locally.
3. At least one operator access endpoint: SSH, Cloudflare Access, or both.

Start from the included example config:

```bash
cp example_config.json config.json
```

In `config.json`, set:

| Field | What To Put |
| --- | --- |
| `agent_name` | Stable host name. Lifecycle commands use it to find the same host and data volumes. |
| `aws_region` | AWS region to deploy into. |
| `aws_access_key_id_env` | Environment variable name containing the AWS access key id. |
| `aws_secret_access_key_env` | Environment variable name containing the AWS secret access key. |
| `operator_connections` | One or more access endpoints. Use `ssh`, `cloudflare_access`, or both. |
| `operator_connections[].mode` | `ssh` or `cloudflare_access`. |
| `operator_connections[].ssh_public_key` | For SSH, public key content installed for operator access, for example the output of `cat ~/.ssh/id_ed25519.pub`. |
| `operator_connections[].hostname` | For Cloudflare Access, the fixed hostname that routes to the admin UI/API. |
| `operator_connections[].tunnel_token_env` | For Cloudflare Access, environment variable name containing the Cloudflare Tunnel token. |

Deploy reads AWS credentials from the environment variables named in your config:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
```

### AWS Account Setup

For a new AWS account, start by installing and signing in to the AWS CLI using
an administrator identity. Deploy currently uses the default VPC and needs a
default subnet with public IPv4 routing. You can use an administrator access key
while evaluating the project. For regular use, create an IAM user or role with
the policy in `iam_policy.json`. It requires TrustyClaw tags on created
resources, allows EC2 updates and cleanup only on TrustyClaw-tagged resources,
and leaves region selection to your deploy config.
See [`docs/architecture/iam-policy.md`](docs/architecture/iam-policy.md) for
why each policy statement is needed and how its resource scope is constrained.

```bash
aws iam create-policy \
  --policy-name trustyclaw-host-deploy \
  --policy-document file://iam_policy.json

aws iam create-user --user-name trustyclaw-host-deploy
aws iam attach-user-policy \
  --user-name trustyclaw-host-deploy \
  --policy-arn arn:aws:iam::<account-id>:policy/trustyclaw-host-deploy

aws iam create-access-key --user-name trustyclaw-host-deploy

export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
```

### SSH Operator Access

Create an SSH keypair if you do not already have one:

```bash
ssh-keygen -t ed25519 -C trustyclaw-operator -f ~/.ssh/trustyclaw_operator
```

Then add this endpoint to `operator_connections`:

```json
{
  "mode": "ssh",
  "ssh_public_key": "ssh-ed25519 AAAA... trustyclaw-operator"
}
```

When an SSH endpoint is configured, TrustyClaw keeps EC2 security-group ingress
for TCP 22 and installs the key for `trustyclaw-operator`. If SSH is omitted,
the final host closes EC2 SSH ingress after bootstrap.

### Cloudflare Access Operator Access

Cloudflare mode assumes you create the Cloudflare resources yourself and give
TrustyClaw only a tunnel token plus the final hostname.

For a new Cloudflare setup:

1. Add a domain to Cloudflare and make it active.
2. In Cloudflare Zero Trust, create a Cloudflare Tunnel.
3. Add a public hostname for the tunnel, for example
   `trustyclaw.example.com`, with service `http://localhost:7443`.
4. Create a Cloudflare Access self-hosted application for that hostname.
5. Add an Access policy that allows only your user, group, or email domain.
6. Copy the tunnel token from Cloudflare and export it locally:

```bash
export TRUSTYCLAW_CLOUDFLARE_TUNNEL_TOKEN=...
```

Then add this endpoint to `operator_connections`:

```json
{
  "mode": "cloudflare_access",
  "hostname": "trustyclaw.example.com",
  "tunnel_token_env": "TRUSTYCLAW_CLOUDFLARE_TUNNEL_TOKEN"
}
```

TrustyClaw installs `cloudflared` as a systemd service, enables it across
reboots, and verifies during bootstrap that the configured hostname returns a
Cloudflare Access login or deny response. The admin password is still required
after Cloudflare Access succeeds.

See [`docs/api/InputConfig.md`](docs/api/InputConfig.md) for the full input
schema for customization.

## Deploy

Run deploy from the repository root:

```bash
python3 -m host.cli.deploy --config config.json
```

The command writes a sensitive result file, created mode `0600`, and prints its
path. By default, each lifecycle command writes a mode-specific result file:
`<agent_name>-<mode>.json`. Deploy and reconfigure result files include the
admin password, so keep them private.

Host lifecycle commands:

| Command | Behavior | Credential behavior |
| --- | --- | --- |
| `python3 -m host.cli.deploy --config <path>` | Creates a new host. Fails if a TrustyClaw instance or data volume already exists for `agent_name`. | Generates a new admin password, or uses `--admin-password-env <name>`. Installs the configured operator endpoints. |
| `python3 -m host.cli.upgrade --config <path>` | Replaces the EC2 instance/root volume and reuses the preserved admin and agent data volumes. Requires an existing instance and existing data volumes. Bootstrap requires the admin state version to be lower than the repo `VERSION`. | Preserves the existing admin password and operator endpoints from admin state. |
| `python3 -m host.cli.recover --config <path>` | Creates a replacement host from preserved admin and agent data volumes when no TrustyClaw instance exists. Bootstrap requires the admin state version to equal the repo `VERSION`, unless `--allow-upgrade` is supplied. | Preserves the existing admin password and operator endpoints from admin state. |
| `python3 -m host.cli.reconfigure --config <path>` | Replaces an existing EC2 instance/root volume, reuses preserved admin and agent data volumes, and replaces the full operator endpoint list. Requires an existing instance and existing data volumes. Bootstrap requires the admin state version to equal the repo `VERSION`. | Installs a new generated admin password, or uses `--admin-password-env <name>`. |
| `python3 -m host.cli.start --config <path>` | Starts the existing EC2 instance for `agent_name`, waits until it is running, and writes a result JSON. Config must use the upgrade/recover shape and omit `operator_connections`. | Does not change credentials, root disk, data volumes, version, or operator endpoints. |
| `python3 -m host.cli.stop --config <path>` | Stops the existing EC2 instance for `agent_name`, waits until it is stopped, and writes a result JSON. Config must use the upgrade/recover shape and omit `operator_connections`. | Does not change credentials, root disk, data volumes, version, or operator endpoints. |

Shared flags:

| Flag | Commands | Behavior |
| --- | --- | --- |
| `--config <path>` | all | Required. Reads the deploy input config from `<path>`. |
| `--result-file <path>` | all | Writes the local result JSON to `<path>` instead of the default path. This may overwrite an existing file at that path. |
| `--admin-password-env <name>` | `deploy`, `reconfigure` | Reads the admin password from environment variable `<name>` instead of generating one. The host still receives only the password hash. |
| `--allow-upgrade` | `recover` | Allows no-instance recovery to advance preserved admin state from an older version to the repo `VERSION`. |

Lifecycle commands fail before replacing an existing instance when the AWS
resource shape or version tag is incompatible with the command. Bootstrap then
checks the preserved admin disk version as the authoritative source before
writing any upgraded state.

The host uses three EBS volumes:

| Volume | Lifecycle | Contents |
| --- | --- | --- |
| Root | Recreated on redeploy and deleted on instance termination | Ubuntu 22.04, system packages, Node.js, Python, Codex CLI, Claude Code CLI, nftables, OpenSSL, curl, jq, CA certificates, and swap. |
| Admin | Preserved on redeploy and marked not to delete on instance termination | Admin API state, tasks, agent events, network events, network policy, provider account pins, and proxy CA state. |
| Agent | Preserved on redeploy and marked not to delete on instance termination | Agent home directory, provider auth/session files, CLI caches, and workspace data. |

Every AWS resource deploy creates is tagged so it can be found and cleaned up:

| Tag | Value | On |
| --- | --- | --- |
| `trustyclaw-host-agent-name` | `<agent_name>` | instance, volume, security group |
| `trustyclaw-host` | `true` | instance, volume, security group |
| `Name` | `trustyclaw-host-<agent_name>` | instance, volume |
| `trustyclaw-host-volume-role` | `admin` or `agent` | data volumes |
| `trustyclaw-host-version` | repo `VERSION` | instance |

See [`docs/api/DeployResult.md`](docs/api/DeployResult.md) for the lifecycle
result file schema.

## Connect

With SSH operator access, forward the admin UI/API over SSH:

```bash
ssh -i <private-key-path> -L 7443:127.0.0.1:7443 trustyclaw-operator@$(jq -r .public_dns <result-file>.json)
```

After forwarding is active, open `http://127.0.0.1:7443` in your browser, or
call the API directly:

```bash
curl -H "Authorization: Bearer $(jq -r .admin_password <deploy-or-reconfigure-result>.json)" \
  http://127.0.0.1:7443/v1/health
```

With Cloudflare Access operator access, open `https://<configured-hostname>` in
your browser, complete Cloudflare Access authentication, then enter the same
TrustyClaw admin password from your latest deploy or reconfigure result file.

Full admin API documentation is in
[`docs/api/AdminAPI.md`](docs/api/AdminAPI.md).

To give the agent files from your machine, upload them as the operator and then
move them into the agent-owned home directory:

```bash
HOST=trustyclaw-operator@$(jq -r .public_dns <result-file>.json)

ssh -i <private-key-path> "$HOST" 'rm -rf /tmp/trustyclaw-upload'
scp -i <private-key-path> -r ./my-files "$HOST":/tmp/trustyclaw-upload
ssh -i <private-key-path> "$HOST" \
  'sudo rm -rf /mnt/trustyclaw-agent/agent-home/inbox && sudo mv /tmp/trustyclaw-upload /mnt/trustyclaw-agent/agent-home/inbox && sudo chown -R trustyclaw-agent:trustyclaw-agent /mnt/trustyclaw-agent/agent-home/inbox'
```

## Cost

The default deployment is intended to be small but always-on. It currently
creates one `t3.small` EC2 instance, a 16 GiB root gp3 EBS volume, a 16 GiB
admin gp3 EBS volume (Postgres data and event retention live there), an 8 GiB
agent gp3 EBS volume, and one public IPv4 address.

As a rough us-east-1 estimate for a host running all month:

| Item | Estimate |
| --- | ---: |
| EC2 `t3.small` Linux instance | about `$15/month` |
| 40 GiB total gp3 EBS storage | about `$3.50/month` |
| One public IPv4 address | about `$4/month` |
| **AWS infrastructure subtotal** | **about `$22.50/month`** |

Actual AWS cost varies by region, month length, free-tier credits, taxes, data
transfer, snapshots, and any T3 burst CPU credit charges. The root EBS volume is
deleted when its EC2 instance is terminated. The durable admin and agent EBS
volumes are explicitly marked not to delete on instance termination and continue
to cost money until deleted, even if the EC2 instance is replaced. Check the
current [EC2 On-Demand pricing](https://aws.amazon.com/ec2/pricing/on-demand/),
[EBS pricing](https://aws.amazon.com/ebs/pricing/), [VPC public IPv4 pricing](https://aws.amazon.com/vpc/pricing/),
or the [AWS Pricing Calculator](https://calculator.aws/) for your region.

AI provider costs are separate. Codex/OpenAI and Claude/Anthropic usage is billed
by those providers on top of the AWS infrastructure cost.

## Internals

For deeper architecture and contribution notes, read:

- [`docs/architecture/index.md`](docs/architecture/index.md)
- [`docs/development/index.md`](docs/development/index.md)
- [`docs/api/index.md`](docs/api/index.md)
