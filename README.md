# TrustyClaw

TrustyClaw is a controlled AI agent host with strong network activity gating.
It lets you run Codex and Claude Code on infrastructure you own while keeping
the agent behind an explicit, auditable network policy.

The host runs on an AWS EC2 instance and exposes a local admin UI/API through an
SSH tunnel. The admin process, network proxy, and agent runtime run as separate
Linux users with separate storage so the agent can work autonomously without
getting direct network access or broad access to host state.

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
3. An SSH key pair for operator access.

Start from the included example config:

```bash
cp example_config.json config.json
```

The deployment config creates the host with SSH access for the operator and no
agent network access. After deploy, use the admin UI network policy controls to
enable managed AI providers or add website/domain rules; see
[`docs/api/NetworkControls.md`](docs/api/NetworkControls.md) for the runtime
policy schema.

In `config.json`, set:

| Field | What To Put |
| --- | --- |
| `agent_name` | Stable host name. Deploy uses it to find/redeploy the same host. |
| `aws_region` | AWS region to deploy into. |
| `aws_access_key_id_env` | Environment variable name containing the AWS access key id. |
| `aws_secret_access_key_env` | Environment variable name containing the AWS secret access key. |
| `ssh_public_key` | Public key content installed for SSH access, for example the output of `cat ~/.ssh/id_ed25519.pub`. |
| `ssh_port_opened` | Required and must be `true`; SSH tunneling is the supported admin access path. |

Deploy reads AWS credentials from the environment variables named in your config:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
```

You can use an administrator access key while evaluating the project. For regular
use, create an IAM user or role with the policy in `iam_policy.json`. It requires
TrustyClaw tags on created resources, allows EC2 updates and cleanup only on
TrustyClaw-tagged resources, and leaves region selection to your deploy config.
See [`docs/IAMPolicy.md`](docs/IAMPolicy.md) for why each policy statement is
needed and how its resource scope is constrained.

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

See [`docs/api/InputConfig.md`](docs/api/InputConfig.md) for the full input
schema for customization.

## Deploy

Run deploy from the repository root:

```bash
python3 -m host.deploy --config config.json
```

Deploy reads the config and writes a sensitive result file named
`<agent_name>.json`. That file contains the generated admin password and is
created mode `0600`.

If `agent_name` already identifies an existing TrustyClaw EC2 instance, deploy
prompts before upgrading or recovering it. Upgrade/recover replaces the EC2
instance and root volume while reusing the admin and agent data volumes.

Deploy flags:

| Flag | Behavior | When to use |
| --- | --- | --- |
| `--config <path>` | Required. Reads the deploy input config from `<path>`. | Every deploy. |
| `--allow-upgrade-or-recover` | If an existing host with the same `agent_name` is found, approve replacing its EC2 instance and root volume without prompting. Preserved admin and agent data volumes are reused unless the dangerous storage reset flag is also set. | Non-interactive upgrades or recovery runs. |
| `--admin-password-env <name>` | Reads the admin password from environment variable `<name>` instead of generating a new one. The host still receives only the password hash. | Hosts where the admin password should stay stable across upgrades. |
| `--reset-storage-dangerous-delete` | Deletes existing preserved admin and agent data volumes before creating replacements. This permanently removes admin API state, tasks, events, network policy, provider account pins, proxy CA state, provider auth/session files, CLI caches, and agent workspace data. | Intentional full state reset only. |

The host uses three EBS volumes:

| Volume | Lifecycle | Contents |
| --- | --- | --- |
| Root | Recreated on redeploy | Ubuntu 22.04, system packages, Node.js, Python, Codex CLI, Claude Code CLI, nftables, OpenSSL, curl, jq, CA certificates, and swap. |
| Admin | Preserved on redeploy | Admin API state, tasks, agent events, network events, network policy, provider account pins, and proxy CA state. |
| Agent | Preserved on redeploy | Agent home directory, provider auth/session files, CLI caches, and workspace data. |

Every AWS resource deploy creates is tagged so it can be found and cleaned up:

| Tag | Value | On |
| --- | --- | --- |
| `trustyclaw-host-agent-name` | `<agent_name>` | instance, volume, security group |
| `trustyclaw-host` | `true` | instance, volume, security group |
| `Name` | `trustyclaw-host-<agent_name>` | instance, volume |
| `trustyclaw-host-volume-role` | `admin` or `agent` | data volumes |

See [`docs/api/DeployResult.md`](docs/api/DeployResult.md) for the deploy result
schema.

## Connect

The host exposes the admin UI/API on localhost inside the EC2 instance. Forward
it over SSH:

```bash
ssh -i <private-key-path> -L 7443:127.0.0.1:7443 trustyclaw-operator@$(jq -r .public_dns <agent_name>.json)
```

After forwarding is active, open `http://127.0.0.1:7443` in your browser, or
call the API directly:

```bash
curl -H "Authorization: Bearer $(jq -r .admin_password <agent_name>.json)" \
  http://127.0.0.1:7443/v1/health
```

Full admin API documentation is in
[`docs/api/AdminAPI.md`](docs/api/AdminAPI.md).

To give the agent files from your machine, upload them as the operator and then
move them into the agent-owned home directory:

```bash
HOST=trustyclaw-operator@$(jq -r .public_dns <agent_name>.json)

ssh -i <private-key-path> "$HOST" 'rm -rf /tmp/trustyclaw-upload'
scp -i <private-key-path> -r ./my-files "$HOST":/tmp/trustyclaw-upload
ssh -i <private-key-path> "$HOST" \
  'sudo rm -rf /mnt/trustyclaw-agent/agent-home/inbox && sudo mv /tmp/trustyclaw-upload /mnt/trustyclaw-agent/agent-home/inbox && sudo chown -R trustyclaw-agent:trustyclaw-agent /mnt/trustyclaw-agent/agent-home/inbox'
```

## Cost

The default deployment is intended to be small but always-on. It currently
creates one `t3.small` EC2 instance, a 16 GiB root gp3 EBS volume, an 8 GiB admin
gp3 EBS volume, an 8 GiB agent gp3 EBS volume, and one public IPv4 address.

As a rough us-east-1 estimate for a host running all month:

| Item | Estimate |
| --- | ---: |
| EC2 `t3.small` Linux instance | about `$15/month` |
| 32 GiB total gp3 EBS storage | about `$3/month` |
| One public IPv4 address | about `$4/month` |
| **AWS infrastructure subtotal** | **about `$21/month`** |

Actual AWS cost varies by region, month length, free-tier credits, taxes, data
transfer, snapshots, and any T3 burst CPU credit charges. The durable admin and
agent EBS volumes continue to cost money until deleted, even if the EC2 instance
is replaced. Check the current [EC2 On-Demand pricing](https://aws.amazon.com/ec2/pricing/on-demand/),
[EBS pricing](https://aws.amazon.com/ebs/pricing/), [VPC public IPv4 pricing](https://aws.amazon.com/vpc/pricing/),
or the [AWS Pricing Calculator](https://calculator.aws/) for your region.

AI provider costs are separate. Codex/OpenAI and Claude/Anthropic usage is billed
by those providers on top of the AWS infrastructure cost.

## Internals

For deeper architecture and contribution notes, read:

- [`docs/Architecture.md`](docs/Architecture.md)
- [`docs/Development.md`](docs/Development.md)
