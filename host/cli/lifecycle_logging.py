"""Small logging helper for TrustyClaw host lifecycle commands."""

from __future__ import annotations


def _log(message: str) -> None:
    print(f"[deploy] {message}", flush=True)
