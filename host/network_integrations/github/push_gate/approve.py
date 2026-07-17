"""Root helper behind ``approve-github-push``.

The ``.github`` push-approval gate quarantines a held push's objects under
``refs/pending/<id>/<n>`` in the proxy's mirror. When the operator approves, the
admin service (which has no egress) calls this helper to replay the push to
GitHub: root has egress and the working token, and reads the proxy-owned mirror.
It runs a single ``git push`` of every held ref, then best-effort drops the
pending refs so ``git gc`` can reclaim them (a leftover ref is inert in the
proxy-private mirror). A validation or push failure fails the approval; the
operator's recovery is to have the agent push again, which starts a fresh gate
round.

Input (stdin JSON):
``{"action": "approve", "owner": "...", "repo": "...", "push_id": "...",
   "token": "...",
   "ref_updates": [{"old": "<oid>", "new": "<oid>", "ref": "refs/heads/..."}, ...]}``
or ``{"action": "cleanup", "owner": ..., "repo": ..., "push_id": ...}`` to
delete pending refs after rejection (cleanup lists the pending refs itself, so
it takes no ref_updates).

Output: ``{"ok": true}`` or ``{"error": {"message": "..."}}`` (exit 2). A
non-fast-forward or rejected ref surfaces as an error rather than being
forced.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any, NoReturn

from host.network_integrations.github.push_gate import engine

GIT_BIN = "/usr/bin/git"
PUSH_TIMEOUT_SECONDS = 120
OWNER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$")
REPO_RE = re.compile(r"^[a-z0-9._-]{1,100}$")
PUSH_ID_RE = re.compile(r"^[a-f0-9]{6,64}$")
REF_RE = engine.REF_RE
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict):
        _fail("payload must be an object")
    owner = payload.get("owner")
    repo = payload.get("repo")
    push_id = payload.get("push_id")
    token = payload.get("token")
    ref_updates = payload.get("ref_updates")
    action = payload.get("action", "approve")
    if action not in ("approve", "cleanup"):
        _fail("action must be approve or cleanup")
    if not isinstance(owner, str) or not OWNER_RE.fullmatch(owner):
        _fail("owner is not a valid GitHub owner")
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        _fail("repo is not a valid GitHub repository name")
    if not isinstance(push_id, str) or not PUSH_ID_RE.fullmatch(push_id):
        _fail("push_id is invalid")
    token_value: str | None = None
    if action == "approve":
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            _fail("token is required")
        token_value = token

    mirror = engine.QUARANTINE_ROOT / owner.lower() / f"{repo.lower()}.git"
    if not (mirror / "HEAD").exists():
        _fail("quarantine mirror is missing (the held objects are gone)")

    refspecs: list[str] = []
    leases: list[str] = []
    pending_refs = _pending_refs(mirror, push_id)
    if action == "cleanup":
        for pending in pending_refs:
            _delete_pending_ref(mirror, pending)
        _ok()
        return
    if token_value is None:
        _fail("token is required")
    if not isinstance(ref_updates, list) or not ref_updates:
        _fail("ref_updates must be a non-empty array")
    for index, update in enumerate(ref_updates):
        if not isinstance(update, dict):
            _fail("ref_updates entries must be objects")
        ref = update.get("ref")
        old = update.get("old")
        new = update.get("new")
        if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
            _fail("ref_updates[].ref must be a refs/heads or refs/tags name")
        if not isinstance(old, str) or not SHA_RE.fullmatch(old):
            _fail("ref_updates[].old must be an object id")
        if not isinstance(new, str) or not SHA_RE.fullmatch(new):
            _fail("ref_updates[].new must be an object id")
        leases.append(f"--force-with-lease={ref}:{'' if old == engine.ZERO_OID else old}")
        if new == engine.ZERO_OID:
            refspecs.append(f":{ref}")
            continue
        pending = f"refs/pending/{push_id}/{index}"
        # The quarantined ref must still point at the approved sha — otherwise
        # the objects the operator reviewed are not what would be pushed.
        if _rev_parse(mirror, pending) != new:
            _fail(f"quarantined ref {pending} does not match the approved commit")
        refspecs.append(f"{pending}:{ref}")

    url = f"https://github.com/{owner}/{repo}.git"
    with engine.git_auth_env(mirror, token_value) as env:
        push = _run(_git_cmd(mirror, "push", "--atomic", *leases, url, *refspecs), env=env)
    if push.returncode != 0:
        _fail(f"git push was rejected: {push.stderr.decode('utf-8', 'replace')[:300].strip()}")
    # The push already landed, so the approval succeeded; the ref deletes are
    # housekeeping and must not turn an on-GitHub push into a failed row.
    for pending in pending_refs:
        _delete_pending_ref(mirror, pending)
    _ok()


def _pending_refs(mirror: Any, push_id: str) -> list[str]:
    result = _run(_git_cmd(mirror, "for-each-ref", "--format=%(refname)", f"refs/pending/{push_id}/"))
    if result.returncode != 0:
        _fail(f"could not list pending refs: {result.stderr.decode('utf-8', 'replace')[:300].strip()}")
    return [line for line in result.stdout.decode("utf-8", "replace").splitlines() if line]


def _rev_parse(mirror: Any, ref: str) -> str | None:
    result = _run(_git_cmd(mirror, "rev-parse", "--verify", "--quiet", ref))
    return result.stdout.decode("ascii", "replace").strip() if result.returncode == 0 else None


def _delete_pending_ref(mirror: Any, ref: str) -> None:
    # Best-effort: a ref that resists deletion stays in the proxy-private
    # quarantine mirror where it is inert (never pushed anywhere).
    _run(_git_cmd(mirror, "update-ref", "-d", ref))


def _git_cmd(mirror: Any, *args: str) -> list[str]:
    return [GIT_BIN, "-c", f"safe.directory={mirror}", "-C", str(mirror), *args]


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=PUSH_TIMEOUT_SECONDS, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail(f"git invocation failed: {exc}")


def _fail(message: str) -> NoReturn:
    print(json.dumps({"error": {"message": message}}, sort_keys=True))
    raise SystemExit(2)


def _ok() -> None:
    print(json.dumps({"ok": True}, sort_keys=True))


if __name__ == "__main__":
    main()
