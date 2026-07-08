"""Unit tests for the .github push-approval gate core (host.runtime.github_push_gate).

The pkt-line parsing and report-status synthesis are pure; the change detection
runs real git against temp repositories, so it validates the actual index-pack +
diff-tree path the proxy uses (no live GitHub needed).
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from host.runtime import approve_github_push, github_push_gate as gate


def _pkt(payload: bytes) -> bytes:
    return b"%04x" % (len(payload) + 4) + payload


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(cwd)},
    )
    return proc.stdout


class PktLineTests(unittest.TestCase):
    def test_parse_receive_pack_extracts_commands_caps_and_pack(self) -> None:
        zero, one = "0" * 40, "1" * 40
        body = (
            _pkt(f"{zero} {one} refs/heads/main\x00report-status side-band-64k".encode())
            + _pkt(f"{one} {zero} refs/heads/old".encode())
            + b"0000PACKbinary"
        )
        with patch.object(gate, "valid_ref_name", return_value=True):
            commands, caps, pack = gate.parse_receive_pack(body)
        self.assertEqual(commands, [(zero, one, "refs/heads/main"), (one, zero, "refs/heads/old")])
        self.assertIn("side-band-64k", caps)
        self.assertTrue(gate.side_band_requested(caps))
        self.assertEqual(pack, b"PACKbinary")

    def test_parse_receive_pack_fails_closed_on_malformed(self) -> None:
        for bad in (b"nothex", b"0005", _pkt(b"0" * 40 + b" x y")):  # no flush-pkt
            with self.subTest(body=bad), self.assertRaises(gate.GateError):
                gate.parse_receive_pack(bad)

    def test_parse_receive_pack_rejects_non_oid_commands(self) -> None:
        body = _pkt(f"{'0' * 40} --output=/tmp/x refs/heads/main".encode()) + b"0000PACK"
        with self.assertRaises(gate.GateError):
            gate.parse_receive_pack(body)

    def test_parse_receive_pack_rejects_invalid_ref_names(self) -> None:
        for ref in ("refs/heads/bad?ref", "refs/heads/foo..bar", "refs/heads/foo.lock", "refs/heads/@{x}"):
            with self.subTest(ref=ref):
                body = _pkt(f"{'0' * 40} {'1' * 40} {ref}".encode()) + b"0000PACK"
                with patch.object(gate, "valid_ref_name", return_value=False), self.assertRaises(gate.GateError):
                    gate.parse_receive_pack(body)

    def test_touches_github(self) -> None:
        self.assertTrue(gate.touches_github({".github/workflows/ci.yml"}))
        self.assertTrue(gate.touches_github({".github"}))
        self.assertFalse(gate.touches_github({"src/.github/x", "README.md"}))

    def test_build_report_status_plain_and_sideband(self) -> None:
        plain = gate.build_report_status(["refs/heads/main"], "queued as push-abc", side_band=False)
        self.assertTrue(plain.startswith(_pkt(b"unpack ok\n")))
        self.assertIn(b"ng refs/heads/main queued as push-abc\n", plain)
        self.assertTrue(plain.endswith(b"0000"))
        wrapped = gate.build_report_status(["refs/heads/main"], "queued as push-abc", side_band=True)
        # Side-band: the report rides band 1 (\x01) and the stream ends flush.
        self.assertIn(b"\x01", wrapped)
        self.assertTrue(wrapped.endswith(b"0000"))
        self.assertEqual(gate.build_http_response(plain).split(b"\r\n\r\n", 1)[1], plain)


@unittest.skipIf(shutil.which("git") is None, "git binary is required for change-detection tests")
class ChangeDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # A bare "mirror" holding commit A, and a work tree that adds commit B.
        self.mirror = self.root / "mirror.git"
        _git(self.root, "init", "--bare", "--quiet", str(self.mirror))
        self.work = self.root / "work"
        _git(self.root, "clone", "--quiet", str(self.mirror), str(self.work))
        (self.work / "README.md").write_text("hello\n")
        _git(self.work, "add", "README.md")
        _git(self.work, "commit", "--quiet", "-m", "A")
        _git(self.work, "branch", "-M", "main")
        _git(self.work, "push", "--quiet", "origin", "main")
        self.old = _git(self.work, "rev-parse", "HEAD").decode().strip()

    def _pack_for_head(self) -> bytes:
        objects = _git(self.work, "rev-list", "--objects", "HEAD")
        return _git(self.work, "pack-objects", "--stdout", stdin=objects)

    def test_detects_github_change(self) -> None:
        wf = self.work / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("on: push\n")
        _git(self.work, "add", ".github/workflows/ci.yml")
        _git(self.work, "commit", "--quiet", "-m", "B: add workflow")
        new = _git(self.work, "rev-parse", "HEAD").decode().strip()
        pack = self._pack_for_head()
        paths = gate.changed_paths(self.mirror, [(self.old, new, "refs/heads/main")], pack)
        self.assertIn(".github/workflows/ci.yml", paths)
        self.assertTrue(gate.touches_github(paths))

    def test_valid_ref_name_matches_git_rules(self) -> None:
        self.assertTrue(gate.valid_ref_name("refs/heads/main"))
        for ref in ("refs/heads/bad?ref", "refs/heads/foo..bar", "refs/heads/foo.lock", "refs/heads/@{x}"):
            with self.subTest(ref=ref):
                self.assertFalse(gate.valid_ref_name(ref))

    def test_clean_push_does_not_touch_github(self) -> None:
        (self.work / "src.py").write_text("print(1)\n")
        _git(self.work, "add", "src.py")
        _git(self.work, "commit", "--quiet", "-m", "B: code only")
        new = _git(self.work, "rev-parse", "HEAD").decode().strip()
        pack = self._pack_for_head()
        paths = gate.changed_paths(self.mirror, [(self.old, new, "refs/heads/main")], pack)
        self.assertEqual(paths, set())
        self.assertFalse(gate.touches_github(paths))
        missing = subprocess.run(
            ["git", "-C", str(self.mirror), "cat-file", "-e", f"{new}^{{commit}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(missing.returncode, 0)

    def test_new_branch_flags_github_added_since_the_branch_point(self) -> None:
        # A brand-new branch (old = zeros) is diffed against its remote branch
        # point, so a .github file the branch itself adds is flagged.
        wf = self.work / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("on: push\n")
        (self.work / "src.py").write_text("print(1)\n")
        _git(self.work, "add", ".github/workflows/ci.yml", "src.py")
        _git(self.work, "commit", "--quiet", "-m", "B: branch contents")
        new = _git(self.work, "rev-parse", "HEAD").decode().strip()
        pack = self._pack_for_head()
        paths = gate.changed_paths(self.mirror, [(gate.ZERO_OID, new, "refs/heads/feature")], pack)
        self.assertEqual(paths, {".github/workflows/ci.yml"})

    def test_new_branch_carrying_preexisting_github_is_clean(self) -> None:
        # main already has a workflow; a new branch that merely carries it and
        # changes only code must NOT be gated -- the file was already there.
        wf = self.work / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("on: push\n")
        _git(self.work, "add", ".github/workflows/ci.yml")
        _git(self.work, "commit", "--quiet", "-m", "add workflow to main")
        _git(self.work, "push", "--quiet", "origin", "main")
        _git(self.work, "checkout", "--quiet", "-b", "feature")
        (self.work / "src.py").write_text("print(1)\n")
        _git(self.work, "add", "src.py")
        _git(self.work, "commit", "--quiet", "-m", "feature: code only")
        new = _git(self.work, "rev-parse", "HEAD").decode().strip()
        pack = self._pack_for_head()
        paths = gate.changed_paths(self.mirror, [(gate.ZERO_OID, new, "refs/heads/feature")], pack)
        self.assertEqual(paths, set())
        self.assertFalse(gate.touches_github(paths))

    def test_new_branch_editing_preexisting_github_is_flagged(self) -> None:
        # Editing a workflow that is already on main IS a .github change.
        wf = self.work / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("on: push\n")
        _git(self.work, "add", ".github/workflows/ci.yml")
        _git(self.work, "commit", "--quiet", "-m", "add workflow to main")
        _git(self.work, "push", "--quiet", "origin", "main")
        _git(self.work, "checkout", "--quiet", "-b", "feature")
        (wf / "ci.yml").write_text("on: [push, pull_request]\n")
        _git(self.work, "add", ".github/workflows/ci.yml")
        _git(self.work, "commit", "--quiet", "-m", "feature: edit workflow")
        new = _git(self.work, "rev-parse", "HEAD").decode().strip()
        pack = self._pack_for_head()
        paths = gate.changed_paths(self.mirror, [(gate.ZERO_OID, new, "refs/heads/feature")], pack)
        self.assertEqual(paths, {".github/workflows/ci.yml"})

    def test_new_branch_merging_remote_github_change_is_held(self) -> None:
        # A new branch that merges several remote heads has no single
        # authoritative base, so we fail closed: any .github difference against
        # any base holds the push instead of guessing which merged state wins.
        _git(self.work, "checkout", "--quiet", "-b", "feature")
        (self.work / "src.py").write_text("print(1)\n")
        _git(self.work, "add", "src.py")
        _git(self.work, "commit", "--quiet", "-m", "feature: code")
        _git(self.work, "checkout", "--quiet", "main")
        wf = self.work / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("on: push\n")
        _git(self.work, "add", ".github/workflows/ci.yml")
        _git(self.work, "commit", "--quiet", "-m", "main: add workflow")
        _git(self.work, "push", "--quiet", "origin", "main")
        _git(self.work, "checkout", "--quiet", "feature")
        _git(self.work, "merge", "--quiet", "--no-ff", "--no-edit", "main")
        new = _git(self.work, "rev-parse", "HEAD").decode().strip()
        pack = self._pack_for_head()
        paths = gate.changed_paths(self.mirror, [(gate.ZERO_OID, new, "refs/heads/feature")], pack)
        self.assertEqual(paths, {".github/workflows/ci.yml"})
        self.assertTrue(gate.touches_github(paths))

    def test_new_branch_merging_remote_without_github_change_is_clean(self) -> None:
        # A code-only branch that merges a remote head (no .github change on
        # either side) still diffs clean -- the common "merged main" workflow is
        # not over-blocked by the multi-base fail-closed rule.
        _git(self.work, "checkout", "--quiet", "-b", "feature")
        (self.work / "src.py").write_text("print(1)\n")
        _git(self.work, "add", "src.py")
        _git(self.work, "commit", "--quiet", "-m", "feature: code")
        _git(self.work, "checkout", "--quiet", "main")
        (self.work / "other.py").write_text("print(2)\n")
        _git(self.work, "add", "other.py")
        _git(self.work, "commit", "--quiet", "-m", "main: more code")
        _git(self.work, "push", "--quiet", "origin", "main")
        _git(self.work, "checkout", "--quiet", "feature")
        _git(self.work, "merge", "--quiet", "--no-ff", "--no-edit", "main")
        new = _git(self.work, "rev-parse", "HEAD").decode().strip()
        pack = self._pack_for_head()
        paths = gate.changed_paths(self.mirror, [(gate.ZERO_OID, new, "refs/heads/feature")], pack)
        self.assertEqual(paths, set())
        self.assertFalse(gate.touches_github(paths))

    def test_new_branch_deleting_github_across_bases_is_flagged(self) -> None:
        # main has a workflow; an older remote branch never had it. A new branch
        # merges that older branch AND deletes the workflow. The deletion differs
        # from main's base, so the multi-base union holds the push for approval.
        legacy_base = self.old
        wf = self.work / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("on: push\n")
        _git(self.work, "add", ".github/workflows/ci.yml")
        _git(self.work, "commit", "--quiet", "-m", "main: add workflow")
        _git(self.work, "push", "--quiet", "origin", "main")
        _git(self.work, "checkout", "--quiet", "-b", "legacy", legacy_base)
        (self.work / "legacy.txt").write_text("old\n")
        _git(self.work, "add", "legacy.txt")
        _git(self.work, "commit", "--quiet", "-m", "legacy: unrelated")
        _git(self.work, "push", "--quiet", "origin", "legacy")
        _git(self.work, "checkout", "--quiet", "main")
        _git(self.work, "checkout", "--quiet", "-b", "feature")
        _git(self.work, "merge", "--quiet", "--no-ff", "--no-edit", "legacy")
        _git(self.work, "rm", "--quiet", ".github/workflows/ci.yml")
        _git(self.work, "commit", "--quiet", "-m", "feature: drop workflow")
        new = _git(self.work, "rev-parse", "HEAD").decode().strip()
        pack = self._pack_for_head()
        paths = gate.changed_paths(self.mirror, [(gate.ZERO_OID, new, "refs/heads/feature")], pack)
        self.assertEqual(paths, {".github/workflows/ci.yml"})
        self.assertTrue(gate.touches_github(paths))

    def test_new_branch_pointing_at_existing_commit_is_clean(self) -> None:
        # Creating a branch at a commit already on the remote introduces nothing.
        paths = gate.changed_paths(
            self.mirror, [(gate.ZERO_OID, self.old, "refs/heads/feature")], b""
        )
        self.assertEqual(paths, set())

    def test_changed_paths_caps_github_paths_for_pending_ui(self) -> None:
        wf = self.work / ".github" / "workflows"
        wf.mkdir(parents=True)
        for index in range(gate.MAX_CHANGED_PATHS + 5):
            (wf / f"ci-{index}.yml").write_text("on: push\n")
        _git(self.work, "add", ".github")
        _git(self.work, "commit", "--quiet", "-m", "B: many workflows")
        new = _git(self.work, "rev-parse", "HEAD").decode().strip()
        pack = self._pack_for_head()
        paths = gate.changed_paths(self.mirror, [(self.old, new, "refs/heads/main")], pack)
        self.assertIn(gate.CHANGED_PATHS_TRUNCATED, paths)
        self.assertLessEqual(len(paths), gate.MAX_CHANGED_PATHS + 1)
        self.assertTrue(gate.touches_github(paths))

    def test_quarantine_keeps_pending_ref(self) -> None:
        new = self.old
        gate.quarantine_pending(self.mirror, [(gate.ZERO_OID, new, "refs/heads/main")], "abc123")
        kept = _git(self.mirror, "rev-parse", "refs/pending/abc123/0").decode().strip()
        self.assertEqual(kept, new)

    def test_mirror_fetch_does_not_put_token_in_argv(self) -> None:
        calls: list[tuple[list[str], dict[str, str] | None]] = []

        def fake_run(_mirror: Path, args: list[str], **kwargs: object) -> bytes:
            calls.append((args, kwargs.get("env") if isinstance(kwargs.get("env"), dict) else None))
            return b""

        with patch.object(gate, "_run_git", side_effect=fake_run):
            gate.ensure_mirror("infiversehq", "trustyclaw", "ghs_secret", root=self.root)

        fetch_args, fetch_env = calls[-1]
        self.assertEqual(fetch_args[0], "fetch")
        self.assertIn("https://github.com/infiversehq/trustyclaw.git", fetch_args)
        self.assertIn("+refs/tags/*:refs/tags/*", fetch_args)
        self.assertNotIn("ghs_secret", " ".join(fetch_args))
        self.assertIsNotNone(fetch_env)
        assert fetch_env is not None
        self.assertIn("GIT_ASKPASS", fetch_env)

    def test_approve_replays_deletions_and_keeps_token_out_of_argv(self) -> None:
        root = self.root / "quarantine"
        mirror = root / "infiversehq" / "trustyclaw.git"
        _git(self.root, "clone", "--bare", "--quiet", str(self.mirror), str(mirror))
        gate.quarantine_pending(
            mirror,
            [
                (self.old, gate.ZERO_OID, "refs/heads/old"),
                (gate.ZERO_OID, self.old, "refs/heads/main"),
            ],
            "abc123",
        )
        payload = {
            "action": "approve",
            "owner": "infiversehq",
            "repo": "trustyclaw",
            "push_id": "abc123",
            "token": "ghs_secret",
            "ref_updates": [
                {"old": self.old, "new": gate.ZERO_OID, "ref": "refs/heads/old"},
                {"old": gate.ZERO_OID, "new": self.old, "ref": "refs/heads/main"},
            ],
        }
        original_run = approve_github_push._run
        git_argvs: list[list[str]] = []
        pushes: list[tuple[list[str], dict[str, str] | None]] = []

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            git_argvs.append(argv)
            if "push" in argv:
                pushes.append((argv, kwargs.get("env") if isinstance(kwargs.get("env"), dict) else None))
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return original_run(argv, **kwargs)  # type: ignore[arg-type]

        with (
            patch.object(gate, "QUARANTINE_ROOT", root),
            patch.object(approve_github_push, "_run", side_effect=fake_run),
            patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            patch.object(sys, "stdout", io.StringIO()),
        ):
            approve_github_push.main()

        self.assertEqual(len(pushes), 1)
        push_argv, push_env = pushes[0]
        self.assertIn(":refs/heads/old", push_argv)
        self.assertIn("--atomic", push_argv)
        self.assertIn(f"--force-with-lease=refs/heads/old:{self.old}", push_argv)
        self.assertIn("--force-with-lease=refs/heads/main:", push_argv)
        self.assertIn("refs/pending/abc123/1:refs/heads/main", push_argv)
        self.assertNotIn("ghs_secret", " ".join(push_argv))
        self.assertIsNotNone(push_env)
        assert push_env is not None
        self.assertIn("GIT_ASKPASS", push_env)
        for argv in git_argvs:
            self.assertIn(f"safe.directory={mirror}", argv)

    def test_cleanup_drops_pending_refs_even_if_original_ref_is_invalid(self) -> None:
        root = self.root / "quarantine"
        mirror = root / "infiversehq" / "trustyclaw.git"
        _git(self.root, "clone", "--bare", "--quiet", str(self.mirror), str(mirror))
        gate.quarantine_pending(mirror, [(gate.ZERO_OID, self.old, "refs/heads/main")], "abc123")
        payload = {
            "action": "cleanup",
            "owner": "infiversehq",
            "repo": "trustyclaw",
            "push_id": "abc123",
            "ref_updates": [{"old": gate.ZERO_OID, "new": self.old, "ref": "refs/heads/bad?ref"}],
        }

        with (
            patch.object(gate, "QUARANTINE_ROOT", root),
            patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            patch.object(sys, "stdout", io.StringIO()),
        ):
            approve_github_push.main()

        missing = subprocess.run(
            ["git", "-C", str(mirror), "rev-parse", "--verify", "--quiet", "refs/pending/abc123/0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(missing.returncode, 0)

    def test_cleanup_reports_failed_pending_ref_delete(self) -> None:
        root = self.root / "quarantine"
        mirror = root / "infiversehq" / "trustyclaw.git"
        _git(self.root, "clone", "--bare", "--quiet", str(self.mirror), str(mirror))
        gate.quarantine_pending(mirror, [(gate.ZERO_OID, self.old, "refs/heads/main")], "abc123")
        payload = {
            "action": "cleanup",
            "owner": "infiversehq",
            "repo": "trustyclaw",
            "push_id": "abc123",
            "ref_updates": [{"old": gate.ZERO_OID, "new": self.old, "ref": "refs/heads/main"}],
        }
        original_run = approve_github_push._run

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if "update-ref" in argv:
                return subprocess.CompletedProcess(argv, 1, b"", b"stale lock")
            return original_run(argv, **kwargs)  # type: ignore[arg-type]

        with (
            patch.object(gate, "QUARANTINE_ROOT", root),
            patch.object(approve_github_push, "_run", side_effect=fake_run),
            patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            patch.object(sys, "stdout", io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            approve_github_push.main()
        self.assertEqual(raised.exception.code, 2)

    def test_approve_cleanup_failure_reports_landed_push_code(self) -> None:
        root = self.root / "quarantine"
        mirror = root / "infiversehq" / "trustyclaw.git"
        _git(self.root, "clone", "--bare", "--quiet", str(self.mirror), str(mirror))
        gate.quarantine_pending(mirror, [(gate.ZERO_OID, self.old, "refs/heads/main")], "abc123")
        payload = {
            "action": "approve",
            "owner": "infiversehq",
            "repo": "trustyclaw",
            "push_id": "abc123",
            "token": "ghs_secret",
            "ref_updates": [{"old": gate.ZERO_OID, "new": self.old, "ref": "refs/heads/main"}],
        }
        original_run = approve_github_push._run

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if "push" in argv:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            if "update-ref" in argv:
                return subprocess.CompletedProcess(argv, 1, b"", b"stale lock")
            return original_run(argv, **kwargs)  # type: ignore[arg-type]

        stdout = io.StringIO()
        with (
            patch.object(gate, "QUARANTINE_ROOT", root),
            patch.object(approve_github_push, "_run", side_effect=fake_run),
            patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            patch.object(sys, "stdout", stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            approve_github_push.main()
        self.assertEqual(raised.exception.code, 2)
        error = json.loads(stdout.getvalue())["error"]
        self.assertEqual(error["code"], "cleanup_after_push")
        self.assertIn("stale lock", error["message"])

    def test_approve_recovers_when_pending_refs_were_already_cleaned_after_landing(self) -> None:
        root = self.root / "quarantine"
        mirror = root / "infiversehq" / "trustyclaw.git"
        _git(self.root, "clone", "--bare", "--quiet", str(self.mirror), str(mirror))
        payload = {
            "action": "approve",
            "owner": "infiversehq",
            "repo": "trustyclaw",
            "push_id": "abc123",
            "token": "ghs_secret",
            "ref_updates": [{"old": gate.ZERO_OID, "new": self.old, "ref": "refs/heads/main"}],
        }
        original_run = approve_github_push._run
        calls: list[tuple[list[str], dict[str, str] | None]] = []

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            calls.append((argv, kwargs.get("env") if isinstance(kwargs.get("env"), dict) else None))
            if "push" in argv:
                raise AssertionError("already-landed retry should not push again")
            if "ls-remote" in argv:
                return subprocess.CompletedProcess(argv, 0, f"{self.old}\trefs/heads/main\n".encode(), b"")
            return original_run(argv, **kwargs)  # type: ignore[arg-type]

        stdout = io.StringIO()
        with (
            patch.object(gate, "QUARANTINE_ROOT", root),
            patch.object(approve_github_push, "_run", side_effect=fake_run),
            patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            patch.object(sys, "stdout", stdout),
        ):
            approve_github_push.main()
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": True, "already_landed": True})
        ls_remote = [call for call in calls if "ls-remote" in call[0]]
        self.assertEqual(len(ls_remote), 1)
        self.assertNotIn("ghs_secret", " ".join(ls_remote[0][0]))
        self.assertIsNotNone(ls_remote[0][1])
        assert ls_remote[0][1] is not None
        self.assertIn("GIT_ASKPASS", ls_remote[0][1])

    def test_approve_recovers_when_retry_push_is_rejected_but_remote_matches(self) -> None:
        root = self.root / "quarantine"
        mirror = root / "infiversehq" / "trustyclaw.git"
        _git(self.root, "clone", "--bare", "--quiet", str(self.mirror), str(mirror))
        gate.quarantine_pending(mirror, [(gate.ZERO_OID, self.old, "refs/heads/main")], "abc123")
        payload = {
            "action": "approve",
            "owner": "infiversehq",
            "repo": "trustyclaw",
            "push_id": "abc123",
            "token": "ghs_secret",
            "ref_updates": [{"old": gate.ZERO_OID, "new": self.old, "ref": "refs/heads/main"}],
        }
        original_run = approve_github_push._run

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if "push" in argv:
                return subprocess.CompletedProcess(argv, 1, b"", b"lease rejected")
            if "ls-remote" in argv:
                return subprocess.CompletedProcess(argv, 0, f"{self.old}\trefs/heads/main\n".encode(), b"")
            return original_run(argv, **kwargs)  # type: ignore[arg-type]

        stdout = io.StringIO()
        with (
            patch.object(gate, "QUARANTINE_ROOT", root),
            patch.object(approve_github_push, "_run", side_effect=fake_run),
            patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            patch.object(sys, "stdout", stdout),
        ):
            approve_github_push.main()
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": True, "already_landed": True})
        missing = subprocess.run(
            ["git", "-C", str(mirror), "rev-parse", "--verify", "--quiet", "refs/pending/abc123/0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(missing.returncode, 0)

    def test_gate_result_cleanup_drops_pending_refs(self) -> None:
        new = self.old
        result = gate.GateResult(self.mirror, [(gate.ZERO_OID, new, "refs/heads/main")], False, {".github/x"}, b"")
        result.hold_for_approval("abc123")
        self.assertEqual(_git(self.mirror, "rev-parse", "refs/pending/abc123/0").decode().strip(), new)
        result.cleanup_pending("abc123")
        missing = subprocess.run(
            ["git", "-C", str(self.mirror), "rev-parse", "--verify", "--quiet", "refs/pending/abc123/0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(missing.returncode, 0)

    def test_gate_result_rolls_back_partial_hold_failure(self) -> None:
        new = self.old
        result = gate.GateResult(
            self.mirror,
            [
                (gate.ZERO_OID, new, "refs/heads/main"),
                (gate.ZERO_OID, new, "refs/heads/other"),
            ],
            False,
            {".github/x"},
            b"",
        )
        original_run = gate._run_git
        created = 0

        def fail_second_create(mirror: Path, args: list[str], **kwargs: object) -> bytes:
            nonlocal created
            if args[:1] == ["update-ref"] and len(args) == 3 and args[1].startswith("refs/pending/"):
                created += 1
                if created == 2:
                    raise gate.GateError("stale lock")
            return original_run(mirror, args, **kwargs)  # type: ignore[arg-type]

        with patch.object(gate, "_run_git", side_effect=fail_second_create), self.assertRaises(gate.GateError):
            result.hold_for_approval("abc123")

        missing = subprocess.run(
            ["git", "-C", str(self.mirror), "rev-parse", "--verify", "--quiet", "refs/pending/abc123/0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(missing.returncode, 0)

    def test_pending_ref_compare_does_not_peel_annotated_tags(self) -> None:
        _git(self.work, "tag", "-a", "v1", "-m", "v1")
        tag_oid = _git(self.work, "rev-parse", "v1").decode().strip()
        commit_oid = _git(self.work, "rev-parse", "v1^{}").decode().strip()
        self.assertNotEqual(tag_oid, commit_oid)
        _git(self.mirror, "fetch", "--quiet", str(self.work), "refs/tags/v1:refs/pending/abc123/0")
        self.assertEqual(approve_github_push._rev_parse(self.mirror, "refs/pending/abc123/0"), tag_oid)


if __name__ == "__main__":
    unittest.main()
