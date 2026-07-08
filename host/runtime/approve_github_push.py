"""Root helper behind ``approve-github-push``.

The ``.github`` push-approval gate quarantines a held push's objects under
``refs/pending/<id>/<n>`` in the proxy's mirror. When the operator approves, the
admin service (which has no egress) calls this helper to replay the push to
GitHub: root has egress and the working token, and reads the proxy-owned mirror.
It runs a single ``git push`` of every held ref, then drops the pending refs so
``git gc`` can reclaim them. If an earlier approve request landed on GitHub but
died before the database row was marked approved, a later retry verifies the
remote refs already match the reviewed OIDs and treats that as success.

Input (stdin JSON):
``{"action": "approve", "owner": "...", "repo": "...", "push_id": "...",
   "token": "...",
   "ref_updates": [{"old": "<oid>", "new": "<oid>", "ref": "refs/heads/..."}, ...]}``
or ``{"action": "cleanup", ...}`` to delete pending refs after rejection.

Output: ``{"ok": true}`` or ``{"error": {"message": "...", "code": "..."}}``
(exit 2). A non-fast-forward or rejected ref surfaces as an error rather than
being forced.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any, NoReturn

from host.runtime import github_push_gate

GIT_BIN = "/usr/bin/git"
PUSH_TIMEOUT_SECONDS = 120
OWNER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$")
REPO_RE = re.compile(r"^[a-z0-9._-]{1,100}$")
PUSH_ID_RE = re.compile(r"^[a-f0-9]{6,64}$")
REF_RE = re.compile(r"^refs/(heads|tags)/[^\s?*:\[\\~^]+$")
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
    if not isinstance(ref_updates, list) or not ref_updates:
        _fail("ref_updates must be a non-empty array")

    mirror = github_push_gate.QUARANTINE_ROOT / owner.lower() / f"{repo.lower()}.git"
    if not (mirror / "HEAD").exists():
        _fail("quarantine mirror is missing (the held objects are gone)")

    refspecs: list[str] = []
    leases: list[str] = []
    checked_updates: list[dict[str, str]] = []
    pending_refs = _pending_refs(mirror, push_id)
    if action == "cleanup":
        for pending in pending_refs:
            _delete_pending_ref(mirror, pending)
        _ok()
        return
    if token_value is None:
        _fail("token is required")
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
        checked_updates.append({"old": old, "new": new, "ref": ref})
    for index, update in enumerate(checked_updates):
        ref = update["ref"]
        old = update["old"]
        new = update["new"]
        leases.append(f"--force-with-lease={ref}:{'' if old == github_push_gate.ZERO_OID else old}")
        if new == github_push_gate.ZERO_OID:
            refspecs.append(f":{ref}")
            continue
        pending = f"refs/pending/{push_id}/{index}"
        # The quarantined ref must still point at the approved sha — otherwise
        # the objects the operator reviewed are not what would be pushed.
        if _rev_parse(mirror, pending) != new:
            if _remote_refs_match(mirror, owner, repo, checked_updates, token_value):
                _cleanup_landed_pending_refs(mirror, pending_refs)
                _ok(already_landed=True)
                return
            _fail(f"quarantined ref {pending} does not match the approved commit")
        refspecs.append(f"{pending}:{ref}")

    if action == "approve":
        url = f"https://github.com/{owner}/{repo}.git"
        with github_push_gate.git_auth_env(mirror, token_value) as env:
            push = _run(_git_cmd(mirror, "push", "--atomic", *leases, url, *refspecs), env=env)
        if push.returncode != 0:
            if _remote_refs_match(mirror, owner, repo, checked_updates, token_value):
                _cleanup_landed_pending_refs(mirror, pending_refs)
                _ok(already_landed=True)
                return
            _fail(f"git push was rejected: {push.stderr.decode('utf-8', 'replace')[:300].strip()}")
    for pending in pending_refs:
        _delete_pending_ref(mirror, pending, failure_code="cleanup_after_push")
    _ok()


def _pending_refs(mirror: Any, push_id: str) -> list[str]:
    result = _run(_git_cmd(mirror, "for-each-ref", "--format=%(refname)", f"refs/pending/{push_id}/"))
    if result.returncode != 0:
        _fail(f"could not list pending refs: {result.stderr.decode('utf-8', 'replace')[:300].strip()}")
    return [line for line in result.stdout.decode("utf-8", "replace").splitlines() if line]


def _rev_parse(mirror: Any, ref: str) -> str | None:
    result = _run(_git_cmd(mirror, "rev-parse", "--verify", "--quiet", ref))
    return result.stdout.decode("ascii", "replace").strip() if result.returncode == 0 else None


def _delete_pending_ref(mirror: Any, ref: str, *, failure_code: str | None = None) -> None:
    result = _run(_git_cmd(mirror, "update-ref", "-d", ref))
    if result.returncode != 0:
        _fail(
            f"could not delete pending ref {ref}: {result.stderr.decode('utf-8', 'replace')[:300].strip()}",
            code=failure_code,
        )
    if _rev_parse(mirror, ref) is not None:
        _fail(f"pending ref {ref} still exists after cleanup", code=failure_code)


def _cleanup_landed_pending_refs(mirror: Any, pending_refs: list[str]) -> None:
    for pending in pending_refs:
        _delete_pending_ref(mirror, pending, failure_code="cleanup_after_push")


def _remote_refs_match(
    mirror: Any,
    owner: str,
    repo: str,
    ref_updates: list[dict[str, str]],
    token: str,
) -> bool:
    refs = [update["ref"] for update in ref_updates]
    if not refs:
        return False
    url = f"https://github.com/{owner}/{repo}.git"
    with github_push_gate.git_auth_env(mirror, token) as env:
        result = _run(_git_cmd(mirror, "ls-remote", url, *refs), env=env)
    if result.returncode != 0:
        return False
    remote: dict[str, str] = {}
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        oid, _, ref = line.partition("\t")
        if SHA_RE.fullmatch(oid) and ref:
            remote[ref] = oid
    for update in ref_updates:
        expected = update["new"]
        actual = remote.get(update["ref"])
        if expected == github_push_gate.ZERO_OID:
            if actual is not None:
                return False
        elif actual != expected:
            return False
    return True


def _git_cmd(mirror: Any, *args: str) -> list[str]:
    return [GIT_BIN, "-c", f"safe.directory={mirror}", "-C", str(mirror), *args]


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=PUSH_TIMEOUT_SECONDS, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail(f"git invocation failed: {exc}")


def _fail(message: str, *, code: str | None = None) -> NoReturn:
    error = {"message": message}
    if code is not None:
        error["code"] = code
    print(json.dumps({"error": error}, sort_keys=True))
    raise SystemExit(2)


def _ok(*, already_landed: bool = False) -> None:
    value: dict[str, bool] = {"ok": True}
    if already_landed:
        value["already_landed"] = True
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    main()
