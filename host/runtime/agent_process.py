"""Shared teardown for agent CLI subprocesses (Claude Code and Codex).

Both adapters spawn their CLIs through the sudo run helper, so the child may
run as root or the agent user: closing stdin (EOF) is the one reliable
shutdown lever, and an unprivileged SIGTERM/SIGKILL can raise PermissionError
— the signal ladder is best-effort only, never an error.
"""

from __future__ import annotations

import subprocess


def close_process(proc: subprocess.Popen[str], *, wait_seconds: float = 5.0) -> None:
    """stdin EOF, wait, then best-effort terminate/kill; close the pipes."""
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=wait_seconds)
    except subprocess.TimeoutExpired:
        for stop in (proc.terminate, proc.kill):
            try:
                stop()
                proc.wait(timeout=wait_seconds)
                break
            except (subprocess.TimeoutExpired, PermissionError, ProcessLookupError, OSError):
                continue
    if proc.stdout is not None:
        proc.stdout.close()
    if proc.stderr is not None:
        proc.stderr.close()
