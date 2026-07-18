#!/usr/bin/env bash
# SSH delivery, stage 1 EC2 user data: hardens the base accounts, installs the
# single-use deploy key, and stages the provisioning payload. Stage 2 pushes
# the runtime code and bootstrap script over SSH and runs bootstrap, which
# reads the payload staged here.
set -euo pipefail
umask 077

id -u trustyclaw-operator >/dev/null 2>&1 || useradd --create-home --shell /bin/bash trustyclaw-operator
mkdir -p /home/trustyclaw-operator/.ssh
cat > /home/trustyclaw-operator/.ssh/authorized_keys2 <<'KEYS'
@DEPLOY_PUBLIC_KEY@
KEYS
chmod 700 /home/trustyclaw-operator/.ssh
chmod 600 /home/trustyclaw-operator/.ssh/authorized_keys2
chown -R trustyclaw-operator:trustyclaw-operator /home/trustyclaw-operator/.ssh

echo 'trustyclaw-operator ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/trustyclaw-operator
chmod 440 /etc/sudoers.d/trustyclaw-operator
gpasswd -d ubuntu sudo >/dev/null 2>&1 || true
rm -f /etc/sudoers.d/90-cloud-init-users

cat > /tmp/trustyclaw_payload.json <<'TRUSTYCLAW_PAYLOAD_EOF'
@PAYLOAD_JSON@
TRUSTYCLAW_PAYLOAD_EOF
chmod 600 /tmp/trustyclaw_payload.json
