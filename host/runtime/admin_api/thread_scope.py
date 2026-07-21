"""Host-side teardown of per-thread transient agent scopes.

Every task turn runs inside a systemd scope named after its host thread
(``trustyclaw-agent-thread-<thread_id>.scope``, created by the run-*
launchers). Freeing that scope after a turn — killed or completed — is a host
invariant shared by all four runtimes, so it lives here rather than in each
adapter: the privileged stop-agent-thread helper SIGKILLs the scope's whole
cgroup and returns once the unit is gone, so a same-thread follow-up can
recreate the name. See ``host/bootstrap/helpers/stop-agent-thread.sh``.
"""

from __future__ import annotations

import subprocess

STOP_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/stop-agent-thread"]


def stop_thread_scope(
    thread_id: str | None, command: list[str], launcher_command: list[str]
) -> None:
    """Free the thread's scope; a no-op unless this is a production launcher turn.

    Only the production sudo launcher creates a real systemd scope; a custom
    test command runs in-process with no scope to stop. Codex folds
    ``--thread-scope`` into its command, so the launcher is matched by prefix.
    """
    if thread_id is None or command[: len(launcher_command)] != launcher_command:
        return
    try:
        subprocess.run(
            [*STOP_COMMAND, thread_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # The helper SIGKILLs the cgroup, so the stop reaps promptly; this
            # bound stays above systemd's default TimeoutStopSec so a slow reap
            # never returns here before the scope is actually gone, which would
            # lift the orchestrator's thread fence early.
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Best effort: a broken privileged path is the only way this fails, and
        # raising out of close() would keep the thread fenced forever.
        pass
