"""Agent runtime orchestration: the worker pool that runs queued tasks, the
runtime process cache, and the background poller that keeps the cached runtime
status fresh. The admin API delegates here; nothing in this module speaks HTTP.

Concurrency model: each runtime has its own ``WORKER_COUNT_PER_RUNTIME`` claim
cap, and up to ``WORKER_COUNT`` total tasks can run at once across runtimes.
A Codex server is dedicated to one user-supplied thread id and kept alive after
its turn, so a follow-up task on the same thread skips the app-server boot.
Claude Code turn processes normally exit after one turn and resume by session
id. Tasks on the same thread are serialized, while tasks on different threads
run in parallel.

How the synchronization fits together:

- Two locks. The state lock (private to state.py, entered through
  ``state_update()``/``read_state()``) guards every state.json access.
  ``_POOL_LOCK`` guards ``_POOL`` and ``_CLOSING_THREADS``. The only place
  both are held is ``_claim_next_task``, and the nesting order there — the
  state lock, then _POOL_LOCK — is the one legal order; no code path acquires
  them the other way around, so they cannot deadlock. Slow work (spawning a
  runtime process, running a turn, closing a process) always happens with
  neither lock held.
- ``WORKER_WAKE`` is advisory, never load-bearing. One ``Event`` is shared by
  all workers, so worker A can consume a wakeup meant for worker B; every wait
  therefore uses a 5s timeout and re-checks the queue from scratch.
  Correctness never depends on a wakeup arriving — wakeups only reduce
  latency.
- A user thread is *unavailable* for claiming while any of three things is
  true of it: a task on it is RUNNING in state, its pool slot is busy (its
  worker has not yet released — a task can already be terminal in state while
  its worker is still unwinding), or a previous server for it is still
  shutting down (``_CLOSING_THREADS``). All three are checked in one place,
  under both locks, in ``_claim_next_task``; together they guarantee at most
  one live process per user thread.
- Cross-thread mutation of a running turn happens only through
  the runtime process ``close()`` method (the kill path). The worker that owns
  the turn then observes the dead process as an error; it never needs to be
  signalled directly.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any

from host.config import AGENT_RUNTIMES
from host.runtime import claude_code, codex_app_server, proxy_state_client, task_status
from host.runtime.state import (
    append_agent_event,
    read_state,
    save_claude_account,
    save_openai_account_id,
    state_update,
    utc_now,
)
from host.runtime.task_status import COMPLETED, FAILED, QUEUED, RUNNING

WORKER_COUNT_PER_RUNTIME = 3
WORKER_COUNT = WORKER_COUNT_PER_RUNTIME * len(AGENT_RUNTIMES)
RUNTIME_RECHECK_SECONDS = 300  # re-verify an active agent login this often (it can expire)
RUNTIME_PENDING_RECHECK_SECONDS = 5  # poll more often while loading / awaiting login
WORKER_WAKE = threading.Event()
_MANAGED_PROVIDER_BY_RUNTIME = {"codex": "openai", "claude_code": "claude"}


@dataclass
class _Slot:
    """One pooled runtime process, bound to a user thread id for its lifetime."""

    server: Any
    runtime_type: str
    thread_id: str
    busy: bool
    last_used: float
    task_id: str | None  # the running task while busy, for the kill path


# runtime/thread key -> slot. Slot count never exceeds WORKER_COUNT for long:
# busy slots are bounded by the running-task claim and idle slots are evicted
# on demand.
_POOL: dict[str, _Slot] = {}
_POOL_LOCK = threading.Lock()
# user thread id -> closes in flight. A thread with a server still shutting
# down must not start a new turn: the dying process may still be flushing the
# runtime conversation, and a resume that races it can fail and silently fork
# the conversation onto a fresh provider session. The claim rule
# treats these threads as busy until the close completes.
_CLOSING_THREADS: dict[str, int] = {}


def agent_runtime_status() -> dict[str, Any]:
    # Reads only cached state — never spawns an agent process — so the request path (and
    # /v1/health) is always fast. A background thread keeps the status fresh.
    state = read_state()
    statuses = state.get("agent_runtime_statuses", {})
    runtimes = []
    for runtime_type in sorted(AGENT_RUNTIMES):
        runtime_state = statuses.get(runtime_type, {})
        status = runtime_state.get("status", "loading") if isinstance(runtime_state, dict) else "loading"
        active = sorted(
            (
                task["task_id"]
                for task in state["tasks"]
                if task["status"] == RUNNING and task["agent_runtime"] == runtime_type
            ),
            key=_task_number,
        )
        response = {"type": runtime_type, "status": status, "active_task_ids": active}
        error_message = runtime_state.get("error_message") if isinstance(runtime_state, dict) else None
        if status == "error" and error_message:
            response["error_message"] = error_message
        runtimes.append(response)
    return {"runtimes": runtimes}


def refresh_runtime_status(runtime_type: str) -> str:
    """Re-derive the agent runtime status and persist it. Runs the provider
    check outside the state transaction so a slow runtime process never blocks
    requests."""
    if not _runtime_network_enabled(runtime_type):
        return _mark_runtime_deactivated(runtime_type)
    provider = _provider_module(runtime_type)
    try:
        status, error_message, account = provider.account_status()
    except Exception as exc:
        status, error_message, account = "error", f"unexpected error checking {runtime_type}: {exc!r}", None
    if not _runtime_network_enabled(runtime_type):
        # A policy replacement may have disabled this runtime while the provider
        # CLI check was running. Do not let that stale result overwrite the
        # deactivated state or make OAuth login available under a denied policy.
        return _mark_runtime_deactivated(runtime_type)
    deactivated = False
    with state_update() as state:
        if not _runtime_network_enabled(runtime_type):
            # The final provider-policy check is inside the state transaction so
            # a stale slow probe cannot race a disable and commit active/login
            # state after the deactivation write.
            _mark_runtime_deactivated_in_state(state, runtime_type)
            deactivated = True
        else:
            statuses = state.setdefault("agent_runtime_statuses", {})
            runtime_state = statuses.setdefault(runtime_type, {})
            previous = runtime_state.get("status", "loading")
            runtime_state["status"] = status
            if status == "error" and error_message:
                runtime_state["error_message"] = error_message
            else:
                runtime_state.pop("error_message", None)
            if status == "active":
                # The device code is spent (or moot) once the account is active.
                # Without this, a later session expiry would resurface the stale
                # record instead of letting the operator start a fresh login.
                if runtime_type == "codex":
                    state["codex_oauth"] = None
                else:
                    state["claude_oauth"] = None
            if previous == "awaiting_login" and status == "active":
                append_agent_event(state, "agent_runtime.login_completed", None, {"agent_runtime": runtime_type})
            if previous != "active" and status == "active":
                append_agent_event(state, "agent_runtime.active", None, {"agent_runtime": runtime_type})
    if deactivated:
        _finish_runtime_deactivation(runtime_type)
        return "deactivated"
    if runtime_type == "claude_code":
        account_value = account if status == "active" and isinstance(account, dict) else None
        save_claude_account(account_value)
        proxy_state_client.sync_claude_account(account_value)
    else:
        account_id = account if status == "active" and isinstance(account, str) else None
        save_openai_account_id(account_id)
        proxy_state_client.sync_openai_account_id(account_id)
    if not _runtime_network_enabled(runtime_type):
        # Account pins live outside state.json and are read by the proxy. If a
        # policy disable landed after the state transaction but before the pin
        # write above, immediately clear the stale pin and return to deactivated.
        return _mark_runtime_deactivated(runtime_type)
    if status == "active":
        WORKER_WAKE.set()  # tasks queued while logged out can start now
    return status


def _mark_runtime_deactivated(runtime_type: str) -> str:
    with state_update() as state:
        _mark_runtime_deactivated_in_state(state, runtime_type)
    _finish_runtime_deactivation(runtime_type)
    return "deactivated"


def _mark_runtime_deactivated_in_state(state: dict[str, Any], runtime_type: str) -> None:
    statuses = state.setdefault("agent_runtime_statuses", {})
    runtime_state = statuses.setdefault(runtime_type, {})
    previous = runtime_state.get("status", "loading")
    runtime_state["status"] = "deactivated"
    runtime_state.pop("error_message", None)
    if runtime_type == "codex":
        state["codex_oauth"] = None
    else:
        state["claude_oauth"] = None
    if previous != "deactivated":
        append_agent_event(state, "agent_runtime.deactivated", None, {"agent_runtime": runtime_type})


def _finish_runtime_deactivation(runtime_type: str) -> None:
    if runtime_type == "claude_code":
        save_claude_account(None)
        proxy_state_client.sync_claude_account(None)
    else:
        save_openai_account_id(None)
        proxy_state_client.sync_openai_account_id(None)
    deactivate_runtime(runtime_type, "agent runtime deactivated because its managed network provider is disabled")


def runtime_network_enabled(runtime_type: str) -> bool:
    return _runtime_network_enabled(runtime_type)


def reconcile_runtime_status_after_policy_change() -> None:
    """Synchronize cached runtime state after a policy update.

    Disabled runtimes are deactivated synchronously because that fails running
    tasks and closes their processes. Enabled runtimes are refreshed in the
    background: a policy change may have re-enabled a runtime whose poller still
    has a stale long active-runtime deadline, but the network-policy request
    path must not block on provider CLI checks.
    """
    enabled: list[str] = []
    for runtime_type in sorted(AGENT_RUNTIMES):
        if not _runtime_network_enabled(runtime_type):
            refresh_runtime_status(runtime_type)
        else:
            enabled.append(runtime_type)
    if enabled:
        threading.Thread(target=_refresh_runtimes, args=(tuple(enabled),), daemon=True).start()


def _refresh_runtimes(runtime_types: tuple[str, ...]) -> None:
    for runtime_type in runtime_types:
        try:
            refresh_runtime_status(runtime_type)
        except Exception:
            continue


def deactivate_runtime(runtime_type: str, reason: str) -> None:
    """Stop every live process for a non-active runtime and fail its in-flight
    tasks. Queued tasks are left queued; if the runtime becomes active again,
    the normal claim path can run them."""
    with state_update() as state:
        now = utc_now()
        for task in state["tasks"]:
            if task["status"] == RUNNING and task["agent_runtime"] == runtime_type:
                task_status.set_status(task, FAILED, now=now)
                task["error_message"] = reason
                append_agent_event(state, "task.failed", task["task_id"], {"error_message": reason})

    closing: list[tuple[str, Any]] = []
    with _POOL_LOCK:
        for key, slot in list(_POOL.items()):
            if slot.runtime_type != runtime_type:
                continue
            del _POOL[key]
            _begin_close_locked(slot.runtime_type, slot.thread_id)
            closing.append((key, slot.server))
    for key, server in closing:
        _finish_close(key, server)
    WORKER_WAKE.set()


def runtime_status_loop() -> None:
    next_check_at = {runtime_type: 0.0 for runtime_type in sorted(AGENT_RUNTIMES)}
    while True:
        now = time.monotonic()
        try:
            for runtime_type in sorted(AGENT_RUNTIMES):
                if now < next_check_at[runtime_type]:
                    continue
                status = refresh_runtime_status(runtime_type)
                delay = RUNTIME_RECHECK_SECONDS if status == "active" else RUNTIME_PENDING_RECHECK_SECONDS
                next_check_at[runtime_type] = time.monotonic() + delay
        except Exception:
            # Keep the loop alive; retry soon because the failed refresh did
            # not update that runtime's cached state.
            time.sleep(RUNTIME_PENDING_RECHECK_SECONDS)
            continue
        sleep_for = min(max(0.0, due - time.monotonic()) for due in next_check_at.values())
        time.sleep(min(max(sleep_for, 0.1), RUNTIME_PENDING_RECHECK_SECONDS))


def start_workers() -> None:
    for _ in range(WORKER_COUNT):
        threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=runtime_status_loop, daemon=True).start()


def worker_loop() -> None:
    # All workers share WORKER_WAKE, so this worker may consume a wakeup meant
    # for another (clear() after a single wait). That is fine by design: the
    # 5s timeout re-checks the queue regardless, so a lost wakeup costs at
    # most 5s of latency, never a stuck task.
    while True:
        WORKER_WAKE.wait(timeout=5)
        WORKER_WAKE.clear()
        try:
            run_next_task()
        except Exception:
            # run_next_task fails its claimed task internally; anything that
            # still escapes (e.g. state file I/O errors) must not kill the
            # worker thread — back off briefly and keep serving the queue.
            time.sleep(2)


def run_next_task() -> None:
    claimed = _claim_next_task()
    if claimed is None:
        return
    task_id, runtime_type, thread_id, input_message, provider_session_id = claimed
    provider = _provider_module(runtime_type)

    def steers() -> list[str]:
        current = _find_task(read_state(), task_id)
        return list(current.get("steer_messages", [])) if current else []

    def steer_delivered(message: str) -> None:
        # Drop a delivered steer from the task's pending queue. Its content is
        # already preserved as a task.message event, so nothing is lost, and
        # the queue stays bounded: steer_messages holds only undelivered
        # steers. Only this worker pops and the API only appends, so popping
        # the head it just delivered is race-free.
        with state_update() as state:
            current = _find_task(state, task_id)
            if current:
                pending = current.get("steer_messages", [])
                if pending and pending[0] == message:
                    pending.pop(0)

    def on_agent_message(message: str) -> None:
        with state_update() as state:
            append_agent_event(state, "task.message", task_id, {"message": message, "source": "agent"})

    # Everything from acquire onward is inside one try: the task was claimed
    # (marked RUNNING) above, so ANY exception from here — including a failure
    # to acquire or boot the server — must fail the task. An exception that
    # escaped to worker_loop instead would leave the task RUNNING forever with
    # no worker attached.
    server: Any | None = None
    healthy = False
    try:
        if runtime_type == "claude_code":
            status = refresh_runtime_status(runtime_type)
            if status != "active":
                raise RuntimeError(f"Claude Code runtime is {status}")
        # The slot is registered (with this task id) before the possibly slow
        # start(), so a concurrent kill can close the server mid-boot.
        # Otherwise a kill in the start() window finds no server, closes
        # nothing, and the turn runs on after the operator believes it was
        # stopped.
        server, needs_start = _acquire_server(runtime_type, thread_id, task_id)
        if needs_start:
            server.start()
        # If a kill cancelled this task while the server was starting, abandon
        # it rather than running a full turn the operator thinks was stopped.
        current = _find_task(read_state(), task_id)
        if current is None or current["status"] != RUNNING:
            return
        new_provider_session_id, output = provider.run_turn(
            server, input_message, provider_session_id, steers, on_agent_message, steer_delivered
        )
        healthy = True
        _finish_task(
            task_id,
            COMPLETED,
            output=output,
            runtime_type=runtime_type,
            thread_id=thread_id,
            provider_session_id=new_provider_session_id,
        )
    except Exception as exc:
        _finish_task(task_id, FAILED, error_message=str(exc), runtime_type=runtime_type, thread_id=thread_id)
    finally:
        if server is not None:
            _release_server(runtime_type, thread_id, server, healthy)
        WORKER_WAKE.set()


def _claim_next_task() -> tuple[str, str, str, str, str | None] | None:
    """Atomically claim the first runnable queued task. Returns
    (task_id, runtime_type, thread_id, input_message, provider session/thread id or None)."""
    with state_update() as state:
        tasks = state["tasks"]
        running = [task for task in tasks if task["status"] == RUNNING]
        if len(running) >= WORKER_COUNT:
            return None
        running_by_runtime: dict[str, int] = {}
        for task in running:
            runtime_type = task["agent_runtime"]
            running_by_runtime[runtime_type] = running_by_runtime.get(runtime_type, 0) + 1
        statuses = state.get("agent_runtime_statuses", {})
        active_runtimes = {
            runtime_type
            for runtime_type, runtime_state in statuses.items()
            if isinstance(runtime_state, dict) and runtime_state.get("status") == "active"
            and _runtime_network_enabled(runtime_type)
        }
        running_threads = {_pool_key(task["agent_runtime"], task["thread_id"]) for task in running}
        # A thread is unavailable while: a task on it is RUNNING (state), its
        # slot is busy (a finished task's worker that has not released yet —
        # status goes terminal in _finish_task BEFORE the finally releases the
        # slot), or its previous server is still shutting down. Checking all
        # three here, under both locks, is what guarantees at most one live
        # process per user thread.
        with _POOL_LOCK:
            busy_slots = set()
            busy_by_runtime: dict[str, int] = {}
            for slot in _POOL.values():
                if not slot.busy:
                    continue
                busy_slots.add(_pool_key(slot.runtime_type, slot.thread_id))
                busy_by_runtime[slot.runtime_type] = busy_by_runtime.get(slot.runtime_type, 0) + 1
            if len(busy_slots) >= WORKER_COUNT:
                return None
            unavailable = running_threads | busy_slots | set(_CLOSING_THREADS)
        task = next(
            (
                t for t in tasks
                if t["status"] == QUEUED
                and t["agent_runtime"] in active_runtimes
                and running_by_runtime.get(t["agent_runtime"], 0) < WORKER_COUNT_PER_RUNTIME
                and busy_by_runtime.get(t["agent_runtime"], 0) < WORKER_COUNT_PER_RUNTIME
                and _pool_key(t["agent_runtime"], t["thread_id"]) not in unavailable
            ),
            None,
        )
        if task is None:
            return None
        runtime_type = task["agent_runtime"]
        task_status.set_status(task, RUNNING, now=utc_now())
        append_agent_event(state, "task.started", task["task_id"], {})
        append_agent_event(
            state,
            "task.message",
            task["task_id"],
            {"message": task["input_message"], "source": "user"},
        )
        mapping = state.get(_session_map_key(runtime_type), {}).get(task["thread_id"]) or {}
        session_key = "session_id" if runtime_type == "claude_code" else "codex_thread_id"
        return task["task_id"], runtime_type, task["thread_id"], task["input_message"], mapping.get(session_key)


def _acquire_server(runtime_type: str, thread_id: str, task_id: str) -> tuple[Any, bool]:
    """Return (server, needs_start): a warm server already bound to this
    thread, or a fresh one (evicting the least recently used idle slot when
    the pool is full). Closes evicted/dead servers outside the pool lock."""
    evicted: list[tuple[str, Any]] = []
    key = _pool_key(runtime_type, thread_id)
    with _POOL_LOCK:
        slot = _POOL.get(key)
        if slot is not None and not slot.busy and slot.server.alive():
            slot.busy = True
            slot.task_id = task_id
            return slot.server, False
        if slot is not None and not slot.busy:
            # The warm server died while idle; replace it.
            del _POOL[key]
            evicted.append((_pool_key(slot.runtime_type, slot.thread_id), slot.server))
            _begin_close_locked(slot.runtime_type, slot.thread_id)
        # A still-BUSY slot for this thread cannot happen here — the claim rule
        # refuses a thread whose slot is busy. If a future change breaks that
        # invariant, the fall-through below overwrites the slot and the stale
        # worker's release closes its own (now unpooled) server: degraded but
        # not corrupting.
        if len(_POOL) >= WORKER_COUNT:
            idle = [s for s in _POOL.values() if not s.busy]
            if idle:
                lru = min(idle, key=lambda s: s.last_used)
                del _POOL[_pool_key(lru.runtime_type, lru.thread_id)]
                evicted.append((_pool_key(lru.runtime_type, lru.thread_id), lru.server))
                _begin_close_locked(lru.runtime_type, lru.thread_id)
        server = _new_agent_server(runtime_type)
        _POOL[key] = _Slot(server, runtime_type, thread_id, True, time.monotonic(), task_id)
    for old_key, old_server in evicted:
        _finish_close(old_key, old_server)
    return server, True


def _release_server(runtime_type: str, thread_id: str, server: Any, healthy: bool) -> None:
    """Return a server to the warm pool after a turn, or close it: an unhealthy
    server (failed turn — it may be dead or wedged) must not poison the next
    task on this thread. A kill may already have removed the slot."""
    close = False
    key = _pool_key(runtime_type, thread_id)
    with _POOL_LOCK:
        slot = _POOL.get(key)
        if slot is None or slot.server is not server:
            close = True  # slot was killed or replaced; just make sure it dies
        elif healthy and server.alive():
            slot.busy = False
            slot.task_id = None
            slot.last_used = time.monotonic()
        else:
            del _POOL[key]
            close = True
        if close:
            _begin_close_locked(runtime_type, thread_id)
    if close:
        _finish_close(key, server)


def close_task_server(task_id: str) -> None:
    """Kill the runtime process running ``task_id`` (the kill-task path). The
    worker blocked in run_turn surfaces the dead server as an error and finds
    the task already cancelled, so the cancellation sticks."""
    with _POOL_LOCK:
        slot = next((s for s in _POOL.values() if s.task_id == task_id), None)
        if slot is not None:
            key = _pool_key(slot.runtime_type, slot.thread_id)
            del _POOL[key]
            _begin_close_locked(slot.runtime_type, slot.thread_id)
    if slot is not None:
        _finish_close(_pool_key(slot.runtime_type, slot.thread_id), slot.server)


def _begin_close_locked(runtime_type: str, thread_id: str) -> None:
    """Mark a thread's server as shutting down. Must run under _POOL_LOCK, in
    the same critical section that removed the slot — otherwise a claim could
    slip between removal and the mark and start a new turn on a runtime thread
    whose old process is still dying. The count handles concurrent closers
    (e.g. the kill path and the killed worker's release closing the same
    server)."""
    key = _pool_key(runtime_type, thread_id)
    _CLOSING_THREADS[key] = _CLOSING_THREADS.get(key, 0) + 1


def _finish_close(key: str, server: Any) -> None:
    """Close a server marked by _begin_close_locked and lift the mark. Runs
    outside _POOL_LOCK: shutdown can take seconds and must not block the pool."""
    try:
        server.close()
    finally:
        with _POOL_LOCK:
            remaining = _CLOSING_THREADS.get(key, 1) - 1
            if remaining <= 0:
                _CLOSING_THREADS.pop(key, None)
            else:
                _CLOSING_THREADS[key] = remaining
        WORKER_WAKE.set()  # tasks queued on this thread are claimable again


def _finish_task(
    task_id: str,
    status: str,
    *,
    output: str | None = None,
    error_message: str | None = None,
    runtime_type: str,
    thread_id: str,
    provider_session_id: str | None = None,
) -> None:
    with state_update() as state:
        task = _find_task(state, task_id)
        if task is None or task["status"] != RUNNING:
            return  # killed while the turn was in flight
        task_status.set_status(task, status, now=utc_now())
        if status == COMPLETED:
            task["output_message"] = output
            session_record = {"last_used_at": utc_now()}
            if runtime_type == "claude_code":
                session_record["session_id"] = provider_session_id
            else:
                session_record["codex_thread_id"] = provider_session_id
            state.setdefault(_session_map_key(runtime_type), {})[thread_id] = session_record
            append_agent_event(state, "task.completed", task_id, {})
        else:
            task["error_message"] = error_message
            append_agent_event(state, "task.failed", task_id, {"error_message": error_message})


def _find_task(state: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    return next((task for task in state["tasks"] if task["task_id"] == task_id), None)


def _task_number(task_id: str) -> int:
    try:
        return int(str(task_id).rsplit("_", 1)[-1])
    except ValueError:
        return 0


def _provider_module(runtime_type: str | None = None) -> Any:
    return claude_code if runtime_type == "claude_code" else codex_app_server


def _new_agent_server(runtime_type: str) -> Any:
    if runtime_type == "claude_code":
        return claude_code.ClaudeCodeSession()
    return codex_app_server.CodexAppServer()


def _session_map_key(runtime_type: str) -> str:
    return "claude_sessions" if runtime_type == "claude_code" else "codex_threads"


def _pool_key(runtime_type: str, thread_id: Any) -> str:
    return f"{runtime_type}:{thread_id}"


def _runtime_network_enabled(runtime_type: str) -> bool:
    provider = _MANAGED_PROVIDER_BY_RUNTIME.get(runtime_type)
    policy = proxy_state_client.network_policy()
    managed = policy.get("managed_ai_provider_network_access", {})
    return bool(provider and isinstance(managed, dict) and managed.get(provider) is True)
