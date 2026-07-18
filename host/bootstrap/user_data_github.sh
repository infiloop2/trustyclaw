#!/usr/bin/env bash
# GitHub delivery: single-stage EC2 user data. Cloud-init runs this as root at
# first boot. It hardens the base accounts, stages the provisioning payload,
# fetches the pinned public commit, and hands off to
# host.bootstrap.self_provision, which renders and runs the same bootstrap the
# SSH delivery pushes. No deploy key exists in this mode; port 22 stays closed
# unless the stored operator connections include an ssh endpoint.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
umask 077

# Failed provisioning leaves no instance, same as the SSH delivery where the
# CLI terminates it: instances launch with instance-initiated shutdown
# behavior set to terminate, so shutting down on any failure terminates this
# instance and deletes its root volume. Attached data volumes survive.
on_exit() {
  code=$?
  if [ "$code" != 0 ]; then
    echo "TrustyClaw provisioning failed (exit $code); shutting down to terminate this instance" >&2
    shutdown -h now
  fi
}
trap on_exit EXIT

id -u trustyclaw-operator >/dev/null 2>&1 || useradd --create-home --shell /bin/bash trustyclaw-operator
echo 'trustyclaw-operator ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/trustyclaw-operator
chmod 440 /etc/sudoers.d/trustyclaw-operator
gpasswd -d ubuntu sudo >/dev/null 2>&1 || true
rm -f /etc/sudoers.d/90-cloud-init-users

cat > /tmp/trustyclaw_payload.json <<'TRUSTYCLAW_PAYLOAD_EOF'
@PAYLOAD_JSON@
TRUSTYCLAW_PAYLOAD_EOF
chmod 600 /tmp/trustyclaw_payload.json

# The lifecycle CLI already proved the pinned commit exists and is readable
# before launching this instance, so failures below are transient network or
# GitHub availability issues. Retry for an extended window (roughly half an
# hour each) so an outage delays provisioning instead of failing it.
for attempt in $(seq 1 60); do
  if apt-get update -q && apt-get install -y -q git; then
    break
  fi
  if [ "$attempt" = 60 ]; then
    echo "could not install git for TrustyClaw provisioning" >&2
    exit 1
  fi
  sleep 20
done

rm -rf /tmp/trustyclaw-checkout
git init -q /tmp/trustyclaw-checkout
cd /tmp/trustyclaw-checkout
git remote add origin 'https://github.com/@GITHUB_REPOSITORY@.git'
for attempt in $(seq 1 60); do
  if git fetch -q --depth 1 origin '@COMMIT_SHA@'; then
    break
  fi
  if [ "$attempt" = 60 ]; then
    echo "could not fetch pinned TrustyClaw commit @COMMIT_SHA@" >&2
    exit 1
  fi
  sleep 30
done
git checkout -q --detach FETCH_HEAD

PYTHONPATH=/tmp/trustyclaw-checkout python3 -m host.bootstrap.self_provision \
  --payload /tmp/trustyclaw_payload.json \
  --checkout /tmp/trustyclaw-checkout
