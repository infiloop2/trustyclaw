"""Shared types for TrustyClaw host lifecycle commands."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LifecycleCommand:
    mode: str
    config_path: str
    admin_password_env: str | None = None
    allow_upgrade: bool = False
    result_file: str | None = None
