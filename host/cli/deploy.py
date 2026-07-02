"""Fresh deploy entrypoint: python3 -m host.cli.deploy --config <deploy-config.json>."""

from __future__ import annotations

from host.cli.lifecycle import main_for_mode


def main(argv: list[str] | None = None) -> int:
    return main_for_mode("deploy", argv)


if __name__ == "__main__":
    raise SystemExit(main())
