"""Test helpers that read/write whole admin-state snapshots.

The runtime uses per-operation storage accessors (host.runtime.state); tests
often want to stage or inspect a complete picture instead. load_state() and
save_state() keep the old dict shape the tests were written against — tasks
list, counters, runtime statuses, the codex_threads/claude_sessions maps with
their legacy per-runtime session keys, and the OAuth records. save_state()
replaces the tables to mirror the dict exactly.

Idempotency replay records are absent on purpose: they live in
admin_api.IDEMPOTENCY_ENTRIES (process memory), which tests manipulate
directly.
"""

from __future__ import annotations

from typing import Any

from host.runtime import state

_SESSION_MAPS = {"codex_threads": ("codex", "codex_thread_id"), "claude_sessions": ("claude_code", "session_id")}


def load_state() -> dict[str, Any]:
    from host.runtime import db

    from host.runtime import orchestrator

    snapshot: dict[str, Any] = {
        "agent_runtime_statuses": orchestrator.all_runtime_status_records(),
        "codex_threads": {},
        "claude_sessions": {},
        "codex_oauth": state.oauth_login("codex"),
        "claude_oauth": state.oauth_login("claude"),
    }
    with db.transaction() as cur:
        cur.execute(f"SELECT {state._TASK_FIELDS} FROM tasks ORDER BY number")
        snapshot["tasks"] = [state._task_from_row(row) for row in cur.fetchall()]
        for task in snapshot["tasks"]:
            task["steer_messages"] = state.task_steers(str(task.get("task_id")), cur)
        cur.execute("SELECT value FROM counters WHERE name = 'next_task_number'")
        row = cur.fetchone()
        snapshot["next_task_number"] = int(row[0]) if row else 1
        cur.execute(
            "SELECT agent_runtime, thread_id, provider_session_id, last_used_at"
            " FROM thread_sessions ORDER BY thread_id"
        )
        for runtime, thread_id, provider_session_id, last_used_at in cur.fetchall():
            map_key = "claude_sessions" if runtime == "claude_code" else "codex_threads"
            session_key = _SESSION_MAPS[map_key][1]
            mapping: dict[str, Any] = {}
            if last_used_at is not None:
                mapping["last_used_at"] = last_used_at
            if provider_session_id is not None:
                mapping[session_key] = provider_session_id
            snapshot[map_key][str(thread_id)] = mapping
    return snapshot


def save_state(snapshot: dict[str, Any]) -> None:
    from host.runtime import orchestrator

    with orchestrator._RUNTIME_STATUS_LOCK:
        orchestrator._RUNTIME_STATUSES.clear()
        for runtime, record in snapshot.get("agent_runtime_statuses", {}).items():
            record = dict(record) if isinstance(record, dict) else {}
            record.setdefault("status", "loading")
            orchestrator._RUNTIME_STATUSES[runtime] = record

    with state.mutation() as cur:
        cur.execute("DELETE FROM tasks")
        for task in snapshot.get("tasks", []):
            state.insert_task(cur, task)
        # Never leave the counter at or below a seeded task number: tasks
        # carry a UNIQUE task_id, so a later create must allocate past them
        # (production numbering is dense; synthetic seeds may not be).
        numbers = []
        for task in snapshot.get("tasks", []):
            tail = str(task.get("task_id", "")).rsplit("_", 1)[-1]
            if tail.isdigit():
                numbers.append(int(tail))
        counter = max(int(snapshot.get("next_task_number", 1)), max(numbers, default=0) + 1)
        cur.execute(
            "INSERT INTO counters (name, value) VALUES ('next_task_number', %s)"
            " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
            (counter,),
        )
        cur.execute("DELETE FROM thread_sessions")
        for map_key, (runtime, session_key) in _SESSION_MAPS.items():
            for thread_id, mapping in snapshot.get(map_key, {}).items():
                mapping = mapping if isinstance(mapping, dict) else {}
                state.save_thread_session(
                    cur,
                    runtime,
                    thread_id,
                    mapping.get(session_key),
                    mapping.get("last_used_at"),
                )
        state.set_oauth_login(cur, "codex", snapshot.get("codex_oauth"))
        state.set_oauth_login(cur, "claude", snapshot.get("claude_oauth"))
