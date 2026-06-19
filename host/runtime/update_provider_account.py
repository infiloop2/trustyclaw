"""Proxy-user helper: replace provider account pins used by network guards."""

from __future__ import annotations

import json
import sys

from host.runtime.state import save_proxy_claude_account, save_proxy_openai_account_id


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        provider = payload.get("provider")
        if provider == "openai":
            account_id = payload.get("account_id")
            if account_id is not None and not isinstance(account_id, str):
                raise ValueError("account_id must be a string or null")
            save_proxy_openai_account_id(account_id)
        elif provider == "claude":
            account = payload.get("account")
            if account is not None and not isinstance(account, dict):
                raise ValueError("account must be an object or null")
            save_proxy_claude_account(account if isinstance(account, dict) else None)
        else:
            raise ValueError("provider must be openai or claude")
        print(json.dumps({"status": "ok"}, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
