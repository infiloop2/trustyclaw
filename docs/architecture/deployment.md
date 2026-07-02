# Deployment and Upgrades

TrustyClaw has explicit host lifecycle commands:

```text
python3 -m host.cli.deploy --config <deploy-config>
python3 -m host.cli.upgrade --config <upgrade-config>
python3 -m host.cli.recover --config <recover-config>
python3 -m host.cli.reconfigure --config <operator-config>
python3 -m host.cli.start --config <config>
python3 -m host.cli.stop --config <config>
```

Each command validates its own config shape, reads AWS credentials from the
configured environment variables, and provisions an Ubuntu 22.04 instance in
the account's default VPC. It only selects a default subnet whose route table
has an active `0.0.0.0/0` route to an internet gateway, because bootstrap
currently provisions over a temporary SSH connection and Cloudflare Tunnel also
needs outbound internet reachability; otherwise it errors before launching.

The command is intentionally split by lifecycle intent:

| Command | Preconditions checked before launch | Bootstrap version check | State effect |
| --- | --- | --- | --- |
| `host.cli.deploy` | No existing TrustyClaw instance or admin/agent data volumes for `agent_name`. | Admin state must be empty. | Creates new admin and agent state at the repo `VERSION`. |
| `host.cli.upgrade` | Existing instance plus existing admin and agent data volumes. | Admin state version must be lower than the repo `VERSION`. | Replaces root volume and preserves admin password, operator endpoints, network policy, tasks, account pins, and agent home. |
| `host.cli.recover` | Existing admin and agent data volumes and no existing TrustyClaw instance. | Admin state version must equal the repo `VERSION`; with `--allow-upgrade`, it may be older than or equal to the repo `VERSION`. | Creates a new root host using preserved admin password and operator endpoints. With `--allow-upgrade`, it can also advance older preserved state. |
| `host.cli.reconfigure` | Existing instance plus existing admin and agent data volumes. | Admin state version must equal the repo `VERSION`. | Replaces root volume, replaces the full operator endpoint list, and installs a new admin password. |
| `host.cli.start` | Exactly one existing TrustyClaw instance plus existing admin and agent data volumes. | None. | Starts the EC2 instance and waits for `running`; does not mutate TrustyClaw state. |
| `host.cli.stop` | Exactly one existing TrustyClaw instance plus existing admin and agent data volumes. | None. | Stops the EC2 instance and waits for `stopped`; does not mutate TrustyClaw state. |

Deploy and reconfigure configs include `operator_connections` because those
commands create or refresh operator access. Supported endpoint modes are `ssh`
and `cloudflare_access`; at least one endpoint is required and only one endpoint
per mode is currently allowed. `reconfigure` always receives the full desired
endpoint list rather than a partial patch. Upgrade, recover, start, and stop
configs omit `operator_connections`; upgrade/recover bootstrap preserves the
stored endpoint list from admin-state `config.json`, while start/stop never run
bootstrap.

Provisioning happens in two stages:

1. **EC2 user data** (small, secret-free): creates the `trustyclaw-operator`
   account with only the generated single-use deploy key, and grants
   `trustyclaw-operator` passwordless sudo. User data never installs persistent
   operator SSH keys or Cloudflare credentials.
2. **Over SSH with the deploy key**: deploy copies the runtime code and a bootstrap
   script to the instance and runs the bootstrap as root. Bootstrap mounts the
   durable data volumes, enforces the command/version preconditions against the
   authoritative admin disk state, installs packages (Python, PostgreSQL,
   Node, npm, Codex CLI, Claude Code CLI, nftables, OpenSSL),
   applies pending security updates, starts the admin-state Postgres on the
   durable admin volume and applies schema migrations, creates a 6 GiB
   swapfile, creates the proxy CA, installs the systemd services, seeds an
   empty runtime network policy only when no preserved policy exists, installs
   optional Cloudflare Tunnel service state, writes the final admin state
   version after successful setup, and exits. The lifecycle CLI then reads
   the final runtime config over the single-use SSH key, updates EC2 SSH ingress
   to match the stored operator endpoints, and deletes the deploy key.

SSH port 22 is open only while provisioning and remains open only when the final
stored endpoint list contains an `ssh` endpoint. If only Cloudflare Access is
configured, bootstrap removes persistent SSH keys and the lifecycle CLI revokes
EC2 security-group SSH ingress before returning success.

For Cloudflare Access, bootstrap installs a pinned `cloudflared` binary and a
`trustyclaw-cloudflared.service` systemd unit with `Restart=always` and
`WantedBy=multi-user.target`, so the tunnel reconnects after service crashes and
host reboots. Bootstrap fails if the service does not become active or if the
configured hostname does not return a Cloudflare Access login/deny response.
The admin password is still required behind Cloudflare Access.

Egress in the security group is pinned to TCP 80, TCP 443, UDP 123 (NTP), and
optionally TCP/UDP 7844 for Cloudflare Tunnel. During provisioning, the
lifecycle CLI always opens TCP/UDP 7844 because bootstrap may need to start
`cloudflared` and verify Cloudflare Access before the final operator endpoint
list has been applied. After bootstrap succeeds, the CLI reads the final stored
operator endpoints: it leaves TCP/UDP 7844 open only when that list contains a
`cloudflare_access` endpoint, and revokes it otherwise. Bootstrap downloads and
proxied agent traffic use 80/443, timesync uses UDP 123, and DNS to the VPC
resolver bypasses security groups. This 7844 rule is outbound egress only; it
does not open inbound access to the EC2 instance and does not create a listener
the internet can connect to. The host-level nftables policy is the per-user
enforcement layer: only root and the dedicated `cloudflared` uid can use the
Cloudflare Tunnel egress path, while `trustyclaw-agent`, `trustyclaw-admin`, and
`trustyclaw-proxy` cannot directly send traffic on 7844. If the agent has not
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
  thread/session mappings, provider account metadata, config) plus the
  proxy policy files, network events, and proxy CA material.
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

If provisioning fails after `deploy` creates new admin or agent data volumes but
before the host is usable, deploy terminates the half-provisioned instance and
prints the created data volume ids. It leaves the data volumes in place rather
than guessing whether they are disposable. A later `deploy` retry refuses those
existing volumes and explains that blank volumes from a failed first install
must be deleted before retrying. Recovery commands are reserved for initialized
TrustyClaw volumes with admin state.

Upgrade preserves credentials by reading the existing stored config (the
`config` table in the admin-state database) and carrying forward
`admin_password_sha256` and `operator_connections`.
`reconfigure` replaces `operator_connections` from the input config and
refreshes `admin_password_sha256` every time, using `--admin-password-env` when
supplied and generating a new password otherwise.

## Secret handling

The admin password is generated by `host.cli.deploy` and `host.cli.reconfigure`,
or supplied to either command with `--admin-password-env`, then written only to
the local result file (mode 0600). The host stores just its SHA-256 hash in
`config.json`, so no file, log, or instance metadata on the host ever contains
the cleartext. `host.cli.upgrade`, `host.cli.recover`, `host.cli.start`, and
`host.cli.stop` preserve the existing password and omit it from their result
files.

Cloudflare Tunnel tokens are secrets. The input config names a local environment
variable, but the token value is copied into admin-state `config.json` because a
replacement root volume must be able to recreate the `cloudflared` service and
the service must reconnect after host reboot. Bootstrap creates a dedicated
unprivileged Linux user and group named `cloudflared`, and
`trustyclaw-cloudflared.service` runs as that `cloudflared` user. The token is
also written to `/etc/trustyclaw/cloudflared.token` on the root volume with
owner `root`, group `cloudflared`, and mode `0640`. That means the token file is
readable by root and by processes running as the `cloudflared` user/group; it is
not readable by the `trustyclaw-admin`, `trustyclaw-agent`,
`trustyclaw-proxy`, or `trustyclaw-operator` users. The service passes the token
to cloudflared with `--token-file`, so the token value is not exposed in process
argv.
