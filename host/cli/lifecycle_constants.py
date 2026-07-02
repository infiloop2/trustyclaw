"""Constants shared by TrustyClaw host lifecycle commands."""

from __future__ import annotations

INSTANCE_TAG_KEY = "trustyclaw-host-agent-name"
OWNER_TAG_KEY = "trustyclaw-host"
VOLUME_ROLE_TAG_KEY = "trustyclaw-host-volume-role"
VERSION_TAG_KEY = "trustyclaw-host-version"
SSH_USER = "trustyclaw-operator"
INSTANCE_TYPE = "t3.small"
ROOT_VOLUME_SIZE_GB = 16
# Sized for the event retention caps (1M network + 1M agent events with
# bounded row sizes) plus Postgres overhead; health reports the mount's usage.
ADMIN_VOLUME_SIZE_GB = 16
AGENT_VOLUME_SIZE_GB = 8
ADMIN_VOLUME_DEVICE = "/dev/sdf"
AGENT_VOLUME_DEVICE = "/dev/sdg"
SSH_WAIT_ATTEMPTS = 60
SSH_WAIT_SECONDS = 10
SSH_INGRESS = {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
