#!/usr/bin/env bash
set -euo pipefail
exec env PYTHONPATH=/opt/trustyclaw-host /usr/bin/python3 -m host.runtime.root_helpers.audit_github_repo
