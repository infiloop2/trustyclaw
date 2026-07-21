#!/usr/bin/env bash
set -euo pipefail

# Tear down a host thread's transient agent scope. A killed or finished task
# leaves its runtime process, and anything that process spawned (a shell still
# inside a long-running command), in the cgroup of the
# trustyclaw-agent-thread-<thread_id>.scope the run-* launcher created.
# Signalling the launcher only reparents those descendants; while any remain the
# scope stays active and its name cannot be reused, so the next task on this
# thread fails to recreate the identically named scope. SIGKILL the whole cgroup
# first: this is a kill, so graceful termination buys nothing, and it avoids
# systemctl stop blocking for the scope's TimeoutStopSec on a child that ignores
# SIGTERM. stop then reaps the emptied unit promptly and returns once it is gone;
# reset-failed frees the name even if the stopped scope lingers as failed. All
# three are no-ops when the scope is already gone (the normal-completion path),
# so the kill path can call this after every turn. Admin invokes this exact path
# through the trustyclaw-host sudoers policy.

thread_id="${1:-}"
# The id becomes a unit name, so validate it exactly as the run-* launchers do.
if ! [[ "${thread_id}" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
  echo "stop-agent-thread: invalid thread id: ${thread_id:-<missing>}" >&2
  exit 64
fi

scope="trustyclaw-agent-thread-${thread_id}.scope"
# Best effort by design. SIGKILL empties the cgroup with an unignorable signal,
# so stop then reaps an already-dead unit; a real stop failure needs an
# unresponsive PID 1 or a D-state task, both of which break the whole host. A
# missing unit (the normal-completion path, where the scope is already gone) is
# expected and not an error. Failing loudly would only propagate through the
# runtime close() into the orchestrator, which keeps a thread fenced when close
# raises: that would wedge the thread permanently, a worse and less recoverable
# outcome than the transient it prevents (a same-thread follow-up retries once
# the scope clears). So swallow every case here.
systemctl kill --signal=KILL "${scope}" 2>/dev/null || true
systemctl stop "${scope}" 2>/dev/null || true
systemctl reset-failed "${scope}" 2>/dev/null || true
