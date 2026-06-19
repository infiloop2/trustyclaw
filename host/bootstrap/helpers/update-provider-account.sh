#!/usr/bin/env bash
set -euo pipefail
exec /usr/sbin/runuser -u trustyclaw-proxy -- env PYTHONPATH=/opt/trustyclaw-host /usr/bin/python3 -m host.runtime.update_provider_account
