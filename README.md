# TrustyClaw

TrustyClaw is a controlled AI agent host with strong network activity gating.
It runs Codex and Claude Code on infrastructure you own while keeping the agent
behind an explicit, auditable network policy. Learn more at
[trustyclaw.me](https://trustyclaw.me).

## Deploy Your First Host

TrustyClaw uses Cloudflare Access to give the admin UI a stable HTTPS address
protected by your Cloudflare identity. The steps below use this setup. It takes
a few extra steps if you are new to Cloudflare, but once configured you can
open TrustyClaw securely from any browser, including mobile.

Alternatively, you can deploy without HTTPS UI access and connect using SSH
port forwarding. That setup is simpler, but the UI is available only from a
computer that holds your SSH private key, not from mobile or browsers on other
devices. Follow [Deploy Without Cloudflare](#deploy-without-cloudflare) for
that path. Tailscale SSH support is coming.

### Before You Start

You need:

- An AWS account. The walkthrough works with a newly created AWS account, so
  no prior AWS configuration is required.
- A macOS or Linux terminal with Git, Python 3.11, and
  [AWS CLI v2.32.0 or newer](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).
- A Cloudflare account. The walkthrough works with a newly created Cloudflare
  account, so no prior Cloudflare configuration is required.

### Cost

TrustyClaw deploys one `t3.small` EC2 instance, one public IPv4 address, and
40 GiB of gp3 disk, plus a Cloudflare Tunnel and Access configuration. A newly
created [AWS Free Tier](https://aws.amazon.com/free/) account usually costs
`$0` while its included credits remain; outside those credits, expect about
`$22/month` in `us-east-1`.
[Cloudflare's free plan](https://www.cloudflare.com/plans/zero-trust-services/)
costs `$0` for limited personal use. AI provider usage is billed separately
through your Codex or Claude Code subscription.

### 1. Download TrustyClaw

```bash
git clone https://github.com/infiloop2/trustyclaw.git
cd trustyclaw
```

### 2. Create Temporary AWS Administrator Credentials

For a brand-new AWS account, the easiest path is to use the account owner for
this first deployment:

1. Open the [AWS console](https://console.aws.amazon.com/) and choose **Sign in
   using root user email**.
2. Sign in with the email address used to create the AWS account. Enable MFA on
   the root user if you have not already.
3. In your terminal, run the commands below. `aws login` opens the browser and
   lets you select the signed-in account owner session.

```bash
aws login
eval "$(aws configure export-credentials --format env)"
aws sts get-caller-identity
```

The last command prints the account and identity that will create the
TrustyClaw resources. This creates temporary administrator credentials in the
current terminal; it does not create or store a root access key.

If you use an IAM user or federated identity instead, its administrator needs
to grant the permissions in [`iam_policy.json`](iam_policy.json). Then run the
same commands while signed in as that identity.

If `aws login` is unavailable, update AWS CLI v2. Existing access keys also
work; export `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, then run
`unset AWS_SESSION_TOKEN`. The configs below use the temporary token created by
`aws login`; if you use access keys, delete the `aws_session_token_env` line.

### 3. Set Up Cloudflare Access

Cloudflare Access is recommended because it gives TrustyClaw a persistent HTTPS
address and an admin UI you can open securely from anywhere. To deploy without
Cloudflare, skip steps 3 and 4; use
[Deploy Without Cloudflare](#deploy-without-cloudflare) instead.

You will create an active domain, a Zero Trust organization, an Access
application, a tunnel, and a published hostname. Cloudflare moves dashboard
menus occasionally; if a label differs, look for the same concept in its
current [tunnel setup](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/)
and [self-hosted application](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)
instructions.

#### 3.1. Add an Active Domain

- Sign up at [dash.cloudflare.com](https://dash.cloudflare.com) on the free
  plan.
- Either buy a domain through **Domain Registration > Register Domains**, or
  select **Account Home > Add a domain** and change the nameservers at your
  current registrar to the two Cloudflare assigns you.
- Wait for an added domain to show **Active** before continuing. Nameserver
  changes can take from minutes to a day or two. A domain bought through
  Cloudflare activates immediately.

#### 3.2. Complete Zero Trust Onboarding

- Open **Zero Trust** in the Cloudflare sidebar.
- Pick a unique team name and select the **Free** plan. Cloudflare requires a
  payment method even for this `$0` plan, but does not charge for the plan.

#### 3.3. Protect Your TrustyClaw Hostname

Choose the final hostname now, for example `trustyclaw.example.com`. Create the
Access application before publishing the tunnel route so the hostname is
deny-by-default throughout setup.

- Go to **Zero Trust > Access controls > Applications > Create new
  application**, choose **Self-hosted and private**, then select **Add public
  hostname**.
- Name the application and enter the final hostname exactly, with no path.
- Under **Access policies**, create or attach an **Allow** policy that matches
  only you. The simplest rule is **Emails** with your own email address.
- Select the default Cloudflare identity provider. New Zero Trust
  organizations let account members sign in with their Cloudflare account.
  One-time PIN is optional and can be added under **Zero Trust > Integrations >
  Identity providers**.
- Accept the remaining defaults and create the application. Users who do not
  match an Allow policy are denied.

#### 3.4. Create a Tunnel and Copy Its Token

- Go to **Networking > Tunnels > Create a tunnel** and name it, for example
  `trustyclaw`.
- The next screen shows connector installation commands. Do not run them;
  TrustyClaw installs the connector on the host. Copy only the long token
  starting with `eyJ` from the end of any installation command.
- The tunnel remains **Inactive** or **Down** until deploy connects it. This is
  expected.

#### 3.5. Publish the Hostname

- Open the tunnel, then select **Routes > Add route > Published application**.
- Enter the exact hostname protected by the Access application.
- Set **Service URL** to `http://localhost:7443`. This hop stays on the host's
  loopback interface; browsers still reach the hostname over HTTPS.
- Save the route. Cloudflare creates its DNS record automatically, so do not
  create another one.

#### 3.6. Export the Tunnel Token

```bash
export TRUSTYCLAW_CLOUDFLARE_TUNNEL_TOKEN='eyJ...'
```

If you lose the token, open the tunnel's **Overview** tab and copy it from the
installation command again, or select **Refresh token**.

### 4. Create the Deploy Config

Create a file named `config.json` with the content below. Replace
`trustyclaw.example.com` with the hostname from step 3. This deploys a host
named `my-trustyclaw` in `us-east-1`; change either value if needed.

```json
{
  "agent_name": "my-trustyclaw",
  "aws_region": "us-east-1",
  "aws_access_key_id_env": "AWS_ACCESS_KEY_ID",
  "aws_secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
  "aws_session_token_env": "AWS_SESSION_TOKEN",
  "operator_connections": [
    {
      "mode": "cloudflare_access",
      "hostname": "trustyclaw.example.com",
      "tunnel_token_env": "TRUSTYCLAW_CLOUDFLARE_TUNNEL_TOKEN"
    }
  ]
}
```

The config stores only environment variable names, not credentials or the
tunnel token. `agent_name` is the stable identity used to find this host and
its preserved data volumes during future lifecycle commands. Keep it unchanged
after deploy.

### 5. Deploy

```bash
python3 -m host.cli.deploy \
  --config config.json \
  --result-file trustyclaw-deploy.json
```

Wait for `Wrote deploy result to trustyclaw-deploy.json`. The command creates
the host, installs TrustyClaw, and writes the address and generated admin
password to that local file. The file is created with mode `0600`; keep it
private.

The AWS credentials are no longer needed after deploy. Remove them from this
terminal and end the browser login session:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
aws logout
```

### 6. Open TrustyClaw

Copy `admin_password` from the deploy result in `trustyclaw-deploy.json`.

If you used Cloudflare, the tunnel now shows **Healthy**. Open the hostname from
step 3, complete Cloudflare Access authentication, then sign in with the admin
password.

If you deployed without Cloudflare, use the `public_dns` value from the same
deploy result to start the SSH tunnel. Leave this terminal open:

```bash
ssh -i ~/.ssh/trustyclaw_operator \
  -L 7443:127.0.0.1:7443 \
  trustyclaw-operator@<public-dns>
```

Then open [http://127.0.0.1:7443](http://127.0.0.1:7443) and sign in with the
admin password. Type `exit` in the SSH terminal to close the tunnel; the host
keeps running.

Your host is ready. The admin UI guides you through connecting an AI provider,
enabling network access, and adding optional tools.

### Deploy Without Cloudflare

Use this SSH-only path in place of steps 3 and 4 above. It keeps public SSH
access open and makes the admin UI available only while you run a tunnel from
your computer. You also need OpenSSH locally.

Create a dedicated SSH key. Skip this command if the file already exists:

```bash
ssh-keygen -t ed25519 -C trustyclaw-operator -f ~/.ssh/trustyclaw_operator
```

Print the public key:

```bash
cat ~/.ssh/trustyclaw_operator.pub
```

Create a file named `config.json` with the content below. Replace the
`ssh_public_key` value with the complete output from the command above.

```json
{
  "agent_name": "my-trustyclaw",
  "aws_region": "us-east-1",
  "aws_access_key_id_env": "AWS_ACCESS_KEY_ID",
  "aws_secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
  "aws_session_token_env": "AWS_SESSION_TOKEN",
  "operator_connections": [
    {
      "mode": "ssh",
      "ssh_public_key": "ssh-ed25519 AAAA... trustyclaw-operator"
    }
  ]
}
```

Continue to [step 5](#5-deploy).

## Why Use TrustyClaw

- **Runs in the cloud by default:** keep long-running agents active without
  keeping your laptop open.
- **No permission prompts:** the agent runs autonomously in auto-approve mode
  as an unprivileged Linux user, while filesystem and network controls prevent
  broad host-state access, unapproved data leaks, and unexpected internet
  actions.
- **Controlled tools:** bundled tool packages (Gmail, Google Calendar, Brave
  Search, X/Twitter, LinkedIn, LinkedIn Discovery, Instagram, Instagram
  Discovery, Polymarket, Interactive Brokers, Runway media generation) connect
  agents to third-party services through deterministic data paths, with
  operator approval required for outward-facing actions such as sending email
  or publishing a post
  ([tools architecture](docs/architecture/tools/README.md)).
- **Installed apps:** purpose-built product surfaces with richer UX than a
  terminal chat loop, running behind the same host boundaries. These are
  **Agent Chat** (threaded conversations over host tasks) and **Mission
  Pursuit** (a persistent workspace one agent furnishes with a goal, artifacts,
  memory, and scheduled runs).

These choices follow from a broader set of beliefs about running AI agents.
See [PHILOSOPHY.md](./PHILOSOPHY.md).

## Configuration Reference

The first-deploy walkthrough generates a complete `config.json`. To build or
edit one manually, start from the included example:

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
| `aws_session_token_env` | Optional. Environment variable name containing an AWS session token, for temporary credentials from an assumed STS role. |
| `operator_connections` | One or more access endpoints. Use `ssh`, `cloudflare_access`, or both. |
| `operator_connections[].mode` | `ssh` or `cloudflare_access`. |
| `operator_connections[].ssh_public_key` | For SSH, public key content installed for operator access, for example the output of `cat ~/.ssh/id_ed25519.pub`. |
| `operator_connections[].hostname` | For Cloudflare Access, the fixed hostname that routes to the admin UI/API. |
| `operator_connections[].tunnel_token_env` | For Cloudflare Access, environment variable name containing the Cloudflare Tunnel token. |

Deploy reads AWS credentials from the environment variables named in the
config. For long-lived credentials:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
unset AWS_SESSION_TOKEN
```

For temporary credentials, also export `AWS_SESSION_TOKEN` and set
`aws_session_token_env` to `AWS_SESSION_TOKEN`. Repeat the AWS sign-in and
export step before a lifecycle command whenever the temporary credentials have
expired.

### AWS Account Setup

For a first evaluation, the short-lived `aws login` session in the walkthrough
avoids creating an access key. Deploy uses the default VPC and needs a default
subnet with public IPv4 routing.

For regular use or automation, attach `iam_policy.json` to a federated IAM role.
The policy requires TrustyClaw tags on created resources, allows EC2 updates
and cleanup only on TrustyClaw-tagged resources, and leaves region selection to
your deploy config.
See [`docs/architecture/iam-policy.md`](docs/architecture/iam-policy.md) for
why each policy statement is needed and how its resource scope is constrained.

If federation is unavailable, the commands below create a dedicated IAM user
with the same policy. AWS recommends federation instead of long-lived IAM user
credentials for real data.

```bash
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

aws iam create-policy \
  --policy-name trustyclaw-host-deploy \
  --policy-document file://iam_policy.json

aws iam create-user --user-name trustyclaw-host-deploy
aws iam attach-user-policy \
  --user-name trustyclaw-host-deploy \
  --policy-arn "arn:aws:iam::$AWS_ACCOUNT_ID:policy/trustyclaw-host-deploy"

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

The recommended walkthrough above covers Cloudflare setup from a new account.
Its `operator_connections` entry has this shape:

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

## Manage Your Host

Run lifecycle commands from the repository root. Each command writes a local
result file and prints its path. By default, the filename is
`<agent_name>-<mode>.json`. Deploy and reconfigure result files include the
admin password and are created with mode `0600`; keep them private.

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

The admin toolbar quietly shows version status after checking the `VERSION` on
the public repository's `main` branch. A small upgrade icon shows the available
version and reminds you to use the operator plane; a small checkmark confirms
the host is at the latest version. The icons themselves perform no action.

The host uses three EBS volumes:

| Volume | Lifecycle | Contents |
| --- | --- | --- |
| Root | Recreated on redeploy and deleted on instance termination | Ubuntu 22.04, system packages, Node.js, Python, Codex CLI, Claude Code CLI, nftables, OpenSSL, curl, jq, CA certificates, and swap. |
| Admin | Preserved on redeploy and marked not to delete on instance termination | Postgres state for the admin API, apps, tools, tasks, audit logs, network policy, credentials, and provider pins; proxy CA/certificate, queued-push state, and a bounded temporary tool-media spool. |
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

## Admin API and File Uploads

With the SSH tunnel from [step 6](#6-open-trustyclaw) active, call the admin API
directly:

```bash
curl -H "Authorization: Bearer <admin-password>" \
  http://127.0.0.1:7443/v1/health
```

Full admin API documentation is in
[`docs/api/AdminAPI.md`](docs/api/AdminAPI.md).

With SSH operator access, give the agent files from your machine by uploading
them as the operator and moving them into the agent-owned home directory:

```bash
HOST=trustyclaw-operator@<public-dns>

ssh -i <private-key-path> "$HOST" 'rm -rf /tmp/trustyclaw-upload'
scp -i <private-key-path> -r ./my-files "$HOST":/tmp/trustyclaw-upload
ssh -i <private-key-path> "$HOST" \
  'sudo rm -rf /mnt/trustyclaw-agent/agent-home/inbox && sudo mv /tmp/trustyclaw-upload /mnt/trustyclaw-agent/agent-home/inbox && sudo chown -R trustyclaw-agent:trustyclaw-agent /mnt/trustyclaw-agent/agent-home/inbox'
```

## Internals

The host runs on an AWS EC2 instance. The admin API, network proxy, tools
service, installed app backends, optional Cloudflare Tunnel connector,
database, and agent runtime run as separate Linux users. Filesystem ownership,
peer-authenticated local sockets, scoped database roles, and uid-based firewall
rules keep the agent from getting direct network access or broad access to host
state.

For deeper architecture and contribution notes, read:

- [`docs/architecture/diagram.md`](docs/architecture/diagram.md), for a
  one-page host capability map
- [`docs/architecture/index.md`](docs/architecture/index.md)
- [`docs/development/index.md`](docs/development/index.md)
- [`docs/api/index.md`](docs/api/index.md)
- [`docs/audit-reports/README.md`](docs/audit-reports/README.md)

## License

TrustyClaw is source-available under the Business Source License 1.1.
Production or commercial use is not granted by the public license. Commercial
licenses are available on request from the copyright holder.

The Change Date is 2030-07-09, after which the Change License is the GNU
Affero General Public License v3.0 or any later version. See [LICENSE](LICENSE)
and [NOTICE](NOTICE).
