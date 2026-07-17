"""The ``.github`` push-approval gate.

When a write repository has ``require_dot_github_approval`` set, a git push that
changes any ``.github/`` path is not forwarded to GitHub; it is held for
operator approval. The network proxy hands the already-buffered
``git-receive-pack`` body here. Real git (not a hand-rolled packfile parser)
decides whether ``.github/`` is touched, against a per-repo quarantine mirror
that resolves the thin pack the client sends:

- **No ``.github/`` change** -> ``forward``: the proxy sends the original body
  upstream unchanged, so clean pushes are transparent.
- **``.github/`` touched** -> ``queue``: the new objects are kept under
  ``refs/pending/<id>`` in the quarantine, a ``pending_pushes`` row is written,
  and the agent's push is answered with a synthesized git ``report-status`` that
  rejects each ref as "queued for approval". The objects are retained so an
  approved push can be replayed by the ``approve-github-push`` root helper
  without a re-push.

Everything fails closed: any git or mirror error rejects the push (the proxy
never forwards an un-inspected gated push). Only the pkt-line command list is
parsed here; pack objects are only ever read by ``git``.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Iterator
import uuid

# git's well-known empty-tree object: the base for an orphan branch with no
# common history on the remote, so every path it carries counts as introduced.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
ZERO_OID = "0" * 40
OID_RE = re.compile(r"^[0-9a-f]{40}$")
# Branch/tag refs only, no whitespace or git metacharacters. Parsing fails
# closed on anything else; the authoritative check stays at the root helper,
# whose `git push` rejects any ref name git itself would refuse.
REF_RE = re.compile(r"^refs/(heads|tags)/[^\s?*:\[\\~^]+$")
GIT_BIN = "/usr/bin/git"
GATE_TIMEOUT_SECONDS = 120
MAX_CHANGED_PATHS = 200
CHANGED_PATHS_TRUNCATED = ".github/... (additional paths omitted)"
# The quarantine mirrors live under the proxy's own state directory (proxy-owned,
# root-readable so the approval helper can replay from them).
QUARANTINE_ROOT = Path("/mnt/trustyclaw-admin/proxy-state/github-quarantine")


class GateError(Exception):
    """Inspection could not complete; the caller fails closed."""


def parse_receive_pack(body: bytes) -> tuple[list[tuple[str, str, str]], set[str], bytes]:
    """Split a ``git-receive-pack`` request body into its command list, the
    client capabilities, and the trailing packfile bytes.

    Each command pkt-line is ``<oldsha> <newsha> <ref>[\\0capabilities]``; a
    ``0000`` flush-pkt ends the command list and the packfile follows. Raises
    ``GateError`` on malformed framing (fail closed)."""
    commands: list[tuple[str, str, str]] = []
    capabilities: set[str] = set()
    offset = 0
    total = len(body)
    while offset + 4 <= total:
        try:
            length = int(body[offset : offset + 4], 16)
        except ValueError as exc:
            raise GateError("receive-pack framing is not pkt-line") from exc
        if length == 0:  # flush-pkt: the packfile starts after it
            return commands, capabilities, body[offset + 4 :]
        if length < 4 or offset + length > total:
            raise GateError("receive-pack pkt-line length is out of range")
        payload = body[offset + 4 : offset + length]
        offset += length
        head, _, caps = payload.partition(b"\x00")
        if caps and not commands:
            capabilities = set(caps.decode("utf-8", "replace").split())
        fields = head.rstrip(b"\n").split(b" ")
        if len(fields) >= 3:
            old = fields[0].decode("ascii", "replace")
            new = fields[1].decode("ascii", "replace")
            if not OID_RE.fullmatch(old) or not OID_RE.fullmatch(new):
                raise GateError("receive-pack command has invalid object id")
            ref = fields[2].decode("utf-8", "replace")
            if not valid_ref_name(ref):
                raise GateError("receive-pack command has invalid ref name")
            commands.append((old, new, ref))
    raise GateError("receive-pack command list has no flush-pkt")


def valid_ref_name(ref: str) -> bool:
    return REF_RE.fullmatch(ref) is not None


def touches_github(paths: set[str]) -> bool:
    return any(path == ".github" or path.startswith(".github/") for path in paths)


def _pkt(payload: bytes) -> bytes:
    return b"%04x" % (len(payload) + 4) + payload


def build_report_status(refs: list[str], message: str, *, side_band: bool) -> bytes:
    """A git ``report-status`` that accepts the pack (``unpack ok`` — it did
    index cleanly into the quarantine) but rejects every ref with ``message``,
    so the agent's ``git push`` fails per-ref with an actionable reason. Wrapped
    in side-band channel 1 when the client advertised ``side-band-64k``."""
    report = _pkt(b"unpack ok\n")
    for ref in refs:
        report += _pkt(b"ng " + ref.encode("utf-8") + b" " + message.encode("utf-8") + b"\n")
    report += b"0000"
    if not side_band:
        return report
    wrapped = b""
    for start in range(0, len(report), 65515):
        wrapped += _pkt(b"\x01" + report[start : start + 65515])
    return wrapped + b"0000"


def build_http_response(report_status: bytes) -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/x-git-receive-pack-result\r\n"
        b"Connection: close\r\n"
        b"Content-Length: " + str(len(report_status)).encode() + b"\r\n\r\n" + report_status
    )


def _run_git(
    mirror: Path,
    args: list[str],
    *,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
) -> bytes:
    try:
        proc = subprocess.run(
            [GIT_BIN, "-C", str(mirror), *args],
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GATE_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError(f"git {args[0]} failed: {exc}") from exc
    if proc.returncode != 0:
        raise GateError(f"git {args[0]} exited {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[:200]}")
    return proc.stdout


@contextmanager
def git_auth_env(work_dir: Path, token: str | None) -> Iterator[dict[str, str]]:
    """GitHub HTTPS auth without putting the token in argv."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if not token:
        yield env
        return
    work_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=work_dir,
        prefix=".git-askpass-",
        delete=False,
    )
    try:
        path = Path(handle.name)
        os.chmod(path, 0o700)
        escaped = token.replace("'", "'\"'\"'")
        handle.write("#!/bin/sh\n")
        handle.write("case \"$1\" in\n")
        handle.write("  *Username*) printf '%s\\n' 'x-access-token' ;;\n")
        handle.write(f"  *) printf '%s\\n' '{escaped}' ;;\n")
        handle.write("esac\n")
        handle.close()
        env["GIT_ASKPASS"] = str(path)
        yield env
    finally:
        try:
            handle.close()
        except Exception:
            pass
        try:
            Path(handle.name).unlink()
        except OSError:
            pass


def ensure_mirror(owner: str, repo: str, token: str | None, *, root: Path = QUARANTINE_ROOT) -> Path:
    """A bare quarantine mirror kept current with GitHub, so the thin pack a
    client pushes can be resolved and diffed. Fetched with the working token
    (private repos); the proxy is the one non-root uid with egress, so this
    direct fetch is allowed by nftables."""
    mirror = root / owner.lower() / f"{repo.lower()}.git"
    url = f"https://github.com/{owner}/{repo}.git"
    if not (mirror / "HEAD").exists():
        mirror.mkdir(parents=True, exist_ok=True)
        _run_git(mirror, ["init", "--bare", "--quiet"])
        _run_git(mirror, ["config", "transfer.fsckObjects", "true"])
    # Fetch every branch head so bases for the thin pack (and the old commits to
    # diff against) are present. Failures fail closed at the caller.
    with git_auth_env(mirror, token) as env:
        _run_git(
            mirror,
            [
                "fetch",
                "--quiet",
                "--prune",
                url,
                "+refs/heads/*:refs/heads/*",
                "+refs/tags/*:refs/tags/*",
            ],
            env=env,
        )
    return mirror


def changed_paths(mirror: Path, commands: list[tuple[str, str, str]], pack: bytes) -> set[str]:
    """Index the pushed pack into the quarantine mirror (resolving the thin
    pack) and return capped ``.github`` paths the ref updates *introduce*
    relative to what is already on the remote. A clean (forwarded) push leaves
    its objects unreachable in the proxy-private mirror until git's own gc
    during a later fetch reclaims them — disk-only, on a path no agent can
    read."""
    if pack:
        _run_git(mirror, ["index-pack", "--stdin", "--fix-thin"], stdin=pack)
    paths: set[str] = set()
    for old, new, _ref in commands:
        if new == ZERO_OID:  # branch deletion introduces no content
            continue
        bases = _remote_ancestor_bases(mirror, old, new)
        if bases is None:  # tip is already on the remote; nothing introduced
            continue
        # A ref update or a branch off a single remote head has one base, giving
        # an exact answer. When a new branch merges several remote heads there is
        # no single authoritative base, so we fail closed: any .github difference
        # against any base holds the push, rather than guessing which merged
        # remote state is canonical. Code-only merges still diff clean.
        for base in bases:
            paths |= _github_diff(mirror, base, new)
    if len(paths) > MAX_CHANGED_PATHS:
        # Every path is under .github/, so the hold decision was made the
        # moment the set went non-empty; the cap only bounds the operator
        # display list stored on the pending row.
        paths = set(sorted(paths)[:MAX_CHANGED_PATHS]) | {CHANGED_PATHS_TRUNCATED}
    return paths


def _remote_ancestor_bases(mirror: Path, old: str, new: str) -> list[str] | None:
    """The commits to diff ``new`` against so only the ``.github`` changes this
    push *introduces* are seen -- files already on the remote don't count.

    For a ref update that base is the ref's prior tip (``old``). For a new
    branch (``old`` all-zeros) it is the set of already-present commits the
    branch was built on: ``new``'s boundary against the remote heads and tags.
    A branch that merely carries the repo's existing ``.github/`` then diffs
    clean. Returns ``None`` when ``new`` adds no new commits at all (its tip is
    already on the remote), and ``[EMPTY_TREE]`` for an orphan history with no
    common ancestor (everything it carries is genuinely new)."""
    if old != ZERO_OID:
        return [old]
    # Exclude only real remote refs (heads/tags), never our own refs/pending/*
    # quarantine, so a re-push of already-held objects is still inspected.
    out = _run_git(
        mirror,
        ["rev-list", "--boundary", new, "--not", "--branches", "--tags"],
    ).decode("utf-8", "replace")
    introduced = False
    bases: list[str] = []
    for line in out.splitlines():
        if not line:
            continue
        if line.startswith("-"):
            bases.append(line[1:])
        else:
            introduced = True
    if not introduced:
        return None
    return bases or [EMPTY_TREE]


def _github_diff(mirror: Path, base: str, new: str) -> set[str]:
    """The ``.github`` paths that differ between ``base`` and ``new``. A
    diff-tree without ``--exit-code`` simply prints nothing when there is no
    difference, so the one listing call is authoritative."""
    output = _run_git(
        mirror,
        ["diff-tree", "-r", "--no-commit-id", "--name-only", base, new, "--", ".github"],
    )
    return {line for line in output.decode("utf-8", "replace").splitlines() if line}


def quarantine_pending(mirror: Path, commands: list[tuple[str, str, str]], push_id: str) -> None:
    """Retain the pushed tips under ``refs/pending/<id>/...`` so an approved
    push can be replayed without the agent re-pushing."""
    for index, (_old, new, _ref) in enumerate(commands):
        if new != ZERO_OID:
            _run_git(mirror, ["update-ref", f"refs/pending/{push_id}/{index}", new])


def cleanup_pending(mirror: Path, push_id: str) -> None:
    refs = _run_git(mirror, ["for-each-ref", "--format=%(refname)", f"refs/pending/{push_id}/"])
    for ref in refs.decode("utf-8", "replace").splitlines():
        if ref:
            _run_git(mirror, ["update-ref", "-d", ref])


def new_push_id() -> str:
    return uuid.uuid4().hex[:12]


def side_band_requested(capabilities: set[str]) -> bool:
    return "side-band-64k" in capabilities or "side-band" in capabilities


class GateResult:
    """The outcome of inspecting one gated push, ready for the proxy to act on:
    forward the original body when nothing under ``.github/`` changed, or hold it
    for approval (quarantine the objects, enqueue a row, answer the agent) when
    it did."""

    def __init__(
        self, mirror: Path, commands: list[tuple[str, str, str]], side_band: bool, paths: set[str]
    ) -> None:
        self._mirror = mirror
        self._commands = commands
        self._side_band = side_band
        self.paths = paths
        self.refs = [ref for _old, _new, ref in commands]
        self.ref_updates = [{"old": old, "new": new, "ref": ref} for old, new, ref in commands]

    @property
    def touches_github(self) -> bool:
        return touches_github(self.paths)

    def hold_for_approval(self, push_id: str) -> bytes:
        """Retain the already-indexed objects and return the HTTP response
        that tells the agent the push is queued."""
        try:
            quarantine_pending(self._mirror, self._commands, push_id)
            report = build_report_status(self.refs, f"queued for approval as push-{push_id}", side_band=self._side_band)
            return build_http_response(report)
        except Exception:
            try:
                cleanup_pending(self._mirror, push_id)
            except Exception:
                pass
            raise

    def cleanup_pending(self, push_id: str) -> None:
        cleanup_pending(self._mirror, push_id)


def inspect(owner: str, repo: str, body: bytes, token: str | None) -> GateResult:
    """Parse the push, refresh the quarantine mirror, and diff the ref updates.
    Raises ``GateError`` (fail closed) on any framing, mirror, or git failure."""
    commands, capabilities, pack = parse_receive_pack(body)
    mirror = ensure_mirror(owner, repo, token)
    paths = changed_paths(mirror, commands, pack)
    return GateResult(mirror, commands, side_band_requested(capabilities), paths)
