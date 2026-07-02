"""Start entrypoint: python3 -m host.cli.start --config <config.json>."""

from __future__ import annotations

from host.cli.power import main_for_power_mode


def main(argv: list[str] | None = None) -> int:
    return main_for_power_mode("start", argv)


if __name__ == "__main__":
    raise SystemExit(main())
