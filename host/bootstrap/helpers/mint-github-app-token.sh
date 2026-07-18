#!/usr/bin/env bash
set -euo pipefail
exec env PYTHONPATH=/opt/trustyclaw-host /usr/bin/python3 -m host.runtime.root_helpers.mint_github_app_token
