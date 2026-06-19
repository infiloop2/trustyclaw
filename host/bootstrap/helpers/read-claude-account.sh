#!/usr/bin/env bash
set -euo pipefail
exec /usr/sbin/runuser -u trustyclaw-agent -- env HOME=/mnt/trustyclaw-agent/agent-home CLAUDE_CONFIG_DIR=/mnt/trustyclaw-agent/agent-home/.claude /usr/bin/python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys

home = Path.home()
config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude")))


def load_first(paths):
    for path in paths:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            continue
    return {}


config = load_first([
    config_dir / ".claude.json",
    Path(str(config_dir) + ".json"),
    config_dir.parent / ".claude.json",
    home / ".claude.json",
])
credentials = load_first([
    config_dir / ".credentials.json",
    home / ".claude" / ".credentials.json",
])

oauth = config.get("oauthAccount")
tokens = credentials.get("claudeAiOauth")
if not isinstance(tokens, dict):
    sys.exit(1)
access_token = tokens.get("accessToken")
if not isinstance(access_token, str) or not access_token.strip():
    sys.exit(1)

result = {
    "access_token_sha256": hashlib.sha256(access_token.strip().encode()).hexdigest(),
}
if isinstance(oauth, dict):
    account_id = oauth.get("accountUuid")
    organization_id = oauth.get("organizationUuid")
    email = oauth.get("emailAddress")
    if isinstance(account_id, str) and account_id.strip():
        result["account_id"] = account_id.strip()
    if isinstance(organization_id, str) and organization_id.strip():
        result["organization_id"] = organization_id.strip()
    if isinstance(email, str) and email.strip():
        result["email"] = email.strip()
print(json.dumps(result, sort_keys=True))
PY
