#!/usr/bin/env bash
set -euo pipefail
exec /usr/sbin/runuser -u trustyclaw-agent -- env HOME=/mnt/trustyclaw-agent/agent-home /usr/bin/python3 - <<'PY'
import base64
import json
from pathlib import Path
import sys


def jwt_payload(token):
    if not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


auth_path = Path.home() / ".codex" / "auth.json"
try:
    auth = json.loads(auth_path.read_text())
except FileNotFoundError:
    sys.exit(2)

tokens = auth.get("tokens")
tokens = tokens if isinstance(tokens, dict) else {}
account_id = tokens.get("account_id")
if isinstance(account_id, str) and account_id.strip():
    print(account_id.strip())
    sys.exit(0)

for key in ("access_token", "id_token"):
    payload = jwt_payload(tokens.get(key))
    auth_claim = payload.get("https://api.openai.com/auth")
    if isinstance(auth_claim, dict):
        account_id = auth_claim.get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id.strip():
            print(account_id.strip())
            sys.exit(0)

sys.exit(1)
PY
