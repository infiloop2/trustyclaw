"""Admin-side resolution of pushes held by the ``.github`` approval gate.

The proxy enqueues a ``pending_pushes`` row and quarantines the objects when a
gated push touches ``.github/``. The operator then approves or rejects it here:

- **approve** invokes the ``approve-github-push`` root helper, which replays the
  quarantined objects to GitHub with the working token (the admin service has no
  egress), then marks the row approved.
- **reject** invokes the same root helper in cleanup mode to drop the pending
  refs, then marks the row rejected.

Replay failures are recorded on the row (``failed``) after one best-effort
cleanup attempt. Cleanup failures are terminal too: the row detail records the
cleanup error for later maintenance instead of sending the operator through a
retry loop.
"""

from __future__ import annotations

from typing import Any

from host.runtime import state
from host.runtime.github_credential import HelperError, _run_helper_json

APPROVE_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/approve-github-push"]
APPROVE_HELPER_TIMEOUT_SECONDS = 150
HELPER_CLEANUP_AFTER_PUSH_CODE = "cleanup_after_push"


class PendingPushError(Exception):
    """The pending push could not be resolved (missing, already resolved, or the
    replay failed)."""


def approve(push_id: str) -> dict[str, Any]:
    row = _claim_pending(push_id)
    token = state.read_proxy_github_token()
    if not token:
        detail = "no working GitHub token is available to replay the push"
        detail = _append_cleanup_detail(detail, _cleanup_pending_refs(row, push_id))
        state.resolve_pending_push(push_id, "failed", detail[:500])
        raise PendingPushError(detail) from None
    payload = _helper_payload(row, push_id, "approve", token=token)
    try:
        _run_helper_json(APPROVE_COMMAND, payload, timeout=APPROVE_HELPER_TIMEOUT_SECONDS)
    except HelperError as exc:
        if exc.code == HELPER_CLEANUP_AFTER_PUSH_CODE:
            state.resolve_pending_push(push_id, "approved", f"pending-ref cleanup failed: {exc}"[:500])
            return _require(push_id)
        detail = _append_cleanup_detail(f"replay to GitHub failed: {exc}", _cleanup_pending_refs(row, push_id))
        state.resolve_pending_push(push_id, "failed", detail[:500])
        raise PendingPushError(detail) from exc
    state.resolve_pending_push(push_id, "approved")
    return _require(push_id)


def reject(push_id: str) -> dict[str, Any]:
    row = _claim_pending(push_id)
    payload = _helper_payload(row, push_id, "cleanup")
    try:
        _run_helper_json(APPROVE_COMMAND, payload, timeout=APPROVE_HELPER_TIMEOUT_SECONDS)
    except HelperError as exc:
        state.resolve_pending_push(push_id, "failed", f"pending-ref cleanup failed: {exc}"[:500])
        raise PendingPushError(f"pending-ref cleanup failed: {exc}") from exc
    state.resolve_pending_push(push_id, "rejected")
    return _require(push_id)


def _helper_payload(row: dict[str, Any], push_id: str, action: str, *, token: str | None = None) -> dict[str, Any]:
    payload = {
        "owner": row["owner"],
        "repo": row["repo"],
        "push_id": push_id,
        "action": action,
        "ref_updates": row["ref_updates"],
    }
    if token is not None:
        payload["token"] = token
    return payload


def _cleanup_pending_refs(row: dict[str, Any], push_id: str) -> str | None:
    payload = _helper_payload(row, push_id, "cleanup")
    try:
        _run_helper_json(APPROVE_COMMAND, payload, timeout=APPROVE_HELPER_TIMEOUT_SECONDS)
    except HelperError as exc:
        return str(exc)[:300]
    return None


def _append_cleanup_detail(detail: str, cleanup_detail: str | None) -> str:
    if cleanup_detail:
        return f"{detail}; cleanup failed: {cleanup_detail}"
    return detail


def _claim_pending(push_id: str) -> dict[str, Any]:
    row = state.claim_pending_push(push_id)
    if row is not None:
        return row
    existing = state.get_pending_push(push_id)
    if existing is None:
        raise PendingPushError("pending push not found")
    raise PendingPushError(f"pending push is already {existing['status']}")


def _require(push_id: str) -> dict[str, Any]:
    row = state.get_pending_push(push_id)
    if row is None:
        raise PendingPushError("pending push not found")
    return row
