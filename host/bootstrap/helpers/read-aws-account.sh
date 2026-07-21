#!/usr/bin/env bash
set -euo pipefail
# Makes one signed AWS call as root (the admin uid has no egress; the agent uid
# can only reach the local proxy). The admin service decrypts the connected
# credential and passes it in this process's environment
# (TRUSTYCLAW_BEDROCK_AWS_ACCESS_KEY_ID / ..._SECRET_ACCESS_KEY, preserved
# across sudo by the env_keep entry in the sudoers file). The secret exists in
# the admin and helper process environments but never reaches disk or an agent
# process.
exec env PYTHONPATH=/opt/trustyclaw-host /usr/bin/python3 -m host.runtime.root_helpers.aws_account "$@"
