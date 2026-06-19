"""Proxy-user helper: read selected proxy-owned network state as JSON."""

from __future__ import annotations

import json
import sys

from host.runtime.network_policy import load_policy, load_policy_updated_at, load_status
from host.runtime.state import page_network_events


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("missing read kind", file=sys.stderr)
        return 2
    kind = args[0]
    if kind == "status" and len(args) == 1:
        print(json.dumps({"status": load_status()}, sort_keys=True))
        return 0
    if kind == "policy" and len(args) == 1:
        print(json.dumps({"network_controls": load_policy(), "updated_at": load_policy_updated_at()}, sort_keys=True))
        return 0
    if kind == "events" and len(args) in (1, 2):
        try:
            since = int(args[1]) if len(args) == 2 else None
        except ValueError:
            print("since must be an integer", file=sys.stderr)
            return 2
        print(json.dumps({"events": page_network_events(since).items}, sort_keys=True))
        return 0
    print(f"unsupported read kind: {kind}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
