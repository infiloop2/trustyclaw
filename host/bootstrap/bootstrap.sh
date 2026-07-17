#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
umask 077
# Bootstrap is usually launched from the operator home over SSH. Use a neutral
# cwd so runuser children do not inherit an unreadable directory.
cd /
NODE_VERSION=22.12.0
CODEX_CLI_VERSION=0.144.0
CLAUDE_CODE_VERSION=2.1.206
CLOUDFLARED_VERSION=2026.6.1
# Ubuntu 22.04 ships PostgreSQL 14. The data directory below is versioned by
# major so a future base-image bump gets an explicit pg_upgrade step instead
# of a silent mismatch.
PG_MAJOR=14
TRUSTYCLAW_ADMIN_UID=47741
TRUSTYCLAW_ADMIN_GID=47741
TRUSTYCLAW_PROXY_UID=47742
TRUSTYCLAW_PROXY_GID=47742
TRUSTYCLAW_AGENT_UID=47743
TRUSTYCLAW_AGENT_GID=47743
CLOUDFLARED_UID=47744
CLOUDFLARED_GID=47744
POSTGRES_UID=47745
POSTGRES_GID=47745
TRUSTYCLAW_TOOLS_UID=47746
TRUSTYCLAW_TOOLS_GID=47746
TRUSTYCLAW_AGENT_APP_UID=47747
TRUSTYCLAW_AGENT_APP_GID=47747
TRUSTYCLAW_AGENT_NETWORK_UID=47748
TRUSTYCLAW_AGENT_NETWORK_GID=47748
@APP_UID_GID_CONSTANTS@
PROXY_PORT=@PROXY_PORT@
@APP_PORT_CONSTANTS@

# Persistent volume layout. The admin volume is durable across redeploys, so
# the admin-state Postgres data directory and proxy-owned mutable state live
# in separate directories with separate Unix owners.
ADMIN_MOUNT=/mnt/trustyclaw-admin
PGDATA_DIR="/mnt/trustyclaw-admin/postgres/${PG_MAJOR}/main"
PROXY_STATE_DIR=/mnt/trustyclaw-admin/proxy-state
AGENT_MOUNT=/mnt/trustyclaw-agent
AGENT_HOME_PATH=/mnt/trustyclaw-agent/agent-home

# Read one value out of the JSON payload staged by the deploy command.
payload_value() {
  local key="$1"
  python3 - "$key" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path('/tmp/trustyclaw_payload.json').read_text())
value = payload
for part in sys.argv[1].split("."):
    value = value[part]
print(value)
PY
}

# EBS device names requested in the EC2 API are not stable inside Nitro guests;
# resolve by EBS volume id and mount by UUID.
resolve_ebs_device() {
  local volume_id="$1"
  local normalized="${volume_id//-/}"
  local candidates=(
    "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${normalized}"
    "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${volume_id}"
    "/dev/disk/by-id/scsi-0Amazon_Elastic_Block_Store_${normalized}"
    "/dev/disk/by-id/scsi-0Amazon_Elastic_Block_Store_${volume_id}"
  )
  local attempt candidate
  for attempt in $(seq 1 30); do
    for candidate in "${candidates[@]}"; do
      if [ -e "$candidate" ]; then
        readlink -f "$candidate"
        return 0
      fi
    done
    sleep 1
  done
  echo "could not find attached EBS volume device for ${volume_id}" >&2
  return 1
}

# Format a new EBS volume exactly once, then mount it through /etc/fstab.
prepare_volume() {
  local volume_id="$1"
  local mount_point="$2"
  local label="$3"
  local device uuid
  device="$(resolve_ebs_device "$volume_id")"
  if ! blkid "$device" >/dev/null 2>&1; then
    mkfs.ext4 -F -L "$label" "$device"
  fi
  uuid="$(blkid -s UUID -o value "$device")"
  mkdir -p "$mount_point"
  if ! grep -qE "[[:space:]]${mount_point}[[:space:]]" /etc/fstab; then
    echo "UUID=${uuid} ${mount_point} ext4 defaults,nofail 0 2" >> /etc/fstab
  fi
  mountpoint -q "$mount_point" || mount "$mount_point"
}

# Mount durable admin and agent volumes before creating service users; their
# home directories live on those volumes.
admin_volume_id="$(payload_value storage_volumes.admin)"
agent_volume_id="$(payload_value storage_volumes.agent)"
prepare_volume "$admin_volume_id" "$ADMIN_MOUNT" TRUSTYCLAW_ADMIN
prepare_volume "$agent_volume_id" "$AGENT_MOUNT" TRUSTYCLAW_AGENT

# Stable IDs keep durable EBS file owners meaningful across root-volume
# replacement. If an image already uses one of these IDs, fail instead of
# silently assigning preserved data to the wrong service account.
ensure_group() {
  local name="$1"
  local gid="$2"
  local existing
  existing="$(getent group "$name" | cut -d: -f3 || true)"
  if [ -n "$existing" ]; then
    if [ "$existing" != "$gid" ]; then
      echo "group ${name} has gid ${existing}, expected ${gid}" >&2
      exit 1
    fi
    return
  fi
  if getent group "$gid" >/dev/null; then
    echo "gid ${gid} is already allocated before creating ${name}" >&2
    exit 1
  fi
  groupadd --gid "$gid" "$name"
}

ensure_user() {
  local name="$1"
  local uid="$2"
  local group="$3"
  local home="$4"
  local extra_args="${5:-}"
  local existing
  existing="$(id -u "$name" 2>/dev/null || true)"
  if [ -n "$existing" ]; then
    if [ "$existing" != "$uid" ]; then
      echo "user ${name} has uid ${existing}, expected ${uid}" >&2
      exit 1
    fi
    return
  fi
  if getent passwd "$uid" >/dev/null; then
    echo "uid ${uid} is already allocated before creating ${name}" >&2
    exit 1
  fi
  # shellcheck disable=SC2086
  useradd --uid "$uid" --gid "$group" $extra_args --home-dir "$home" --no-create-home --shell /usr/sbin/nologin "$name"
}

ensure_group trustyclaw-admin "$TRUSTYCLAW_ADMIN_GID"
ensure_group trustyclaw-proxy "$TRUSTYCLAW_PROXY_GID"
ensure_group trustyclaw-agent "$TRUSTYCLAW_AGENT_GID"
ensure_group cloudflared "$CLOUDFLARED_GID"
ensure_group trustyclaw-tools "$TRUSTYCLAW_TOOLS_GID"
ensure_group trustyclaw-agent-app "$TRUSTYCLAW_AGENT_APP_GID"
ensure_group trustyclaw-agent-network "$TRUSTYCLAW_AGENT_NETWORK_GID"
@APP_ENSURE_GROUPS@
ensure_user trustyclaw-admin "$TRUSTYCLAW_ADMIN_UID" trustyclaw-admin /mnt/trustyclaw-admin/admin-home
ensure_user trustyclaw-proxy "$TRUSTYCLAW_PROXY_UID" trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state
ensure_user trustyclaw-agent "$TRUSTYCLAW_AGENT_UID" trustyclaw-agent /mnt/trustyclaw-agent/agent-home
ensure_user cloudflared "$CLOUDFLARED_UID" cloudflared /nonexistent
# The tools service holds no durable state of its own (its state lives in the
# tool tables, reached with a scoped Postgres role), so it needs no home.
ensure_user trustyclaw-tools "$TRUSTYCLAW_TOOLS_UID" trustyclaw-tools /nonexistent
# The agent-app service derives authority from kernel-owned thread scopes and
# keeps no durable state of its own, so it also needs no home.
ensure_user trustyclaw-agent-app "$TRUSTYCLAW_AGENT_APP_UID" trustyclaw-agent-app /nonexistent
# The agent-network service serves read-only policy introspection with no
# filesystem state or egress.
ensure_user trustyclaw-agent-network "$TRUSTYCLAW_AGENT_NETWORK_UID" trustyclaw-agent-network /nonexistent
@APP_ENSURE_USERS@
# The postgres account is created here, before the postgresql packages would
# create it with a dynamic system uid: the preserved cluster files on the
# admin volume are 0600/0700 postgres-owned, so like the trustyclaw-* users
# the uid must stay stable across root-volume replacement. The Debian
# packaging reuses an existing postgres user as-is.
ensure_group postgres "$POSTGRES_GID"
ensure_user postgres "$POSTGRES_UID" postgres /var/lib/postgresql

# Sanitize managed paths on reused durable volumes before root writes anything
# there. Symlinks planted by a compromised previous service are removed;
# unexpected non-file/non-directory nodes fail the deploy.
PG_MAJOR="$PG_MAJOR" python3 - <<'PY'
import os
from pathlib import Path
import shutil
import stat


def ensure_directory(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True)
        return
    if stat.S_ISLNK(mode):
        os.unlink(path)
        path.mkdir(parents=True, exist_ok=True)
        return
    if not stat.S_ISDIR(mode):
        raise SystemExit(f"refusing to reuse non-directory managed path: {path}")


def ensure_regular_file_slot(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        os.unlink(path)
        return
    if not stat.S_ISREG(mode):
        raise SystemExit(f"refusing to reuse non-regular managed path: {path}")


def recreate_directory(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True)
        return
    if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)
    path.mkdir(parents=True, exist_ok=True)


admin_mount = Path("/mnt/trustyclaw-admin")
admin_state = admin_mount / "admin-state"
proxy_state = admin_mount / "proxy-state"
tools_state = admin_mount / "tools-state"
agent_home = Path("/mnt/trustyclaw-agent/agent-home")
pgdata = admin_mount / "postgres" / os.environ["PG_MAJOR"] / "main"
for directory in (
    # admin-state holds only the deploy-plane version.json; runtime admin
    # state lives in Postgres. Every component of the data directory path is
    # sanitized: the tree is owned by the postgres service user on a preserved
    # volume, so a compromised previous database process could otherwise plant
    # a symlink that a later root write follows.
    admin_state,
    admin_mount / "admin-home",
    admin_mount / "postgres",
    pgdata.parent,
    pgdata,
    proxy_state,
    tools_state,
    agent_home,
    agent_home / ".codex",
    agent_home / ".claude",
):
    ensure_directory(directory)
recreate_directory(proxy_state / "generated-certs")
recreate_directory(tools_state / "assets")

for path in (
    admin_state / "version.json",
    # Root rewrites these two managed files inside the postgres-owned data
    # directory on every deploy; the slots must not be symlinks.
    pgdata / "postgresql.conf",
    pgdata / "pg_hba.conf",
    proxy_state / "network_proxy_ca.key",
    proxy_state / "network_proxy_ca.crt",
    agent_home / "AGENTS.md",
    agent_home / "CLAUDE.md",
    agent_home / ".codex" / "config.toml",
    agent_home / ".claude" / "settings.json",
):
    ensure_regular_file_slot(path)
PY

# Enforce the deploy operation against the authoritative admin disk version.
# The EC2 tag is only a discovery hint; this check runs after the durable admin
# volume is mounted, before preserved state is modified.
python3 - <<'PY'
import json
import pathlib
import re
import sys

VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def parse_version(value: str) -> tuple[int, int, int]:
    if not VERSION_RE.fullmatch(value):
        fail(f"invalid TrustyClaw version {value!r}; expected MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in value.split("."))


def compare_versions(left: str, right: str) -> int:
    left_tuple = parse_version(left)
    right_tuple = parse_version(right)
    return (left_tuple > right_tuple) - (left_tuple < right_tuple)


payload = json.loads(pathlib.Path("/tmp/trustyclaw_payload.json").read_text())
operation = payload["operation"]
mode = operation["mode"]
target_version = operation["target_version"]
allow_upgrade = bool(operation.get("allow_upgrade"))
parse_version(target_version)

admin_state = pathlib.Path("/mnt/trustyclaw-admin/admin-state")
version_path = admin_state / "version.json"
config_path = admin_state / "config.json"
state_version = None
if version_path.exists():
    try:
        version_payload = json.loads(version_path.read_text())
    except json.JSONDecodeError as exc:
        fail(f"could not parse admin state version file {version_path}: {exc}")
    if not isinstance(version_payload, dict) or not isinstance(version_payload.get("version"), str):
        fail(f"admin state version file {version_path} must contain a string version field")
    state_version = version_payload["version"]
    parse_version(state_version)

if mode == "deploy":
    if state_version is not None or config_path.exists():
        fail("deploy requires empty admin state; use upgrade or recover for preserved state")
elif state_version is None:
    fail(f"{mode} requires existing admin state version file {version_path}")

if mode == "upgrade":
    comparison = compare_versions(state_version, target_version)
    if comparison >= 0:
        fail(
            f"upgrade requires admin state version lower than local VERSION; "
            f"state={state_version}, local={target_version}"
        )
elif mode == "recover":
    comparison = compare_versions(state_version, target_version)
    if allow_upgrade:
        if comparison > 0:
            fail(
                f"recover --allow-upgrade cannot move state backward; "
                f"state={state_version}, local={target_version}"
            )
    elif comparison != 0:
        fail(
            f"recover requires admin state version to match local VERSION; "
            f"state={state_version}, local={target_version}. Use recover --allow-upgrade to advance older state."
        )
elif mode == "reconfigure":
    comparison = compare_versions(state_version, target_version)
    if comparison != 0:
        fail(
            f"{mode} requires admin state version to match local VERSION; "
            f"state={state_version}, local={target_version}. Run upgrade first."
        )
elif mode != "deploy":
    fail(f"unknown deploy operation mode {mode!r}")

print(f"version check passed: mode={mode}, state={state_version or 'new'}, local={target_version}")
PY

# Install the Python runtime package copied by deploy. Runtime code is root
# owned but readable by service users.
mkdir -p /opt/trustyclaw-host
tar -xzf /tmp/trustyclaw-host-code.tar.gz -C /opt/trustyclaw-host
payload_value operation.target_version > /opt/trustyclaw-host/VERSION
# The script runs with umask 077; the runtime code must stay root-owned but
# readable by the service users that import it.
chown -R root:root /opt/trustyclaw-host
chmod -R a+rX /opt/trustyclaw-host
chmod 644 /opt/trustyclaw-host/VERSION

# Base OS packages and security updates.
echo "== installing system packages =="
# Node.js (and npm) come from the official tarball below, not apt: the Ubuntu
# npm package pulls in hundreds of node-* dependencies. -qq keeps the output to
# warnings and errors.
apt-get update -qq
apt-get install -y -qq ca-certificates curl gh git jq nftables openssl python3 python3-venv sudo unattended-upgrades xz-utils

# PostgreSQL for admin state. postgresql-common is installed first so its
# default-cluster creation can be disabled: the data directory must live on
# the durable admin volume (set up below), not on this replaceable root volume.
apt-get install -y -qq postgresql-common
sed -i 's/^#\?create_main_cluster.*/create_main_cluster = false/' /etc/postgresql-common/createcluster.conf
apt-get install -y -qq "postgresql-${PG_MAJOR}"
# The packaged umbrella unit only manages clusters registered with the Debian
# tooling; the TrustyClaw cluster runs under its own unit below.
systemctl disable --now postgresql.service >/dev/null 2>&1 || true

# Apply pending security updates now to close the window before the
# unattended-upgrades timer would run on its own. A new kernel takes effect on
# the next reboot; userspace (setuid binaries, libraries) is patched immediately.
echo "== applying security updates =="
unattended-upgrade || true

echo "== setting up admin-state PostgreSQL =="
PG_BIN="/usr/lib/postgresql/${PG_MAJOR}/bin"
install -d -o postgres -g postgres -m 700 "$(dirname "$PGDATA_DIR")" "$PGDATA_DIR"
chown root:root /mnt/trustyclaw-admin/postgres
chmod 711 /mnt/trustyclaw-admin/postgres
if [ ! -f "$PGDATA_DIR/PG_VERSION" ]; then
  runuser -u postgres -- "$PG_BIN/initdb" -D "$PGDATA_DIR" --auth-local=peer --auth-host=reject -E UTF8
fi

# Managed database config, rewritten on every deploy like the rest of the
# root-of-trust config. Unix-socket only: there is no TCP listener at all, and
# peer auth maps OS users to database roles, so access control is the host's
# user model. trustyclaw-admin (owner of the admin database), the scoped
# trustyclaw-proxy, trustyclaw-tools, and trustyclaw-agent-network roles, and the postgres superuser
# (operators, via sudo) can connect; every other user —
# including the agent user — matches only the final reject rule.
cat > "$PGDATA_DIR/postgresql.conf" <<PGCONF
# Managed by TrustyClaw bootstrap; rewritten on every deploy.
listen_addresses = ''
unix_socket_directories = '/var/run/postgresql'
# Each service process bounds its own active sessions client-side
# (db.MAX_ACTIVE_CONNECTIONS = 14). Provisioned once for growth rather than
# retuned per app: up to fifteen bundled apps plus the four core database
# clients use at most 19 x 14 = 266 sessions, leaving 34 slots for operator
# psql, the superuser reserve, and deployment work (test_deploy asserts the
# installed budget stays under this provision). Idle Postgres backends cost a
# few MB each, so the larger ceiling is cheap; bursts beyond a process's cap
# queue client-side instead of immediately failing at the server.
max_connections = 300
log_destination = 'stderr'
PGCONF
cat > "$PGDATA_DIR/pg_hba.conf" <<'PGHBA'
# Managed by TrustyClaw bootstrap; rewritten on every deploy.
# Unix-socket connections only; identity is the OS user (peer auth). Schema
# migrations give proxy, tools, and app roles only their required tables or
# schema. The agent user has no role and no rule that admits it.
local  trustyclaw_admin  trustyclaw-admin  peer
local  trustyclaw_admin  trustyclaw-proxy  peer
local  trustyclaw_admin  trustyclaw-tools  peer
local  trustyclaw_admin  trustyclaw-agent-network  peer
@APP_PG_HBA_LINES@
local  all               postgres          peer
local  all               all               reject
PGHBA
chown postgres:postgres "$PGDATA_DIR/postgresql.conf" "$PGDATA_DIR/pg_hba.conf"
chmod 600 "$PGDATA_DIR/postgresql.conf" "$PGDATA_DIR/pg_hba.conf"

cat > /etc/systemd/system/trustyclaw-postgres.service <<UNIT
[Unit]
Description=TrustyClaw Admin State PostgreSQL
After=local-fs.target
# Admin state is unreachable without it, and it is local-only, so a crash
# loop must keep retrying rather than hit the default start-limit.
StartLimitIntervalSec=0

[Service]
Type=notify
User=postgres
ExecStart=$PG_BIN/postgres -D $PGDATA_DIR
Restart=always
RestartSec=3
TimeoutStartSec=300
RuntimeDirectory=postgresql
RuntimeDirectoryPreserve=yes

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now trustyclaw-postgres.service
for attempt in $(seq 1 60); do
  if runuser -u postgres -- "$PG_BIN/pg_isready" -q; then
    break
  fi
  if [ "$attempt" = 60 ]; then
    echo "PostgreSQL did not become ready" >&2
    exit 1
  fi
  sleep 1
done

# Role and database, created once; both survive redeploys inside the data
# directory. The role name matches the service's Unix user so peer auth works.
runuser -u postgres -- psql -v ON_ERROR_STOP=1 --quiet <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'trustyclaw-admin') THEN
    CREATE ROLE "trustyclaw-admin" LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'trustyclaw-proxy') THEN
    CREATE ROLE "trustyclaw-proxy" LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'trustyclaw-tools') THEN
    CREATE ROLE "trustyclaw-tools" LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'trustyclaw-agent-network') THEN
    CREATE ROLE "trustyclaw-agent-network" LOGIN;
  END IF;
@APP_ROLE_SQL@
END
$$;
SQL
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname = 'trustyclaw_admin'" | grep -q 1; then
  runuser -u postgres -- createdb --owner=trustyclaw-admin trustyclaw_admin
fi
runuser -u postgres -- psql -d trustyclaw_admin -v ON_ERROR_STOP=1 --quiet \
  -c "REVOKE ALL ON DATABASE trustyclaw_admin FROM PUBLIC;" \
  -c "REVOKE CREATE ON SCHEMA public FROM PUBLIC;" \
  -c "GRANT CREATE ON SCHEMA public TO \"trustyclaw-admin\";" \
@APP_POSTGRES_SCHEMA_GRANTS@
  -c "GRANT CONNECT ON DATABASE trustyclaw_admin TO \"trustyclaw-proxy\";" \
  -c "GRANT CONNECT ON DATABASE trustyclaw_admin TO \"trustyclaw-tools\";" \
  -c "GRANT CONNECT ON DATABASE trustyclaw_admin TO \"trustyclaw-agent-network\";" \
@APP_POSTGRES_CONNECT_GRANTS@
# The PUBLIC revoke also stripped the proxy, tools, and agent-network roles'
# inherited CONNECT, so it is granted back explicitly; without it the proxy
# cannot log network decisions (and, being fail-closed, would fail every agent
# request), and the tools service cannot reach any tool state. PostgreSQL 14 ships the public schema
# creatable by every connecting role, so CREATE is revoked there too and
# granted back to exactly the schema-owning admin role: a compromised proxy,
# tools, or agent-network service can use only its granted tables, not mint new
# objects. The owning trustyclaw-admin role keeps its database privileges
# implicitly.

# Apply schema migrations, then compute and store the effective host config.
# Both run as trustyclaw-admin: migrations are owned by the same role the
# service uses, so the service never needs DDL rights it does not already
# have, and on upgrade/recover write_config carries the stored password and
# operator connections over from the existing config table. write_config echoes
# the effective config, which is staged root-only for the later bootstrap steps
# (SSH keys, cloudflared) that need it without database access.
echo "== migrating admin state schema =="
# The trustyclaw-tools role's table grants live in the schema migration
# (0007_tool_state.sql), the same pattern as the trustyclaw-proxy grants;
# bootstrap only provisions the role, its pg_hba line, and database CONNECT
# above, before migrations run.
runuser -u trustyclaw-admin -- env PYTHONPATH=/opt/trustyclaw-host python3 -m host.runtime.migrate up
echo "== migrating app schemas =="
@APP_MIGRATION_COMMANDS@
python3 - <<'PY' | runuser -u trustyclaw-admin -- env PYTHONPATH=/opt/trustyclaw-host python3 -m host.runtime.write_config > /tmp/trustyclaw_effective_config.json
import json, pathlib
payload = json.loads(pathlib.Path('/tmp/trustyclaw_payload.json').read_text())
print(json.dumps({'mode': payload['operation']['mode'], 'runtime_config': payload['runtime_config']}))
PY
chmod 600 /tmp/trustyclaw_effective_config.json

# Apply or remove persistent SSH operator access from the effective config.
# EC2 user data installs only the generated deploy key long enough for
# bootstrap to start.
python3 - <<'PY'
import json, pathlib

config = json.loads(pathlib.Path('/tmp/trustyclaw_effective_config.json').read_text())
ssh_keys = [
    connection['ssh_public_key']
    for connection in config['operator_connections']
    if connection.get('mode') == 'ssh'
]
ssh_dir = pathlib.Path('/home/trustyclaw-operator/.ssh')
ssh_dir.mkdir(parents=True, exist_ok=True)
authorized_keys = ssh_dir / 'authorized_keys'
if ssh_keys:
    authorized_keys.write_text('\n'.join(key.rstrip() for key in ssh_keys) + '\n')
else:
    authorized_keys.unlink(missing_ok=True)
PY
chown -R trustyclaw-operator:trustyclaw-operator /home/trustyclaw-operator/.ssh
chmod 700 /home/trustyclaw-operator/.ssh
if [ -f /home/trustyclaw-operator/.ssh/authorized_keys ]; then
  chmod 600 /home/trustyclaw-operator/.ssh/authorized_keys
fi

# Runtime CLIs used by the unprivileged agent user.
echo "== installing Node.js ${NODE_VERSION} =="
arch="$(dpkg --print-architecture)"
case "$arch" in
  amd64) node_arch=x64 ;;
  arm64) node_arch=arm64 ;;
  *) echo "unsupported architecture: ${arch}" >&2; exit 1 ;;
esac
curl -fsSLo /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"
tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner
rm -f /tmp/node.tar.xz

echo "== installing Codex CLI =="
npm install -g --no-fund --no-audit --loglevel=error "@openai/codex@${CODEX_CLI_VERSION}"
echo "== installing Claude Code CLI =="
npm install -g --no-fund --no-audit --loglevel=error "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"
# npm inherits the script's umask 077, which would leave the CLI root-only;
# the agent user must be able to run it.
chmod -R a+rX /usr/local/lib/node_modules

cloudflare_connection_count="$(python3 - <<'PY'
import json, pathlib
config = json.loads(pathlib.Path('/tmp/trustyclaw_effective_config.json').read_text())
print(sum(1 for connection in config['operator_connections'] if connection.get('mode') == 'cloudflare_access'))
PY
)"
if [ "$cloudflare_connection_count" -gt 0 ]; then
  echo "== installing cloudflared ${CLOUDFLARED_VERSION} =="
  case "$arch" in
    amd64) cloudflared_arch=amd64 ;;
    arm64) cloudflared_arch=arm64 ;;
    *) echo "unsupported cloudflared architecture: ${arch}" >&2; exit 1 ;;
  esac
  curl -fsSLo /usr/local/bin/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${cloudflared_arch}"
  chmod 755 /usr/local/bin/cloudflared
  cloudflared_version="$(/usr/local/bin/cloudflared --version)"
  case "$cloudflared_version" in
    *"${CLOUDFLARED_VERSION}"*) ;;
    *) echo "unexpected cloudflared version: ${cloudflared_version}" >&2; exit 1 ;;
  esac
  install -m 0750 -o root -g cloudflared -d /etc/trustyclaw
  python3 - <<'PY'
import json, pathlib
config = json.loads(pathlib.Path('/tmp/trustyclaw_effective_config.json').read_text())
connections = [
    connection
    for connection in config['operator_connections']
    if connection.get('mode') == 'cloudflare_access'
]
if len(connections) != 1:
    raise SystemExit(f'expected exactly one cloudflare_access connection, found {len(connections)}')
connection = connections[0]
pathlib.Path('/etc/trustyclaw/cloudflared.token').write_text(connection['tunnel_token'].strip() + '\n')
pathlib.Path('/etc/trustyclaw/cloudflare_hostname').write_text(connection['hostname'] + '\n')
PY
  chown root:cloudflared /etc/trustyclaw/cloudflared.token
  chown root:root /etc/trustyclaw/cloudflare_hostname
  chmod 640 /etc/trustyclaw/cloudflared.token
  chmod 644 /etc/trustyclaw/cloudflare_hostname
else
  rm -f /etc/systemd/system/trustyclaw-cloudflared.service /etc/trustyclaw/cloudflared.token /etc/trustyclaw/cloudflare_hostname
fi

# Managed Codex policy: restrict the agent to cached web search and disable
# Codex-hosted app/plugin/browse surfaces so the agent does not even attempt a
# tool the proxy would deny. `cached` is the only allowed web-search mode, which
# structurally excludes both `live` and `indexed` (server-approved external URL
# fetch) and keeps `open_page`/`find_in_page` reading OpenAI's index rather than
# fetching live — so no separate knob is needed for those. The network proxy is
# the ultimate layer: it denies any non-cached web/browse tool on OpenAI domains
# regardless of what the client requests.
mkdir -p /etc/codex
chmod 755 /etc/codex
cat > /etc/codex/requirements.toml <<'EOF'
allowed_web_search_modes = ["cached"]

[features]
apps = false
plugins = false
tool_search = false
tool_suggest = false
computer_use = false
remote_plugin = false
plugin_sharing = false
EOF
chmod 644 /etc/codex/requirements.toml

# Managed Codex config layer: the bundled tools surface. Codex spawns the
# MCP shim as trustyclaw-agent; the shim forwards to the tools service's
# socket, which authenticates the caller by kernel peer credentials.
# managed_config.toml is the documented root-owned system layer Codex loads
# alongside the agent-home config.
cat > /etc/codex/managed_config.toml <<'EOF'
[mcp_servers.trustyclaw]
command = "/usr/bin/python3"
args = ["-m", "host.runtime.tools_mcp_shim"]
env = { PYTHONPATH = "/opt/trustyclaw-host" }
EOF
chmod 644 /etc/codex/managed_config.toml

# Reduce the root-daemon attack surface reachable by the agent. snapd ships in
# the base image but is unused here, and its socket is world-accessible; stop
# and mask it so the agent has no snapd API to reach for privilege escalation.
systemctl disable --now snapd.socket snapd.service >/dev/null 2>&1 || true
systemctl mask snapd.socket snapd.service >/dev/null 2>&1 || true

# Root-volume swap is replaceable on redeploy, unlike admin/agent data.
if [ ! -f /swapfile ]; then
  fallocate -l 6G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Persistent proxy CA. The proxy user owns the private key after ownership is
# fixed below; the public CA is installed into the system trust store.
if [ ! -f "$PROXY_STATE_DIR/network_proxy_ca.key" ] || [ ! -f "$PROXY_STATE_DIR/network_proxy_ca.crt" ]; then
  openssl req -x509 -newkey rsa:4096 -nodes \
    -keyout "$PROXY_STATE_DIR/network_proxy_ca.key" \
    -out "$PROXY_STATE_DIR/network_proxy_ca.crt" \
    -days 3650 -subj "/CN=TrustyClaw Network Proxy"
fi
cp "$PROXY_STATE_DIR/network_proxy_ca.crt" /usr/local/share/ca-certificates/trustyclaw-network-proxy.crt
chmod 644 /usr/local/share/ca-certificates/trustyclaw-network-proxy.crt
update-ca-certificates

# Narrow root-owned helper scripts. Admin may invoke these exact paths through
# sudo, and helpers demote to agent/proxy users for runtime work.
mkdir -p /usr/local/lib/trustyclaw-host "$PROXY_STATE_DIR/generated-certs"
chmod 755 /usr/local/lib/trustyclaw-host
HELPER_SOURCE_DIR=/opt/trustyclaw-host/host/bootstrap/helpers
AGENT_HOME_SOURCE_DIR=/opt/trustyclaw-host/host/bootstrap/agent-home
HELPER_NAMES=(
  run-codex-app-server
  read-codex-account-id
  run-claude-code
  read-claude-account
  clear-agent-auth
  read-agent-file
  reboot-host
  check-for-upgrade
  mint-github-app-token
  audit-github-repo
  approve-github-push
)
for helper_name in "${HELPER_NAMES[@]}"; do
  sed "s/@""PROXY_PORT@/${PROXY_PORT}/g" \
    "$HELPER_SOURCE_DIR/${helper_name}.sh" \
    > "/usr/local/lib/trustyclaw-host/${helper_name}"
done
chown root:root /usr/local/lib/trustyclaw-host/*
chmod 755 /usr/local/lib/trustyclaw-host/*

# GitHub credentials are injected by the network proxy (the agent never
# holds the token), so the only client wiring is the gh shim: gh refuses to
# run authenticated calls without GH_TOKEN, so the shim supplies a fixed
# placeholder that the proxy strips and replaces. /usr/local/bin precedes
# /usr/bin on PATH, so the shim shadows the packaged gh. git needs no wiring
# at all — its unauthenticated requests are authenticated in transit.
sed "s/@""PROXY_PORT@/${PROXY_PORT}/g" "$HELPER_SOURCE_DIR/gh-shim.sh" > /usr/local/bin/gh
chown root:root /usr/local/bin/gh
chmod 755 /usr/local/bin/gh

# Final durable-volume ownership. Avoid recursive chown across preserved mutable
# trees; only directory roots and known managed files are adjusted.
chown root:root /mnt/trustyclaw-admin
chmod 711 /mnt/trustyclaw-admin
chown root:root /mnt/trustyclaw-agent
chmod 711 /mnt/trustyclaw-agent
chown trustyclaw-admin:trustyclaw-admin /mnt/trustyclaw-admin/admin-state
chmod 700 /mnt/trustyclaw-admin/admin-state
chown trustyclaw-admin:trustyclaw-admin /mnt/trustyclaw-admin/admin-home
chmod 700 /mnt/trustyclaw-admin/admin-home
chown trustyclaw-agent:trustyclaw-agent /mnt/trustyclaw-agent/agent-home
chmod 700 /mnt/trustyclaw-agent/agent-home
install -d -m 700 -o trustyclaw-agent -g trustyclaw-agent "$AGENT_HOME_PATH/.codex" "$AGENT_HOME_PATH/.claude"
chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state
chmod 700 /mnt/trustyclaw-admin/proxy-state
chown trustyclaw-tools:trustyclaw-tools /mnt/trustyclaw-admin/tools-state
chmod 700 /mnt/trustyclaw-admin/tools-state
chown trustyclaw-tools:trustyclaw-tools /mnt/trustyclaw-admin/tools-state/assets
chmod 700 /mnt/trustyclaw-admin/tools-state/assets
chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state/generated-certs
chmod 700 /mnt/trustyclaw-admin/proxy-state/generated-certs
chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.crt
chmod 600 /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key
chmod 644 /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.crt
# No initial network policy is seeded: a missing policy row is the fail-closed
# empty default (deny everything).

# Durable agent runtime config and instructions. The files live in this repo so
# harness expectations are reviewable, and bootstrap installs them root-owned,
# world-readable, and immutable so the agent can read but not alter them.
for managed_agent_file in \
  "$AGENT_HOME_PATH/AGENTS.md" \
  "$AGENT_HOME_PATH/CLAUDE.md" \
  "$AGENT_HOME_PATH/.codex/config.toml" \
  "$AGENT_HOME_PATH/.claude/settings.json"; do
  if [ -e "$managed_agent_file" ]; then
    chattr -f -i "$managed_agent_file" 2>/dev/null || true
  fi
done
install -m 0644 -o root -g root "$AGENT_HOME_SOURCE_DIR/agents_claude.md" "$AGENT_HOME_PATH/AGENTS.md"
install -m 0644 -o root -g root "$AGENT_HOME_SOURCE_DIR/agents_claude.md" "$AGENT_HOME_PATH/CLAUDE.md"
install -m 0644 -o root -g root "$AGENT_HOME_SOURCE_DIR/.codex/config.toml" "$AGENT_HOME_PATH/.codex/config.toml"
install -m 0644 -o root -g root "$AGENT_HOME_SOURCE_DIR/.claude/settings.json" "$AGENT_HOME_PATH/.claude/settings.json"
chattr +i \
  "$AGENT_HOME_PATH/AGENTS.md" \
  "$AGENT_HOME_PATH/CLAUDE.md" \
  "$AGENT_HOME_PATH/.codex/config.toml" \
  "$AGENT_HOME_PATH/.claude/settings.json"

cat > /etc/sudoers.d/trustyclaw-host <<'SUDOERS'
trustyclaw-admin ALL=(root) NOPASSWD: /usr/local/lib/trustyclaw-host/reboot-host, /usr/local/lib/trustyclaw-host/run-codex-app-server, /usr/local/lib/trustyclaw-host/read-codex-account-id, /usr/local/lib/trustyclaw-host/run-claude-code, /usr/local/lib/trustyclaw-host/read-claude-account, /usr/local/lib/trustyclaw-host/clear-agent-auth, /usr/local/lib/trustyclaw-host/read-agent-file, /usr/local/lib/trustyclaw-host/check-for-upgrade, /usr/local/lib/trustyclaw-host/mint-github-app-token, /usr/local/lib/trustyclaw-host/audit-github-repo, /usr/local/lib/trustyclaw-host/approve-github-push
SUDOERS
chmod 440 /etc/sudoers.d/trustyclaw-host

# Fail deploy now, not at first login, if the pinned CLIs are not executable by
# the agent user from its home directory.
codex_cli_version="$(runuser -u trustyclaw-agent -- env HOME=/mnt/trustyclaw-agent/agent-home \
  /usr/local/bin/codex --version)"
if [ "$codex_cli_version" != "codex-cli ${CODEX_CLI_VERSION}" ]; then
  echo "unexpected Codex CLI version: ${codex_cli_version}" >&2
  exit 1
fi
claude_code_version="$(runuser -u trustyclaw-agent -- env HOME=/mnt/trustyclaw-agent/agent-home CLAUDE_CONFIG_DIR=/mnt/trustyclaw-agent/agent-home/.claude \
  /usr/local/bin/claude --version)"
if [ "$claude_code_version" != "${CLAUDE_CODE_VERSION} (Claude Code)" ]; then
  echo "unexpected Claude Code version: ${claude_code_version}" >&2
  exit 1
fi

# Host firewall. Root, the dedicated proxy user, and the optional cloudflared
# connector can reach their narrow external dependencies; the agent can only
# reach the loopback proxy. DNS is denied for every other non-root user because
# DNS lookups are an exfiltration channel; the proxy may resolve and connect
# only after policy allows a host. The trustyclaw-tools service gets DNS and
# HTTPS because the bundled tool packages run inside it and call their
# third-party APIs directly; the admin service holds no internet egress at all,
# so a compromised tool package cannot exfiltrate admin state, and the agent's
# fail-closed proxy path is unaffected. The trustyclaw-agent-app service gets
# no egress rule at all: its only network reach is the per-app loopback port
# accepts generated below, so it can proxy agent calls to app backends and
# nothing else. The trustyclaw-agent-network service communicates only over
# Unix sockets; its explicit loopback drop prevents the local policy proxy
# from becoming an indirect egress path before the broad loopback accept.
python3 - <<'PY'
import json, pathlib
config = json.loads(pathlib.Path('/tmp/trustyclaw_effective_config.json').read_text())
ssh_enabled = any(connection.get('mode') == 'ssh' for connection in config['operator_connections'])
pathlib.Path('/tmp/trustyclaw_ssh_rule').write_text('    tcp dport 22 accept\n' if ssh_enabled else '')
cloudflare_enabled = any(connection.get('mode') == 'cloudflare_access' for connection in config['operator_connections'])
pathlib.Path('/tmp/trustyclaw_cloudflare_rules').write_text(
    '    meta skuid "cloudflared" udp dport 53 accept\n'
    '    meta skuid "cloudflared" tcp dport 53 accept\n'
    '    meta skuid "cloudflared" tcp dport { 443, 7844 } accept\n'
    '    meta skuid "cloudflared" udp dport 7844 accept\n'
    if cloudflare_enabled else ''
)
PY
cat > /etc/nftables.conf <<NFT
flush ruleset
table inet trustyclaw {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
$(cat /tmp/trustyclaw_ssh_rule)
  }
  chain output {
    type filter hook output priority 0; policy drop;
    meta skuid "systemd-resolve" udp dport 53 accept
    meta skuid "systemd-resolve" tcp dport 53 accept
    meta skuid "systemd-timesync" udp dport 123 accept
$(cat /tmp/trustyclaw_cloudflare_rules)
    meta skuid "trustyclaw-proxy" udp dport 53 accept
    meta skuid "trustyclaw-proxy" tcp dport 53 accept
    meta skuid "trustyclaw-proxy" tcp dport { 80, 443 } accept
    meta skuid "trustyclaw-tools" udp dport 53 accept
    meta skuid "trustyclaw-tools" tcp dport 53 accept
    meta skuid "trustyclaw-tools" tcp dport 443 accept
    udp dport 53 meta skuid != 0 drop
    tcp dport 53 meta skuid != 0 drop
    oif lo tcp dport @PROXY_PORT@ meta skuid "trustyclaw-agent" accept
    oif lo meta skuid "trustyclaw-agent" drop
@APP_NFTABLES_RULES@
    oif lo meta skuid "trustyclaw-agent-app" drop
    oif lo meta skuid "trustyclaw-agent-network" drop
    oif lo accept
    ct state established,related accept
    meta skuid 0 accept
  }
}
NFT
rm -f /tmp/trustyclaw_ssh_rule /tmp/trustyclaw_cloudflare_rules
systemctl enable --now nftables

# Systemd services.
# All agent runtime processes run in transient scopes under this slice (the
# run-* helpers launch them with systemd-run --scope), so the agent competes
# for resources as one cgroup instead of inside the admin API's service
# cgroup. The name deliberately uses an underscore: dashes in slice names
# encode nesting, and a nested slice's CPUWeight would be compared against
# its implicit parent's siblings, not against system.slice. As a top-level
# slice it is a direct sibling of system.slice (admin API, proxy, Postgres;
# default weight 100), so:
# - CPUWeight only matters under contention: an otherwise idle host still
#   gives the agent every core, but when host services need CPU they are
#   guaranteed about two thirds of it. A hard CPUQuota would waste idle
#   cores, so none is set.
# - MemoryHigh reclaims (to the swapfile) before MemoryMax OOM-kills inside
#   the agent cgroup, so a runaway agent build dies instead of triggering a
#   host-wide OOM kill that could take out Postgres or the proxy.
# - MemorySwapMax leaves 1G of the 6G root-volume swapfile (created above)
#   for host services. systemd 249 has no percentage form for swap limits,
#   but this script owns the swapfile size, so the absolute value cannot
#   drift independently.
# - TasksMax bounds agent threads+processes so a fork bomb cannot exhaust
#   kernel PIDs, which would stop the admin API from spawning the sudo
#   helpers (or any process) at all.
cat > /etc/systemd/system/trustyclaw_agent.slice <<'UNIT'
[Unit]
Description=TrustyClaw Agent Runtimes

[Slice]
CPUWeight=50
MemoryHigh=70%
MemoryMax=80%
MemorySwapMax=5G
TasksMax=4096
UNIT

# App backend services share one top-level slice. Like the agent slice, the
# underscore keeps this as a direct sibling of system.slice rather than a nested
# dash-encoded slice. CPUWeight is intentionally soft: apps may use idle cores,
# but under contention host services such as the admin API, proxy, and Postgres
# keep priority over app backend CPU loops.
cat > /etc/systemd/system/trustyclaw_app.slice <<'UNIT'
[Unit]
Description=TrustyClaw App Backends

[Slice]
CPUWeight=50
UNIT

cat > /etc/systemd/system/trustyclaw-network-proxy.service <<'UNIT'
[Unit]
Description=TrustyClaw Network Policy Proxy
After=network-online.target trustyclaw-postgres.service
Wants=network-online.target trustyclaw-postgres.service
# Never give up restarting: the proxy is the agent's only egress path and is
# fail-closed, so a crash loop must keep retrying rather than hit the default
# start-limit and stay dead.
StartLimitIntervalSec=0

[Service]
User=trustyclaw-proxy
UMask=0077
Environment=PYTHONPATH=/opt/trustyclaw-host
ExecStart=/usr/bin/python3 -m host.runtime.network_proxy
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

# The dedicated tools service runs tool code and holds internet egress out of
# the admin service. It owns the agent-facing tools socket and connects to
# Postgres as the scoped trustyclaw-tools role. RuntimeDirectory stays
# world-traversable (0755) so the agent (and admin, for delegated operator
# operations) can connect; the socket peer-credential check is the authentication.
cat > /etc/systemd/system/trustyclaw-tools.service <<'UNIT'
[Unit]
Description=TrustyClaw Tools Service
After=network-online.target trustyclaw-postgres.service
Wants=network-online.target trustyclaw-postgres.service
StartLimitIntervalSec=0

[Service]
User=trustyclaw-tools
UMask=0077
RuntimeDirectory=trustyclaw-tools
RuntimeDirectoryMode=0755
Environment=PYTHONPATH=/opt/trustyclaw-host
ExecStart=/usr/bin/python3 -m host.runtime.tools_service
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

# Read-only agent network introspection is isolated from both the egress-capable
# tools service and the privileged proxy. Its database role can read only the
# policy and network-event tables; nftables grants this uid no egress.
cat > /etc/systemd/system/trustyclaw-agent-network.service <<'UNIT'
[Unit]
Description=TrustyClaw Agent Network Introspection
After=trustyclaw-postgres.service
Wants=trustyclaw-postgres.service
StartLimitIntervalSec=0

[Service]
User=trustyclaw-agent-network
UMask=0077
RuntimeDirectory=trustyclaw-agent-network
RuntimeDirectoryMode=0755
Environment=PYTHONPATH=/opt/trustyclaw-host
ExecStart=/usr/bin/python3 -m host.runtime.network_introspection_service
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

# The dedicated agent-app service proxies agent app_api calls to app backend
# ports (the one uid besides trustyclaw-admin that nftables allows to open new
# connections to them). It derives app ownership from the caller's trusted
# thread scope and needs no database access.
# RuntimeDirectory stays world-traversable (0755) so the agent can connect;
# the socket peer-credential check plus cgroup thread attribution are the
# authentication.
cat > /etc/systemd/system/trustyclaw-agent-app.service <<'UNIT'
[Unit]
Description=TrustyClaw Agent App Service
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
User=trustyclaw-agent-app
UMask=0077
RuntimeDirectory=trustyclaw-agent-app
RuntimeDirectoryMode=0755
Environment=PYTHONPATH=/opt/trustyclaw-host
ExecStart=/usr/bin/python3 -m host.runtime.agent_app_service
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/trustyclaw-admin-api.service <<'UNIT'
[Unit]
Description=TrustyClaw Admin API
After=network-online.target trustyclaw-network-proxy.service trustyclaw-postgres.service trustyclaw-tools.service trustyclaw-agent-network.service trustyclaw-agent-app.service
Wants=network-online.target trustyclaw-network-proxy.service trustyclaw-postgres.service trustyclaw-tools.service trustyclaw-agent-network.service trustyclaw-agent-app.service
StartLimitIntervalSec=0

[Service]
User=trustyclaw-admin
UMask=0077
# trustyclaw-admin-api holds the app-backend admin socket; the agent-facing
# tools socket is owned by trustyclaw-tools.service. The directory stays
# world-traversable so the app users can connect, and the socket peer
# credentials authenticate the caller.
RuntimeDirectory=trustyclaw-admin-api
RuntimeDirectoryMode=0755
Environment=PYTHONPATH=/opt/trustyclaw-host
Environment=HOME=/mnt/trustyclaw-admin/admin-home
WorkingDirectory=/mnt/trustyclaw-admin/admin-home
ExecStart=/usr/bin/python3 -m host.runtime.admin_api
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

@APP_SYSTEMD_UNITS@

if [ "$cloudflare_connection_count" -gt 0 ]; then
  cat > /etc/systemd/system/trustyclaw-cloudflared.service <<'UNIT'
[Unit]
Description=TrustyClaw Cloudflare Tunnel
After=network-online.target trustyclaw-admin-api.service
Wants=network-online.target trustyclaw-admin-api.service
StartLimitIntervalSec=0

[Service]
User=cloudflared
ExecStart=/usr/local/bin/cloudflared tunnel --no-autoupdate run --token-file /etc/trustyclaw/cloudflared.token
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
fi

systemctl daemon-reload
systemctl enable --now trustyclaw-network-proxy.service
systemctl enable --now trustyclaw-tools.service
systemctl enable --now trustyclaw-agent-network.service
systemctl enable --now trustyclaw-agent-app.service
systemctl enable --now trustyclaw-admin-api.service
if [ "$cloudflare_connection_count" -gt 0 ]; then
  systemctl enable --now trustyclaw-cloudflared.service
  for attempt in $(seq 1 30); do
    if systemctl is-active --quiet trustyclaw-cloudflared.service; then
      break
    fi
    sleep 2
  done
  if ! systemctl is-active --quiet trustyclaw-cloudflared.service; then
    journalctl -u trustyclaw-cloudflared.service --no-pager -n 80 >&2 || true
    echo "cloudflared service did not become active" >&2
    exit 1
  fi
  cloudflare_hostname="$(cat /etc/trustyclaw/cloudflare_hostname)"
  cloudflare_status=""
  for attempt in $(seq 1 30); do
    cloudflare_status="$(curl -sS -o /tmp/trustyclaw_cloudflare_probe -w '%{http_code}' --max-time 10 "https://${cloudflare_hostname}/v1/health" || true)"
    case "$cloudflare_status" in
      302|403)
        break
        ;;
    esac
    sleep 5
  done
  case "$cloudflare_status" in
    302|403)
      echo "Cloudflare Access probe for ${cloudflare_hostname} returned ${cloudflare_status}"
      ;;
    401)
      echo "Cloudflare hostname ${cloudflare_hostname} reached the admin API without Cloudflare Access protection" >&2
      exit 1
      ;;
    *)
      echo "Cloudflare hostname ${cloudflare_hostname} did not return an Access login/deny response; last status: ${cloudflare_status:-none}" >&2
      echo "Check that the tunnel public hostname points to http://localhost:@ADMIN_PORT@ and has a Cloudflare Access policy." >&2
      exit 1
      ;;
  esac
fi

# Provisioning is almost done: capture the non-secret target version and emit
# only the non-secret access facts the lifecycle CLI needs for AWS security group
# cleanup, then drop the single-use deploy key and staged files before starting
# arbitrary app service code.
target_version="$(payload_value operation.target_version)"
python3 - <<'PY'
import json, pathlib

config = json.loads(pathlib.Path('/tmp/trustyclaw_effective_config.json').read_text())
connections = config['operator_connections']
print('TRUSTYCLAW_BOOTSTRAP_ACCESS_SUMMARY ' + json.dumps({
    'ssh_enabled': any(connection.get('mode') == 'ssh' for connection in connections),
    'cloudflare_enabled': any(connection.get('mode') == 'cloudflare_access' for connection in connections),
}, sort_keys=True))
PY
rm -f /home/trustyclaw-operator/.ssh/authorized_keys2
rm -f /tmp/trustyclaw_payload.json /tmp/trustyclaw_effective_config.json /tmp/trustyclaw-host-code.tar.gz /tmp/trustyclaw_bootstrap.sh
@APP_ENABLE_START_COMMANDS@

# The admin disk version is authoritative for preserved state. Advance it only
# after the root-volume install and service setup have succeeded.
TRUSTYCLAW_TARGET_VERSION="$target_version" python3 - <<'PY'
import json, os, pathlib, time

version_path = pathlib.Path('/mnt/trustyclaw-admin/admin-state/version.json')
version_path.write_text(json.dumps({
    'version': os.environ['TRUSTYCLAW_TARGET_VERSION'],
    'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
}, indent=2, sort_keys=True) + '\n')
PY
chown trustyclaw-admin:trustyclaw-admin /mnt/trustyclaw-admin/admin-state/version.json
chmod 600 /mnt/trustyclaw-admin/admin-state/version.json
