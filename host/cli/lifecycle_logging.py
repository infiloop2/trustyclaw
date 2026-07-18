"""Small logging helper for TrustyClaw host lifecycle commands.

Progress goes to stderr; stdout carries only the final result JSON, so
callers can redirect or parse it directly.
"""

from __future__ import annotations

import sys


def _log(message: str) -> None:
    print(f"[deploy] {message}", file=sys.stderr, flush=True)
