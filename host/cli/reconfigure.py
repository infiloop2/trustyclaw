"""Reconfigure entrypoint: python3 -m host.cli.reconfigure --config <config.json>."""

from __future__ import annotations

from host.cli.lifecycle import main_for_mode


def main(argv: list[str] | None = None) -> int:
    return main_for_mode("reconfigure", argv)


if __name__ == "__main__":
    raise SystemExit(main())
