#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: clear-agent-auth <codex|claude>" >&2
  exit 2
fi

runtime="$1"
case "${runtime}" in
  codex|claude) ;;
  *)
    echo "usage: clear-agent-auth <codex|claude>" >&2
    exit 2
    ;;
esac

exec /usr/sbin/runuser -u trustyclaw-agent -- env HOME=/mnt/trustyclaw-agent/agent-home CLAUDE_CONFIG_DIR=/mnt/trustyclaw-agent/agent-home/.claude /usr/bin/python3 - "${runtime}" <<'PY'
import json
import os
from pathlib import Path
import sys

runtime = sys.argv[1]
home = Path.home()
claude_config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude")))

paths = [home / ".codex" / "auth.json"] if runtime == "codex" else [
    claude_config_dir / ".credentials.json",
    home / ".claude" / ".credentials.json",
]

removed = []
for path in paths:
    try:
        path.unlink()
        removed.append(str(path))
    except FileNotFoundError:
        pass

if runtime == "claude":
    # Claude stores account metadata in JSON config files that may also hold
    # non-auth settings. Remove only the OAuth account block instead of
    # deleting the whole file.
    for path in (
        claude_config_dir / ".claude.json",
        Path(str(claude_config_dir) + ".json"),
        claude_config_dir.parent / ".claude.json",
        home / ".claude.json",
    ):
        try:
            value = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "oauthAccount" in value:
            value.pop("oauthAccount", None)
            path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
            removed.append(str(path) + ":oauthAccount")

print(json.dumps({"removed": removed}, sort_keys=True))
PY
