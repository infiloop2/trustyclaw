#!/usr/bin/env bash
set -euo pipefail

mode="read"
expected_token_sha256=""
while [[ "$#" -ge 1 ]]; do
  case "$1" in
    --attest)
      mode="attest"
      shift
      ;;
    --expected-token-sha256)
      if [[ "$#" -lt 2 ]]; then
        echo "usage: read-claude-account [--attest [--expected-token-sha256 <sha256>]]" >&2
        exit 2
      fi
      expected_token_sha256="$2"
      shift 2
      ;;
    *)
      echo "usage: read-claude-account [--attest [--expected-token-sha256 <sha256>]]" >&2
      exit 2
      ;;
  esac
done
if [[ -n "${expected_token_sha256}" && "${mode}" != "attest" ]]; then
  echo "usage: read-claude-account [--attest [--expected-token-sha256 <sha256>]]" >&2
  exit 2
fi

if [[ "${mode}" == "attest" ]]; then
  # Attestation asks api.anthropic.com/api/oauth/profile who the agent's
  # current OAuth token belongs to, so the account identity is bound to the
  # token by the provider instead of agent-writable metadata. It runs as root
  # on purpose: the agent uid can only reach the local proxy (whose account
  # guard rejects a just-rotated token) and the admin uid has no egress at
  # all, while root egress is open. The token is read straight from the agent
  # credential file and never leaves this process.
  EXPECTED_TOKEN_SHA256="${expected_token_sha256}" exec /usr/bin/python3 - <<'PY'
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

agent_home = Path("/mnt/trustyclaw-agent/agent-home")


def load_first(paths):
    for path in paths:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            continue
    return {}


credentials = load_first([
    agent_home / ".claude" / ".credentials.json",
])
tokens = credentials.get("claudeAiOauth")
access_token = tokens.get("accessToken") if isinstance(tokens, dict) else None
if not isinstance(access_token, str) or not access_token.strip():
    print("no Claude OAuth credentials to attest", file=sys.stderr)
    sys.exit(1)
access_token = access_token.strip()
access_token_sha256 = hashlib.sha256(access_token.encode()).hexdigest()
expected_token_sha256 = os.environ.get("EXPECTED_TOKEN_SHA256", "").strip()
if expected_token_sha256 and access_token_sha256 != expected_token_sha256:
    print("Claude OAuth token changed before attestation", file=sys.stderr)
    sys.exit(1)

request = urllib.request.Request(
    "https://api.anthropic.com/api/oauth/profile",
    headers={"Authorization": "Bearer " + access_token},
)
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        profile = json.load(response)
except urllib.error.HTTPError as exc:
    print(f"Claude profile endpoint rejected the token: HTTP {exc.code}", file=sys.stderr)
    sys.exit(1)
except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
    print(f"could not reach the Claude profile endpoint: {exc}", file=sys.stderr)
    sys.exit(1)

account = profile.get("account") if isinstance(profile, dict) else None
organization = profile.get("organization") if isinstance(profile, dict) else None
account_uuid = account.get("uuid") if isinstance(account, dict) else None
if not isinstance(account_uuid, str) or not account_uuid.strip():
    print("Claude profile response has no account uuid", file=sys.stderr)
    sys.exit(1)

result = {
    "access_token_sha256": access_token_sha256,
    "account_uuid": account_uuid.strip(),
}
email = account.get("email") if isinstance(account, dict) else None
if isinstance(email, str) and email.strip():
    result["email"] = email.strip()
organization_uuid = organization.get("uuid") if isinstance(organization, dict) else None
if isinstance(organization_uuid, str) and organization_uuid.strip():
    result["organization_uuid"] = organization_uuid.strip()
print(json.dumps(result, sort_keys=True))
PY
fi

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
