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
import select
import subprocess
import tempfile
import time
from typing import Iterator
import uuid

# git's well-known empty-tree object: the base for an orphan branch with no
# common history on the remote, so every path it carries counts as introduced.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
ZERO_OID = "0" * 40
OID_RE = re.compile(r"^[0-9a-f]{40}$")
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
    if not ref.startswith(("refs/heads/", "refs/tags/")):
        return False
    try:
        result = subprocess.run(
            [GIT_BIN, "check-ref-format", ref],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=GATE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError(f"git check-ref-format failed: {exc}") from exc
    return result.returncode == 0


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


def _git_exit_code(mirror: Path, args: list[str], *, env: dict[str, str] | None = None) -> int:
    try:
        proc = subprocess.run(
            [GIT_BIN, "-C", str(mirror), *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=GATE_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError(f"git {args[0]} failed: {exc}") from exc
    return proc.returncode


def _run_git_lines_capped(
    mirror: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    max_lines: int = MAX_CHANGED_PATHS,
) -> tuple[list[str], bool]:
    try:
        proc = subprocess.Popen(
            [GIT_BIN, "-C", str(mirror), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise GateError(f"git {args[0]} failed: {exc}") from exc
    assert proc.stdout is not None
    lines: list[str] = []
    truncated = False
    deadline = time.monotonic() + GATE_TIMEOUT_SECONDS
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.communicate()
                raise GateError(f"git {args[0]} timed out")
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                proc.kill()
                proc.communicate()
                raise GateError(f"git {args[0]} timed out")
            raw = proc.stdout.readline()
            if raw == b"":
                break
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line:
                lines.append(line)
            if len(lines) > max_lines:
                truncated = True
                proc.kill()
                break
        _stdout, stderr = proc.communicate(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise GateError(f"git {args[0]} timed out") from exc
    if not truncated and proc.returncode != 0:
        raise GateError(f"git {args[0]} exited {proc.returncode}: {stderr.decode('utf-8', 'replace')[:200]}")
    return lines[:max_lines], truncated


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


def _git_object_env(object_dir: Path, alternates: list[Path]) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_OBJECT_DIRECTORY"] = str(object_dir)
    if alternates:
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = os.pathsep.join(str(path) for path in alternates)
    return env


def changed_paths(mirror: Path, commands: list[tuple[str, str, str]], pack: bytes) -> set[str]:
    """Index the pushed pack into the quarantine (resolving the thin pack against
    the mirror) and return capped ``.github`` paths the ref updates *introduce*
    relative to what is already on the remote."""
    if not pack:
        return _changed_paths_with_env(mirror, commands, None)
    with tempfile.TemporaryDirectory(prefix="inspect-objects.", dir=mirror.parent) as tmp:
        object_dir = Path(tmp) / "objects"
        object_dir.mkdir()
        env = _git_object_env(object_dir, [mirror / "objects"])
        _run_git(mirror, ["index-pack", "--stdin", "--fix-thin"], stdin=pack, env=env)
        return _changed_paths_with_env(mirror, commands, env)


def _remote_ancestor_bases(
    mirror: Path, old: str, new: str, env: dict[str, str] | None
) -> list[str] | None:
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
        env=env,
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


def _github_diff(
    mirror: Path, base: str, new: str, env: dict[str, str] | None, max_lines: int
) -> tuple[set[str], bool]:
    """The ``.github`` paths that differ between ``base`` and ``new`` (capped),
    and whether the listing was truncated."""
    quiet = _git_exit_code(mirror, ["diff-tree", "--quiet", base, new, "--", ".github"], env=env)
    if quiet == 0:
        return set(), False
    if quiet != 1:
        raise GateError(f"git diff-tree exited {quiet}")
    output, truncated = _run_git_lines_capped(
        mirror,
        ["diff-tree", "-r", "--no-commit-id", "--name-only", base, new, "--", ".github"],
        env=env,
        max_lines=max_lines,
    )
    return set(output), truncated


def _changed_paths_with_env(
    mirror: Path, commands: list[tuple[str, str, str]], env: dict[str, str] | None
) -> set[str]:
    paths: set[str] = set()
    for old, new, _ref in commands:
        if new == ZERO_OID:  # branch deletion introduces no content
            continue
        bases = _remote_ancestor_bases(mirror, old, new, env)
        if bases is None:  # tip is already on the remote; nothing introduced
            continue
        remaining = MAX_CHANGED_PATHS - len(paths)
        if remaining <= 0:
            paths.add(CHANGED_PATHS_TRUNCATED)
            break
        # A ref update or a branch off a single remote head has one base, giving
        # an exact answer. When a new branch merges several remote heads there is
        # no single authoritative base, so we fail closed: any .github difference
        # against any base holds the push, rather than guessing which merged
        # remote state is canonical. Code-only merges still diff clean.
        changed, truncated = _github_diff(mirror, bases[0], new, env, remaining)
        for base in bases[1:]:
            other, other_truncated = _github_diff(mirror, base, new, env, remaining)
            changed |= other
            truncated = truncated or other_truncated
        if len(changed) > remaining:
            truncated = True
            changed = set(sorted(changed)[:remaining])
        if truncated and not changed:
            # Capping left the result unreliable; fail toward review.
            changed = {CHANGED_PATHS_TRUNCATED}
        paths.update(changed)
        if truncated:
            paths.add(CHANGED_PATHS_TRUNCATED)
            break
    return paths


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


def should_inspect(guard: dict, host: str, method: str, path: str) -> tuple[str, str] | None:
    """The (owner, repo) to gate, or None when this request is not a gated push.
    A gated push is a ``POST .../git-receive-pack`` to a write repository while
    the integration has ``require_dot_github_approval`` set."""
    if not isinstance(guard, dict) or guard.get("require_dot_github_approval") is not True:
        return None
    if host.lower() != "github.com" or method.upper() != "POST":
        return None
    parts = [part for part in path.split("/") if part]
    if len(parts) < 3 or parts[2] != "git-receive-pack":
        return None
    owner, repo = parts[0].lower(), parts[1].removesuffix(".git").lower()
    for item in guard.get("write_repositories") or []:
        if (
            isinstance(item, dict)
            and str(item.get("owner", "")).lower() == owner
            and str(item.get("repo", "")).removesuffix(".git").lower() == repo
        ):
            return owner, repo
    return None


class GateResult:
    """The outcome of inspecting one gated push, ready for the proxy to act on:
    forward the original body when nothing under ``.github/`` changed, or hold it
    for approval (quarantine the objects, enqueue a row, answer the agent) when
    it did."""

    def __init__(
        self, mirror: Path, commands: list[tuple[str, str, str]], side_band: bool, paths: set[str], pack: bytes
    ) -> None:
        self._mirror = mirror
        self._commands = commands
        self._side_band = side_band
        self._pack = pack
        self.paths = paths
        self.refs = [ref for _old, _new, ref in commands]
        self.ref_updates = [{"old": old, "new": new, "ref": ref} for old, new, ref in commands]

    @property
    def touches_github(self) -> bool:
        return touches_github(self.paths)

    def hold_for_approval(self, push_id: str) -> bytes:
        """Retain the objects and return the HTTP response that tells the agent
        the push is queued."""
        try:
            if self._pack:
                _run_git(self._mirror, ["index-pack", "--stdin", "--fix-thin"], stdin=self._pack)
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

    def rejection(self, message: str) -> bytes:
        return build_http_response(build_report_status(self.refs, message, side_band=self._side_band))


def inspect(owner: str, repo: str, body: bytes, token: str | None) -> GateResult:
    """Parse the push, refresh the quarantine mirror, and diff the ref updates.
    Raises ``GateError`` (fail closed) on any framing, mirror, or git failure."""
    commands, capabilities, pack = parse_receive_pack(body)
    mirror = ensure_mirror(owner, repo, token)
    paths = changed_paths(mirror, commands, pack)
    return GateResult(mirror, commands, side_band_requested(capabilities), paths, pack)
