"""Task lifecycle: the single source of truth for task statuses and the legal
transitions between them.

Centralizing this keeps the invariants in one place instead of being re-derived
at each call site: terminal statuses are final, and only the documented moves
happen. ``set_status`` refuses any transition not in ``TRANSITIONS`` loudly, so
a coding mistake (e.g. resurrecting a cancelled task) fails fast rather than
silently corrupting the queue.
"""

from __future__ import annotations

from typing import Any

QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

#: from-status -> the statuses it may move to.
TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({RUNNING, CANCELLED}),
    RUNNING: frozenset({COMPLETED, FAILED, CANCELLED}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
}


def set_status(task: dict[str, Any], to_status: str, *, now: str) -> None:
    """Move ``task`` to ``to_status`` and stamp ``updated_at``. Raises on an
    illegal transition so invariants are enforced centrally."""
    current = task["status"]
    if to_status not in TRANSITIONS.get(current, frozenset()):
        raise ValueError(f"illegal task transition {current!r} -> {to_status!r}")
    task["status"] = to_status
    task["updated_at"] = now
