#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
umask 077
NODE_VERSION=22.12.0
CODEX_CLI_VERSION=0.140.0
CLAUDE_CODE_VERSION=2.1.177
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
PROXY_PORT=@PROXY_PORT@

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
ensure_user trustyclaw-admin "$TRUSTYCLAW_ADMIN_UID" trustyclaw-admin /mnt/trustyclaw-admin/admin-home
ensure_user trustyclaw-proxy "$TRUSTYCLAW_PROXY_UID" trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state
ensure_user trustyclaw-agent "$TRUSTYCLAW_AGENT_UID" trustyclaw-agent /mnt/trustyclaw-agent/agent-home
ensure_user cloudflared "$CLOUDFLARED_UID" cloudflared /nonexistent
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
pgdata = admin_mount / "postgres" / os.environ["PG_MAJOR"] / "main"
for directory in (
    # admin-state holds only the deploy-plane version.json now; runtime admin
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
    Path("/mnt/trustyclaw-agent/agent-home"),
):
    ensure_directory(directory)
recreate_directory(proxy_state / "generated-certs")

for path in (
    admin_state / "version.json",
    # Root rewrites these two managed files inside the postgres-owned data
    # directory on every deploy; the slots must not be symlinks.
    pgdata / "postgresql.conf",
    pgdata / "pg_hba.conf",
    proxy_state / "network_proxy_ca.key",
    proxy_state / "network_proxy_ca.crt",
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

# Admin state moved from JSON files into Postgres in 0.5.0, deliberately
# without a data migration. Refuse older preserved state here — before
# anything is modified — rather than failing midway when the carried-over
# credentials turn out to live in the legacy config.json.
MIN_STATE_VERSION = "0.5.0"
if mode != "deploy" and compare_versions(state_version, MIN_STATE_VERSION) < 0:
    fail(
        f"admin state version {state_version} predates the Postgres storage "
        f"introduced in {MIN_STATE_VERSION} and cannot be upgraded in place; "
        "deploy a fresh host (the preserved volume is untouched and still "
        "recoverable with matching older code)"
    )

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
apt-get install -y -qq brotli ca-certificates curl jq nftables openssl python3 python3-venv sudo unattended-upgrades xz-utils zstd

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
# user model. Only trustyclaw-admin (owner of the admin database) and the
# postgres superuser (operators, via sudo) can connect; every other user —
# including the agent user — matches only the final reject rule.
cat > "$PGDATA_DIR/postgresql.conf" <<PGCONF
# Managed by TrustyClaw bootstrap; rewritten on every deploy.
listen_addresses = ''
unix_socket_directories = '/var/run/postgresql'
# Each service process bounds its own active sessions client-side
# (db.MAX_ACTIVE_CONNECTIONS = 18), so admin + proxy stay well below this cap
# and bursts queue for milliseconds in the services instead of failing at the
# server; the remainder is operator psql headroom.
max_connections = 50
log_destination = 'stderr'
PGCONF
cat > "$PGDATA_DIR/pg_hba.conf" <<'PGHBA'
# Managed by TrustyClaw bootstrap; rewritten on every deploy.
# Unix-socket connections only; identity is the OS user (peer auth). The
# proxy role is granted exactly the network_events table (see the schema
# migration); the agent user has no role and no rule that admits it.
local  trustyclaw_admin  trustyclaw-admin  peer
local  trustyclaw_admin  trustyclaw-proxy  peer
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
  -c "GRANT CONNECT ON DATABASE trustyclaw_admin TO \"trustyclaw-proxy\";"
# The PUBLIC revoke also stripped the proxy role's inherited CONNECT, so it is
# granted back explicitly; without it the proxy cannot log network decisions
# and, being fail-closed, would fail every agent request. PostgreSQL 14 ships
# the public schema creatable by every connecting role, so CREATE is revoked
# there too and granted back to exactly the schema-owning admin role: a
# compromised proxy can use only its granted tables, not mint new objects.
# The owning trustyclaw-admin role keeps its database privileges implicitly.

# Apply schema migrations, then compute and store the effective host config.
# Both run as trustyclaw-admin: migrations are owned by the same role the
# service uses, so the service never needs DDL rights it does not already
# have, and on upgrade/recover write_config carries the stored password and
# operator connections over from the existing config table. The subshell cd
# keeps Python from stumbling over a working directory the service user cannot
# read (runuser preserves the caller's cwd). write_config echoes the effective
# config, which is staged root-only for the later bootstrap steps (SSH keys,
# cloudflared) that need it without database access.
echo "== migrating admin state schema =="
(cd / && runuser -u trustyclaw-admin -- env PYTHONPATH=/opt/trustyclaw-host python3 -m host.runtime.migrate up)
python3 - <<'PY' | (cd / && runuser -u trustyclaw-admin -- env PYTHONPATH=/opt/trustyclaw-host python3 -m host.runtime.write_config) > /tmp/trustyclaw_effective_config.json
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

# Managed Codex policy: restrict the agent to cached web search, so it never
# performs live web fetches. The network proxy is the second layer (it denies
# live web_search payloads on the OpenAI domains).
mkdir -p /etc/codex
chmod 755 /etc/codex
cat > /etc/codex/requirements.toml <<'EOF'
allowed_web_search_modes = ["cached"]
EOF
chmod 644 /etc/codex/requirements.toml

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
HELPER_NAMES=(
  run-codex-app-server
  read-codex-account-id
  run-claude-code
  read-claude-account
  read-agent-file
  reboot-host
)
for helper_name in "${HELPER_NAMES[@]}"; do
  sed "s/@""PROXY_PORT@/${PROXY_PORT}/g" \
    "$HELPER_SOURCE_DIR/${helper_name}.sh" \
    > "/usr/local/lib/trustyclaw-host/${helper_name}"
done
chown root:root /usr/local/lib/trustyclaw-host/run-codex-app-server /usr/local/lib/trustyclaw-host/read-codex-account-id /usr/local/lib/trustyclaw-host/run-claude-code /usr/local/lib/trustyclaw-host/read-claude-account /usr/local/lib/trustyclaw-host/read-agent-file /usr/local/lib/trustyclaw-host/reboot-host
chmod 755 /usr/local/lib/trustyclaw-host/run-codex-app-server /usr/local/lib/trustyclaw-host/read-codex-account-id /usr/local/lib/trustyclaw-host/run-claude-code /usr/local/lib/trustyclaw-host/read-claude-account /usr/local/lib/trustyclaw-host/read-agent-file /usr/local/lib/trustyclaw-host/reboot-host

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
chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state
chmod 700 /mnt/trustyclaw-admin/proxy-state
chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state/generated-certs
chmod 700 /mnt/trustyclaw-admin/proxy-state/generated-certs
chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.crt
chmod 600 /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key
chmod 644 /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.crt
# No initial network policy is seeded: a missing policy row is the fail-closed
# empty default (deny everything), which is exactly what the old seeded empty
# policy expressed.

cat > /etc/sudoers.d/trustyclaw-host <<'SUDOERS'
trustyclaw-admin ALL=(root) NOPASSWD: /usr/local/lib/trustyclaw-host/reboot-host, /usr/local/lib/trustyclaw-host/run-codex-app-server, /usr/local/lib/trustyclaw-host/read-codex-account-id, /usr/local/lib/trustyclaw-host/run-claude-code, /usr/local/lib/trustyclaw-host/read-claude-account, /usr/local/lib/trustyclaw-host/read-agent-file
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
# only after policy allows a host.
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
    udp dport 53 meta skuid != 0 drop
    tcp dport 53 meta skuid != 0 drop
    oif lo tcp dport @PROXY_PORT@ meta skuid "trustyclaw-agent" accept
    oif lo meta skuid "trustyclaw-agent" drop
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

cat > /etc/systemd/system/trustyclaw-admin-api.service <<'UNIT'
[Unit]
Description=TrustyClaw Admin API
After=network-online.target trustyclaw-network-proxy.service trustyclaw-postgres.service
Wants=network-online.target trustyclaw-network-proxy.service trustyclaw-postgres.service
StartLimitIntervalSec=0

[Service]
User=trustyclaw-admin
UMask=0077
Environment=PYTHONPATH=/opt/trustyclaw-host
Environment=HOME=/mnt/trustyclaw-admin/admin-home
WorkingDirectory=/mnt/trustyclaw-admin/admin-home
ExecStart=/usr/bin/python3 -m host.runtime.admin_api
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

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

# The admin disk version is authoritative for preserved state. Advance it only
# after the root-volume install and service setup have succeeded.
python3 - <<'PY'
import json, pathlib, time

payload = json.loads(pathlib.Path('/tmp/trustyclaw_payload.json').read_text())
version_path = pathlib.Path('/mnt/trustyclaw-admin/admin-state/version.json')
version_path.write_text(json.dumps({
    'version': payload['operation']['target_version'],
    'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
}, indent=2, sort_keys=True) + '\n')
PY
chown trustyclaw-admin:trustyclaw-admin /mnt/trustyclaw-admin/admin-state/version.json
chmod 600 /mnt/trustyclaw-admin/admin-state/version.json

# Provisioning is done: emit only the non-secret access facts the lifecycle CLI
# needs for AWS security group cleanup, then drop the single-use deploy key and
# staged files.
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
