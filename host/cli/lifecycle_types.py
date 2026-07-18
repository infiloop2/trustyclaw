"""Shared types for TrustyClaw host lifecycle commands."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LifecycleCommand:
    mode: str
    agent_name: str
    # SHA-256 hex digest of the admin password; the CLI never handles the
    # password itself. Required for deploy and reconfigure.
    admin_password_sha256: str | None = None
    allow_upgrade: bool = False
    # GitHub delivery: a full 40-hex public-repo commit to pin, or "" to pin
    # the latest main commit. None selects the SSH delivery of the local
    # checkout.
    github_commit_sha: str | None = None
    # Operator endpoints for deploy and reconfigure; at least one is required.
    operator_ssh_public_key: str | None = None
    operator_cloudflare_hostname: str | None = None
