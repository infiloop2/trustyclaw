#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
umask 077
NODE_VERSION=22.12.0
CODEX_CLI_VERSION=0.140.0
CLAUDE_CODE_VERSION=2.1.177
TRUSTYCLAW_ADMIN_UID=47741
TRUSTYCLAW_ADMIN_GID=47741
TRUSTYCLAW_PROXY_UID=47742
TRUSTYCLAW_PROXY_GID=47742
TRUSTYCLAW_AGENT_UID=47743
TRUSTYCLAW_AGENT_GID=47743
PROXY_PORT=@PROXY_PORT@

# Persistent volume layout. The admin volume is durable across redeploys, so
# admin-owned mutable state and proxy-owned mutable state live in separate
# directories with separate Unix owners.
ADMIN_MOUNT=/mnt/trustyclaw-admin
ADMIN_STATE_DIR=/mnt/trustyclaw-admin/admin-state
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
ensure_user trustyclaw-admin "$TRUSTYCLAW_ADMIN_UID" trustyclaw-admin /mnt/trustyclaw-admin/admin-home
ensure_user trustyclaw-proxy "$TRUSTYCLAW_PROXY_UID" trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state
ensure_user trustyclaw-agent "$TRUSTYCLAW_AGENT_UID" trustyclaw-agent /mnt/trustyclaw-agent/agent-home

# Sanitize managed paths on reused durable volumes before root writes anything
# there. Symlinks planted by a compromised previous service are removed;
# unexpected non-file/non-directory nodes fail the deploy.
python3 - <<'PY'
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
for directory in (
    admin_state,
    admin_mount / "admin-home",
    proxy_state,
    Path("/mnt/trustyclaw-agent/agent-home"),
):
    ensure_directory(directory)
recreate_directory(proxy_state / "generated-certs")

for path in (
    admin_state / "config.json",
    admin_state / "state.json",
    admin_state / "openai_account.json",
    admin_state / "claude_account.json",
    admin_state / "events.jsonl",
    proxy_state / "openai_account.json",
    proxy_state / "claude_account.json",
    proxy_state / "network_events.jsonl",
    proxy_state / "network_controls.json",
    proxy_state / ".network_policy.lock",
    proxy_state / "network_proxy_ca.key",
    proxy_state / "network_proxy_ca.crt",
):
    ensure_regular_file_slot(path)
PY

# Install the Python runtime package copied by deploy. Runtime code is root
# owned but readable by service users.
mkdir -p /opt/trustyclaw-host
tar -xzf /tmp/trustyclaw-host-code.tar.gz -C /opt/trustyclaw-host
# The script runs with umask 077; the runtime code must stay root-owned but
# readable by the service users that import it.
chown -R root:root /opt/trustyclaw-host
chmod -R a+rX /opt/trustyclaw-host

# Seed admin/proxy state files. Config is refreshed on every deploy; other
# files are created only if missing so task history and account pins persist.
python3 - <<'PY'
import json, pathlib
payload = json.loads(pathlib.Path('/tmp/trustyclaw_payload.json').read_text())
admin_state = pathlib.Path('/mnt/trustyclaw-admin/admin-state')
proxy_state = pathlib.Path('/mnt/trustyclaw-admin/proxy-state')
(admin_state / 'config.json').write_text(json.dumps(payload['runtime_config'], indent=2, sort_keys=True) + '\n')
initial_files = {
    'state.json': '{}\n',
    'openai_account.json': '{"account_id": null}\n',
    'claude_account.json': '{}\n',
    'events.jsonl': '',
}
for name, content in initial_files.items():
    path = admin_state / name
    if not path.exists():
        path.write_text(content)
proxy_pin_files = {
    'openai_account.json': '{"account_id": null}\n',
    'claude_account.json': '{}\n',
}
for name, content in proxy_pin_files.items():
    path = proxy_state / name
    if not path.exists():
        path.write_text(content)
network_events = proxy_state / 'network_events.jsonl'
if not network_events.exists():
    network_events.write_text('')
PY

# Base OS packages and security updates.
echo "== installing system packages =="
# Node.js (and npm) come from the official tarball below, not apt: the Ubuntu
# npm package pulls in hundreds of node-* dependencies. -qq keeps the output to
# warnings and errors.
apt-get update -qq
apt-get install -y -qq brotli ca-certificates curl jq nftables openssl python3 python3-venv sudo unattended-upgrades xz-utils zstd

# Apply pending security updates now to close the window before the
# unattended-upgrades timer would run on its own. A new kernel takes effect on
# the next reboot; userspace (setuid binaries, libraries) is patched immediately.
echo "== applying security updates =="
unattended-upgrade || true

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
  reboot-host
  update-network-policy
  read-network-state
  update-provider-account
)
for helper_name in "${HELPER_NAMES[@]}"; do
  sed "s/@""PROXY_PORT@/${PROXY_PORT}/g" \
    "$HELPER_SOURCE_DIR/${helper_name}.sh" \
    > "/usr/local/lib/trustyclaw-host/${helper_name}"
done
chown root:root /usr/local/lib/trustyclaw-host/run-codex-app-server /usr/local/lib/trustyclaw-host/read-codex-account-id /usr/local/lib/trustyclaw-host/run-claude-code /usr/local/lib/trustyclaw-host/read-claude-account /usr/local/lib/trustyclaw-host/reboot-host /usr/local/lib/trustyclaw-host/update-network-policy /usr/local/lib/trustyclaw-host/read-network-state /usr/local/lib/trustyclaw-host/update-provider-account
chmod 755 /usr/local/lib/trustyclaw-host/run-codex-app-server /usr/local/lib/trustyclaw-host/read-codex-account-id /usr/local/lib/trustyclaw-host/run-claude-code /usr/local/lib/trustyclaw-host/read-claude-account /usr/local/lib/trustyclaw-host/reboot-host /usr/local/lib/trustyclaw-host/update-network-policy /usr/local/lib/trustyclaw-host/read-network-state /usr/local/lib/trustyclaw-host/update-provider-account

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
touch /mnt/trustyclaw-admin/proxy-state/.network_policy.lock
chown trustyclaw-admin:trustyclaw-admin /mnt/trustyclaw-admin/admin-state/config.json /mnt/trustyclaw-admin/admin-state/state.json /mnt/trustyclaw-admin/admin-state/openai_account.json /mnt/trustyclaw-admin/admin-state/claude_account.json /mnt/trustyclaw-admin/admin-state/events.jsonl
chmod 600 /mnt/trustyclaw-admin/admin-state/config.json /mnt/trustyclaw-admin/admin-state/state.json /mnt/trustyclaw-admin/admin-state/openai_account.json /mnt/trustyclaw-admin/admin-state/claude_account.json /mnt/trustyclaw-admin/admin-state/events.jsonl
chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state/.network_policy.lock /mnt/trustyclaw-admin/proxy-state/network_events.jsonl /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.crt /mnt/trustyclaw-admin/proxy-state/openai_account.json /mnt/trustyclaw-admin/proxy-state/claude_account.json
chmod 600 /mnt/trustyclaw-admin/proxy-state/.network_policy.lock /mnt/trustyclaw-admin/proxy-state/network_events.jsonl /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.key /mnt/trustyclaw-admin/proxy-state/openai_account.json /mnt/trustyclaw-admin/proxy-state/claude_account.json
chmod 644 /mnt/trustyclaw-admin/proxy-state/network_proxy_ca.crt

if [ -f /mnt/trustyclaw-admin/proxy-state/network_controls.json ]; then
  chown trustyclaw-proxy:trustyclaw-proxy /mnt/trustyclaw-admin/proxy-state/network_controls.json
  chmod 600 /mnt/trustyclaw-admin/proxy-state/network_controls.json
else
  cat > /tmp/trustyclaw_initial_policy.json <<'JSON'
{
  "managed_ai_provider_network_access": {},
  "allowed_network_access": {}
}
JSON
  /usr/local/lib/trustyclaw-host/update-network-policy < /tmp/trustyclaw_initial_policy.json >/dev/null
  rm -f /tmp/trustyclaw_initial_policy.json
fi

cat > /etc/sudoers.d/trustyclaw-host <<'SUDOERS'
trustyclaw-admin ALL=(root) NOPASSWD: /usr/local/lib/trustyclaw-host/reboot-host, /usr/local/lib/trustyclaw-host/run-codex-app-server, /usr/local/lib/trustyclaw-host/read-codex-account-id, /usr/local/lib/trustyclaw-host/run-claude-code, /usr/local/lib/trustyclaw-host/read-claude-account, /usr/local/lib/trustyclaw-host/update-network-policy, /usr/local/lib/trustyclaw-host/read-network-state, /usr/local/lib/trustyclaw-host/update-provider-account
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

# Host firewall. Root and the dedicated proxy user can reach external networks;
# the agent can only reach the loopback proxy. DNS is denied for every other
# non-root user because DNS lookups are an exfiltration channel; the proxy may
# resolve and connect only after policy allows a host.
cat > /etc/nftables.conf <<'NFT'
flush ruleset
table inet trustyclaw {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    @SSH_RULE@
  }
  chain output {
    type filter hook output priority 0; policy drop;
    meta skuid "systemd-resolve" udp dport 53 accept
    meta skuid "systemd-resolve" tcp dport 53 accept
    meta skuid "systemd-timesync" udp dport 123 accept
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
systemctl enable --now nftables

# Systemd services.
cat > /etc/systemd/system/trustyclaw-network-proxy.service <<'UNIT'
[Unit]
Description=TrustyClaw Network Policy Proxy
After=network-online.target
Wants=network-online.target
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
After=network-online.target trustyclaw-network-proxy.service
Wants=network-online.target trustyclaw-network-proxy.service
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

systemctl daemon-reload
systemctl enable --now trustyclaw-network-proxy.service
systemctl enable --now trustyclaw-admin-api.service

# Provisioning is done: drop the single-use deploy key and the staged files.
rm -f /home/trustyclaw-operator/.ssh/authorized_keys2
rm -f /tmp/trustyclaw_payload.json /tmp/trustyclaw-host-code.tar.gz /tmp/trustyclaw_bootstrap.sh
