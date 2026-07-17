"""Dedicated non-egress service for the agent's network introspection tools."""

from __future__ import annotations

from host.runtime import network_introspection_api


def main() -> int:
    network_introspection_api.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
