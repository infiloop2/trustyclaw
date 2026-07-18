# Deployment and Upgrades

TrustyClaw has explicit host lifecycle commands:

```text
python3 -m host.cli.deploy --agent-name <name> ...
python3 -m host.cli.upgrade --agent-name <name>
python3 -m host.cli.recover --agent-name <name>
python3 -m host.cli.reconfigure --agent-name <name> ...
python3 -m host.cli.start --agent-name <name>
python3 -m host.cli.stop --agent-name <name>
```

Each command takes arguments plus the standard AWS environment variables
(`AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and
`AWS_SESSION_TOKEN` exactly when set), reads nothing from disk, streams
progress on stderr, prints one result JSON on stdout, and provisions an
Ubuntu 22.04 instance in the account's default VPC. It only selects a default subnet whose route table
has an active `0.0.0.0/0` route to an internet gateway, because provisioning
needs outbound internet reachability (package installs, and on the GitHub
delivery the pinned code fetch), the SSH delivery additionally connects inbound
over a temporary SSH session, and Cloudflare Tunnel also needs outbound
internet reachability; otherwise it errors before launching.

The command is intentionally split by lifecycle intent:

| Command | Preconditions checked before launch | Bootstrap version check | State effect |
| --- | --- | --- | --- |
| `host.cli.deploy` | No existing TrustyClaw instance or admin/agent data volumes for `agent_name`. | Admin state must be empty. | Creates new admin and agent state at the target `VERSION`. |
| `host.cli.upgrade` | Existing instance plus existing admin and agent data volumes. | Admin state version must be lower than the target `VERSION`. | Replaces root volume and preserves admin password, operator endpoints, network policy, tasks, account pins, and agent home. |
| `host.cli.recover` | Existing admin and agent data volumes and no existing TrustyClaw instance. | Admin state version must equal the target `VERSION`; with `--allow-upgrade`, it may be older than or equal to the target `VERSION`. | Creates a new root host using preserved admin password and operator endpoints. With `--allow-upgrade`, it can also advance older preserved state. |
| `host.cli.reconfigure` | Existing instance plus existing admin and agent data volumes. | Admin state version must equal the target `VERSION`. | Replaces root volume, replaces the full operator endpoint list, and installs a new admin password. |
| `host.cli.start` | Exactly one existing TrustyClaw instance plus existing admin and agent data volumes. | None. | Starts the EC2 instance and waits for `running`; does not mutate TrustyClaw state. |
| `host.cli.stop` | Exactly one existing TrustyClaw instance plus existing admin and agent data volumes. | None. | Stops the EC2 instance and waits for `stopped`; does not mutate TrustyClaw state. |

The target `VERSION` is the local checkout's `VERSION`, or the pinned
commit's `VERSION` on the GitHub delivery.

Deploy and reconfigure take operator endpoint arguments
(`--operator-ssh-public-key` and `--operator-cloudflare-hostname`, the tunnel
token from `TRUSTYCLAW_CLOUDFLARE_TUNNEL_TOKEN`) because those commands create
or refresh operator access; at least one endpoint is required and at most one
per mode exists. `reconfigure` always receives the full desired endpoint list
rather than a partial patch. Upgrade, recover, start, and stop reject
endpoint arguments; upgrade/recover bootstrap preserves the stored endpoint
rows from the admin-state database, while start/stop never run bootstrap.

Provisioning artifacts — the payload JSON, the rendered bootstrap script, and
the runtime code archive — are rendered by `host.bootstrap.render` and
delivered to the instance one of two ways.

**SSH delivery** (the default) happens in two stages:

1. **EC2 user data**: creates the `trustyclaw-operator` account with the
   generated single-use deploy key, grants `trustyclaw-operator` passwordless
   sudo, and stages the provisioning payload. Both deliveries stage the same
   payload through user data; user data never installs persistent operator
   SSH keys.
2. **Over SSH with the deploy key**: the CLI copies the local checkout's
   runtime code archive to the instance, which extracts it and runs the same
   `host.bootstrap.self_provision` entry the GitHub delivery uses; the
   bootstrap script is rendered on the instance from the delivered code in
   both deliveries. Bootstrap is a
   sequence of named phases run by one `main`: it mounts the
   durable data volumes, creates the pinned service accounts (rendered from
   `host/constants.py`), enforces the command/version preconditions against the
   authoritative admin disk state, installs packages (Python, PostgreSQL,
   Node, npm, Codex CLI, Claude Code CLI, git, gh, nftables, OpenSSL),
   applies pending security updates, starts the admin-state Postgres on the
   durable admin volume and applies schema migrations, creates a 6 GiB
   swapfile, creates the proxy CA, applies the declarative durable-path
   ownership table, installs the Postgres, proxy, tools, admin,
   app, and optional Cloudflare systemd services. On a fresh database it leaves
   the network-policy row absent so the fail-closed empty default applies.
   After the services start it runs `host.bootstrap.verify_deploy`, which
   independently re-checks the provisioned state — account ids, path
   permissions, service sockets, loopback listeners, active units, database
   peer auth, and live firewall probes in both directions (the agent reaches
   only the loopback proxy; denied users' packets drop) — and fails the deploy
   listing every mismatch. Service-state checks retry briefly to absorb
   startup latency, and positive external egress is advisory only, so a
   healthy deploy on an egress-restricted network cannot false-fail. Only then does bootstrap drop the staged secrets and
   write the final admin state version, then
   exit. Bootstrap deletes the deploy key when it finishes; the CLI's only
   step after bootstrap is revoking the provisioning SSH ingress when the
   operator endpoints do not include `ssh`.

Because the SSH delivery ships whatever is in the local checkout, it is also
the development path: it deploys unpublished code without any public pin.

**GitHub delivery** (`--bootstrap-from-github [COMMIT_SHA]`, available on
deploy, upgrade, recover, and reconfigure) is single-stage and detached. The
pinned commit always comes from the fixed public repository
(`host.constants.PUBLIC_GITHUB_REPOSITORY`, `infiloop2/trustyclaw`); there is
no repository knob, and without a value the latest `main` commit is pinned.
The CLI first reads the pinned commit's `VERSION` from GitHub, and that
version, not the local checkout's, is the operation's target: the CLI prints
the fetched version and asks for confirmation, and non-interactive callers
pipe the confirmation into stdin. Pins older than 0.35.0 are rejected because
`host.bootstrap.self_provision` does not exist before it; the SSH delivery
serves older versions. Any GitHub read failure, an old pin, or a declined
confirmation aborts before anything in AWS is touched.

EC2 user data then hardens the base accounts, stages the provisioning payload,
installs git, fetches the pinned commit (a `git fetch` of the full commit sha,
so the checkout content is verified by the commit hash), and runs
`host.bootstrap.self_provision`. Because the CLI preflight already proved the
commit exists and is readable, host-side failures of the package install or
the fetch are transient network or GitHub availability issues; both retry for
an extended window (roughly half an hour each) so an outage delays
provisioning instead of failing the instance. `self_provision` fails closed
unless the checkout `VERSION` equals the operation's target version, then
renders the same bootstrap script and runtime code archive from the checkout
and runs bootstrap on the instance itself. No deploy key exists in this
delivery, and the CLI returns once the instance is launched with its data
volumes attached: the command does not wait for or observe bootstrap.
Bootstrap outcome is observable through the operator endpoints coming up (the
Cloudflare Tunnel connects only after bootstrap and its verification succeed)
and through cloud-init output in the EC2 console log; a host whose bootstrap
failed is destroyed and recreated with a fresh lifecycle command rather than
repaired.

Security-group access is derived the same way on both deliveries, before
launch: deploy and reconfigure derive SSH ingress and the Cloudflare
connector egress from the input operator connections, while upgrade and
recover reapply the previous converged security-group state because their
operator endpoints are preserved on the admin volume and unchanged by the
operation. When no security group exists to read, they launch with SSH
ingress closed and the connector egress open. The GitHub delivery launches
with that state directly and never changes it afterward; the SSH delivery
additionally opens SSH at launch for the single-use deploy key and closes it
after bootstrap when the derived state says so.

On the SSH delivery, SSH port 22 is open while provisioning and remains open
only when the final stored endpoint list contains an `ssh` endpoint. If only
Cloudflare Access is configured, bootstrap removes persistent SSH keys and the
lifecycle CLI revokes EC2 security-group SSH ingress before returning success.
On the GitHub delivery, port 22 opens at launch only when the operator
endpoints include an `ssh` endpoint, and bootstrap installs that endpoint's
key; there is never a provisioning-only SSH window.

Operator endpoints expose the admin API/UI only. App backend services bind
host-assigned loopback ports and are not forwarded directly over SSH,
Cloudflare Access, or the EC2 security group; operator app requests go through
the authenticated admin API app proxy.

For Cloudflare Access, bootstrap installs a pinned `cloudflared` binary and a
`trustyclaw-cloudflared.service` systemd unit with `Restart=always` and
`WantedBy=multi-user.target`, so the tunnel reconnects after service crashes and
host reboots. Bootstrap fails if the service does not become active or if the
configured hostname does not return a Cloudflare Access login/deny response.
The admin password is still required behind Cloudflare Access.

Egress in the security group is pinned to TCP 80, TCP 443, UDP 123 (NTP), and
optionally TCP/UDP 7844 for Cloudflare Tunnel. The 7844 connector allowance
is decided at launch on both deliveries, from the same derived operator
access state, and opened only when the endpoints include a
`cloudflare_access` endpoint. Bootstrap downloads and
proxied agent traffic use 80/443, timesync uses UDP 123, and DNS to the VPC
resolver bypasses security groups. This 7844 rule is outbound egress only; it
does not open inbound access to the EC2 instance and does not create a listener
the internet can connect to. The host-level nftables policy is the per-user
enforcement layer: only root and the dedicated `cloudflared` uid can use the
Cloudflare Tunnel egress path, while `trustyclaw-agent`, `trustyclaw-admin`,
`trustyclaw-proxy`, and `trustyclaw-tools` cannot directly send traffic on
7844. If the agent has not
escalated to root or compromised the `cloudflared` process/user, opening 7844
does not give it a new network path. The security group bounds what even a fully
compromised host could reach, though only by port — per-domain egress control
is the proxy's job.

The instance requires IMDSv2 and has no IAM role, so agent code cannot obtain AWS
credentials from instance metadata.

## Drive lifecycle

TrustyClaw splits host data across three EBS volumes with different lifecycle
contracts:

- **Root drive**: replaceable system volume. It holds the OS, packages,
  root-owned TrustyClaw runtime code, bootstrap helpers, systemd units,
  nftables config, global CLI installs, logs, and swap.
- **Admin state drive**: durable data volume mounted at `/mnt/trustyclaw-admin`.
  It holds the admin-state Postgres data directory (task history,
  thread/session mappings, provider account metadata, config, network state,
  tool state, and app migration records) plus proxy CA, generated certificate,
  held-Git-push files, and the bounded temporary tool-media spool.
- **Agent state drive**: durable data volume mounted at `/mnt/trustyclaw-agent`.
  It holds the agent user's home directory, provider auth/session files, CLI
  caches, and workspace data.

On first deploy, all three drives are created from scratch: deploy creates the
EC2 root volume with the instance and creates the admin and agent data volumes,
then bootstrap formats and mounts the data volumes, initializes the Postgres
data directory, and applies the full migration history to create an empty
schema.

The root drive is launched with `DeleteOnTermination=true`, so terminating the
EC2 instance deletes that root volume. The admin and agent data volumes are
created separately, attached after launch, and explicitly set to
`DeleteOnTermination=false` on the instance block-device mapping. Terminating
or replacing the EC2 instance should detach those data volumes and leave them in
the account until a cleanup path deletes them.

On upgrade or reconfiguration, deploy treats any existing EC2 instance and root
drive as disposable. It terminates the instance, creates a fresh root drive from
the current Ubuntu image, reinstalls packages and TrustyClaw root-owned code,
and reattaches the preserved admin and agent state drives for the same
`agent_name`. `recover` does the same host creation from preserved drives, but
requires that no current TrustyClaw instance exists. Bootstrap refreshes
root-volume code and config, sanitizes managed mount paths against symlink
attacks, reuses the preserved Postgres data directory, and runs `migrate up` to
bring the preserved schema to the new code's version; it does not overwrite
task history, provider account pins, agent home data, or an existing runtime
network policy. `start` and `stop` never replace the root drive or attach/detach data
volumes; they only transition the existing EC2 instance power state.

## Version controls

The repository root `VERSION` file is the software version that deploy embeds in
new root volumes. CI requires pull requests to increment it relative to the
target branch. Versions use `MAJOR.MINOR.PATCH`.

Version is stored in three places with different authority:

| Location | Authority | Purpose |
| --- | --- | --- |
| `/mnt/trustyclaw-admin/admin-state/version.json` | Authoritative preserved-state version. | Bootstrap reads this after mounting the admin disk and uses it to allow or deny deploy, upgrade, and recovery. |
| `/opt/trustyclaw-host/VERSION` | Running root/runtime version. | Admin API health compares it with admin state so mismatches are visible. |
| EC2 tag `trustyclaw-host-version` | Pre-replacement safety hint, not state authority. | Lets operators see the last launched root version from AWS before SSH/bootstrap. Deploy uses a parseable tag to reject lifecycle commands that bootstrap would reject before terminating the instance. Bootstrap still follows admin state if the tag is stale. |

This avoids making AWS tags authoritative for preserved state. A tag can be
stale after a partial recovery or manual AWS operation, but the admin disk is
the preserved state being upgraded. The deploy command uses a valid EC2 tag as
an early, fail-closed guard before destructive replacement. Missing tags defer
to bootstrap. Invalid tags stop the command before replacement because they
indicate a corrupted safety hint. Bootstrap performs the authoritative check
once the admin volume is attached.

`/v1/health` includes `version.status`, `version.runtime`, and `version.state`.
Health is degraded if the root version is missing, the admin state version is
missing, or they do not match.

The admin service also checks the public
`infiloop2/trustyclaw` main-branch `VERSION` on startup and every four hours.
The result is advisory and process-local: `/v1/health` reports it under
`upgrade`, and the admin toolbar shows a small passive upgrade icon only when
that version is strictly newer than the running root version. Hovering or
focusing the icon shows the available version and tells the operator to use the
operator plane to upgrade. Equal versions and private builds ahead of public
main are both current, so the same spot shows a small checkmark whose popup
confirms that TrustyClaw is at the latest version. Neither icon is an action. A
failed check hides the status only until the first successful check; later
failures preserve the last successful version without degrading host health.

Admin-state schema changes ride the normal upgrade: every change to persisted
state shape ships as a versioned SQL migration in `host/migrations/`, applied
by bootstrap during redeploy before the admin state version is written; the
admin service itself never migrates. See
[Admin state storage and migrations](admin-state-storage.md) for the rules.
The Postgres *major* version is pinned in bootstrap and the data directory is
versioned by major, so a future base-image change requires an explicit
`pg_upgrade` step rather than a silent binary/data mismatch.

## Upgrade controls

Upgrade/recovery safety checks exist in two layers:

1. The deploy command checks AWS shape before it mutates anything. `deploy`
   refuses existing resources, `upgrade` requires an existing instance and both
   preserved volumes, `recover` requires both preserved volumes and no existing
   instance, and `reconfigure` requires an existing instance plus both
   preserved volumes. It also validates that preserved volumes are in one
   availability zone and selects a matching public subnet before terminating an
   existing instance. If an existing instance has a parseable
   `trustyclaw-host-version` tag, the command applies the same mode-specific
   version shape bootstrap will enforce: `upgrade` requires an older tag,
   `reconfigure` requires an equal tag, and `recover --allow-upgrade` allows
   equal or older tags when no instance exists. `start` and `stop` require
   exactly one existing instance and both preserved volumes but do not check
   version because they do not mutate TrustyClaw state.
2. Bootstrap checks the authoritative admin disk version after the volumes are
   mounted and managed path slots are sanitized. This catches stale EC2 tags,
   partially completed previous runs, and any mismatch between AWS discovery and
   the actual preserved state.

A failed provisioning run leaves no instance, on either delivery. While the
CLI is attached it terminates the instance itself. After the CLI returns (the
GitHub delivery), the user-data script shuts the instance down on any failure,
which terminates it: instances launch with instance-initiated shutdown
behavior set to terminate, because the root volume is disposable by contract
and the durable data volumes survive termination. The `stop` command parks
compute through the EC2 API, which that attribute does not affect; an
OS-level shutdown on any TrustyClaw host terminates it, and `recover`
rebuilds the host from the preserved volumes.

Data volumes created by a failed run are left in place and their ids are
printed when the CLI is attached. A later `deploy` retry refuses those
existing volumes and explains that blank volumes from a failed first install
must be deleted before retrying; preserved volumes are never deleted.
Recovery commands are reserved for initialized TrustyClaw volumes with admin
state.

Upgrade preserves credentials by reading the existing stored config (the
`config` table in the admin-state database) and carrying forward
`admin_password_sha256` and `operator_connections`.
`reconfigure` replaces `operator_connections` from the input config and
installs the `--admin-password-sha256` digest every time.

## Secret handling

The lifecycle CLI never handles the admin password. `host.cli.deploy` and
`host.cli.reconfigure` require `--admin-password-sha256` with the SHA-256 hex
digest of the operator's chosen password, computed locally (for example
`printf %s 'your-password' | sha256sum`); `host.cli.generate_password` prints
a generated password and its digest for operators who want one made for them. The host stores just that hash in
the database `config` row, so no CLI process, result file, log, or instance
metadata anywhere ever contains the cleartext. `host.cli.upgrade`,
`host.cli.recover`, `host.cli.start`, and `host.cli.stop` preserve the
existing stored hash.

Cloudflare Tunnel tokens are secrets. The input config names a local environment
variable, but the token value is encrypted into the database
`operator_connections` row because a replacement root volume must be able to
recreate the `cloudflared` service and the service must reconnect after host
reboot. On both deliveries, the provisioning payload rides in EC2 user
data: it carries the admin password hash (never the cleartext) and the runtime
operator connection values, including the Cloudflare Tunnel token. User data is
readable by root on the instance through instance metadata and by AWS
principals holding `ec2:DescribeInstanceAttribute` in the target account, so
the token's exposure there is bounded by that account's own IAM surface;
`reconfigure` rotates operator access, replacing the token. Bootstrap creates a dedicated
unprivileged Linux user and group named `cloudflared`, and
`trustyclaw-cloudflared.service` runs as that `cloudflared` user. The token is
also written to `/etc/trustyclaw/cloudflared.token` on the root volume with
owner `root`, group `cloudflared`, and mode `0640`. That means the token file is
readable by root and by processes running as the `cloudflared` user/group; it is
not directly readable by the `trustyclaw-admin`, `trustyclaw-agent`, or
`trustyclaw-proxy` users. The SSH operator can read it only by deliberately
using unrestricted sudo. The service passes the token
to cloudflared with `--token-file`, so the token value is not exposed in process
argv.
