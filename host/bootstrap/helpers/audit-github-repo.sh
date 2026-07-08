#!/usr/bin/env bash
set -euo pipefail
exec env PYTHONPATH=/opt/trustyclaw-host /usr/bin/python3 -m host.runtime.audit_github_repo
