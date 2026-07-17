"""Admin-side resolution of pushes held by the ``.github`` approval gate.

The proxy enqueues a ``pending_pushes`` row and quarantines the objects when a
gated push touches ``.github/``. The operator then approves or rejects it here:

- **approve** invokes the ``approve-github-push`` root helper, which replays the
  quarantined objects to GitHub with the working token (the admin service has no
  egress), then marks the row approved.
- **reject** invokes the same root helper in cleanup mode to drop the pending
  refs, then marks the row rejected.

Replay failures are recorded on the row (``failed``); the operator's recovery
is to have the agent push again, which starts a fresh gate round. Pending-ref
cleanup is best-effort housekeeping of the proxy-private quarantine mirror —
it never changes a resolution outcome, and refs a failed cleanup leaves behind
are inert (never pushed anywhere). Resolutions serialize on RESOLVE_LOCK: the
admin service is the single resolver by construction (only its role has UPDATE
on pending_pushes, and the port bind keeps it single-instance), so an
in-process lock is the whole story. A crash mid-resolve leaves the row
``pending`` and the operator simply approves or rejects again.
"""

from __future__ import annotations

import threading
from typing import Any

from host.runtime import state
from host.runtime.github_credential import HelperError, _run_helper_json

APPROVE_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/approve-github-push"]
APPROVE_HELPER_TIMEOUT_SECONDS = 150
# Serializes resolutions across admin threads. Bounded wait: a second
# operator action during a slow helper run gets a crisp conflict error
# instead of queueing behind a 150s replay.
RESOLVE_LOCK = threading.Lock()
RESOLVE_LOCK_TIMEOUT_SECONDS = 5


class PendingPushError(Exception):
    """The pending push could not be resolved (missing, already resolved, or the
    replay failed)."""


def approve(push_id: str) -> dict[str, Any]:
    if not RESOLVE_LOCK.acquire(timeout=RESOLVE_LOCK_TIMEOUT_SECONDS):
        raise PendingPushError("another pending push is being resolved")
    try:
        row = _require_pending(push_id)
        token = state.read_proxy_github_token()
        if not token:
            # An approval resolves the row exactly once. With no working token
            # the replay cannot run, so the push fails terminally like any other
            # replay failure; recovery is the agent pushing again once the
            # credential is fixed, which starts a fresh gate round.
            detail = "no working GitHub token is available to replay the push"
            _cleanup_pending_refs(row, push_id)
            state.resolve_pending_push(push_id, "failed", detail)
            raise PendingPushError(detail)
        payload = _helper_payload(row, push_id, "approve", token=token)
        try:
            _run_helper_json(APPROVE_COMMAND, payload, timeout=APPROVE_HELPER_TIMEOUT_SECONDS)
        except HelperError as exc:
            detail = f"replay to GitHub failed: {exc}"
            _cleanup_pending_refs(row, push_id)
            state.resolve_pending_push(push_id, "failed", detail[:500])
            raise PendingPushError(detail) from exc
        return state.resolve_pending_push(push_id, "approved")
    finally:
        RESOLVE_LOCK.release()


def reject(push_id: str) -> dict[str, Any]:
    if not RESOLVE_LOCK.acquire(timeout=RESOLVE_LOCK_TIMEOUT_SECONDS):
        raise PendingPushError("another pending push is being resolved")
    try:
        row = _require_pending(push_id)
        # Rejecting means the push never leaves the box, so the row is rejected
        # no matter how the ref cleanup fares.
        _cleanup_pending_refs(row, push_id)
        return state.resolve_pending_push(push_id, "rejected")
    finally:
        RESOLVE_LOCK.release()


def _require_pending(push_id: str) -> dict[str, Any]:
    row = state.get_pending_push(push_id)
    if row is None:
        raise PendingPushError("pending push not found")
    if row["status"] != "pending":
        raise PendingPushError(f"pending push is already {row['status']}")
    return row


def _helper_payload(row: dict[str, Any], push_id: str, action: str, *, token: str | None = None) -> dict[str, Any]:
    payload = {
        "owner": row["owner"],
        "repo": row["repo"],
        "push_id": push_id,
        "action": action,
    }
    if action == "approve":
        payload["ref_updates"] = row["ref_updates"]
        if token is not None:
            payload["token"] = token
    return payload


def _cleanup_pending_refs(row: dict[str, Any], push_id: str) -> None:
    payload = _helper_payload(row, push_id, "cleanup")
    try:
        _run_helper_json(APPROVE_COMMAND, payload, timeout=APPROVE_HELPER_TIMEOUT_SECONDS)
    except HelperError:
        pass  # leftover refs in the quarantine mirror are inert
