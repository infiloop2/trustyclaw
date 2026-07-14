"""GitHub credential helper tests: no database, no network.

Covers the mint root helper (App JWT construction and installation-wide
token minting against a fake GitHub API) and the admin-side staleness logic.
The working token itself lives in the proxy-readable database row and is
covered by the admin API integration tests.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from host.runtime import github_credential, mint_github_app_token


class MintGithubAppTokenTests(unittest.TestCase):
    def _generate_key(self) -> str:
        proc = subprocess.run(
            ["/usr/bin/openssl", "genrsa", "2048"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return proc.stdout.decode()

    def test_app_jwt_is_rs256_signed_and_verifiable(self) -> None:
        private_key = self._generate_key()
        jwt = mint_github_app_token._app_jwt("12345", private_key)
        header_b64, claims_b64, signature_b64 = jwt.split(".")

        def unb64(value: str) -> bytes:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

        header = json.loads(unb64(header_b64))
        claims = json.loads(unb64(claims_b64))
        self.assertEqual(header, {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(claims["iss"], "12345")
        self.assertLess(claims["iat"], claims["exp"])

        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "key.pem"
            key_path.write_text(private_key)
            public_path = Path(tmp) / "public.pem"
            subprocess.run(
                ["/usr/bin/openssl", "rsa", "-in", str(key_path), "-pubout", "-out", str(public_path)],
                check=True,
                stderr=subprocess.DEVNULL,
            )
            signature_path = Path(tmp) / "signature.bin"
            signature_path.write_bytes(unb64(signature_b64))
            verify = subprocess.run(
                [
                    "/usr/bin/openssl", "dgst", "-sha256",
                    "-verify", str(public_path), "-signature", str(signature_path),
                ],
                input=f"{header_b64}.{claims_b64}".encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertEqual(verify.returncode, 0, verify.stderr)

    def test_mint_posts_installation_wide_request_and_parses_response(self) -> None:
        private_key = self._generate_key()
        captured: dict[str, object] = {}

        class FakeResponse:
            def read(self) -> bytes:
                return json.dumps({"token": "ghs_minted", "expires_at": "2026-06-08T01:00:00Z"}).encode()

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *args):  # type: ignore[no-untyped-def]
                return None

        def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            captured["auth"] = request.get_header("Authorization")
            return FakeResponse()

        with patch("host.runtime.mint_github_app_token.urllib.request.urlopen", side_effect=fake_urlopen):
            result = mint_github_app_token.mint_installation_token("12345", "67890", private_key)

        self.assertEqual(result, {"token": "ghs_minted", "expires_at": "2026-06-08T01:00:00Z"})
        self.assertEqual(captured["url"], "https://api.github.com/app/installations/67890/access_tokens")
        # Installation-wide: no repository scoping in the mint request. The
        # proxy's GitHub guard enforces per-repository access on every request.
        self.assertEqual(captured["body"], {})
        auth = captured["auth"]
        assert isinstance(auth, str)
        self.assertTrue(auth.startswith("Bearer "))

    def test_mint_failure_shapes_raise_mint_error(self) -> None:
        private_key = self._generate_key()

        class BadResponse:
            def read(self) -> bytes:
                return b"{}"

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *args):  # type: ignore[no-untyped-def]
                return None

        with patch("host.runtime.mint_github_app_token.urllib.request.urlopen", return_value=BadResponse()):
            with self.assertRaises(mint_github_app_token.MintError):
                mint_github_app_token.mint_installation_token("12345", "67890", private_key)

    def test_rs256_sign_never_writes_the_key_to_a_file(self) -> None:
        # The private key reaches openssl on stdin; the only file involved is
        # the (public) signing input, cleaned up after the call.
        private_key = self._generate_key()
        calls: list[dict[str, object]] = []
        created: list[str] = []
        real_named = tempfile.NamedTemporaryFile
        real_run = subprocess.run

        def tracking_named(*args, **kwargs):  # type: ignore[no-untyped-def]
            handle = real_named(*args, **kwargs)
            created.append(handle.name)
            return handle

        def tracking_run(command, **kwargs):  # type: ignore[no-untyped-def]
            calls.append({"command": command, "input": kwargs.get("input")})
            return real_run(command, **kwargs)

        with patch("host.runtime.mint_github_app_token.tempfile.NamedTemporaryFile", side_effect=tracking_named):
            with patch("host.runtime.mint_github_app_token.subprocess.run", side_effect=tracking_run):
                mint_github_app_token._rs256_sign(b"data", private_key)
        self.assertEqual(len(calls), 1)
        self.assertIn("/dev/stdin", calls[0]["command"])
        self.assertEqual(calls[0]["input"], private_key.encode())
        self.assertEqual(len(created), 1)
        self.assertFalse(os.path.exists(created[0]))
        # The temp file held only the signing input, never the key.
        self.assertNotIn("key", created[0])


class WorkflowTriggerParsingTests(unittest.TestCase):
    def test_pages_facts_distinguish_no_site_from_unreadable_site(self) -> None:
        from host.runtime import audit_github_repo

        def not_found() -> audit_github_repo.AuditError:
            exc = audit_github_repo.AuditError("not found")
            exc.status = 404  # type: ignore[attr-defined]
            return exc

        calls: list[str] = []

        def fake_no_pages_get(token: str, path: str):  # type: ignore[no-untyped-def]
            calls.append(path)
            if path == "/repos/infiloop2/trustyclaw":
                return {"visibility": "private", "has_pages": False}
            raise not_found()

        with patch("host.runtime.audit_github_repo._get", side_effect=fake_no_pages_get):
            facts = audit_github_repo.audit_repository("token", "infiloop2", "trustyclaw")
        # pages_public is the single stored Pages fact (tri-state); has_pages
        # only decides locally whether the /pages endpoint is queried.
        self.assertNotIn("has_pages", facts)
        self.assertEqual(facts["pages_public"], False)
        self.assertNotIn("/repos/infiloop2/trustyclaw/pages", calls)

        def fake_unreadable_pages_get(token: str, path: str):  # type: ignore[no-untyped-def]
            if path == "/repos/infiloop2/trustyclaw":
                return {"visibility": "private", "has_pages": True}
            raise not_found()

        with patch("host.runtime.audit_github_repo._get", side_effect=fake_unreadable_pages_get):
            facts = audit_github_repo.audit_repository("token", "infiloop2", "trustyclaw")
        self.assertNotIn("has_pages", facts)
        self.assertIsNone(facts["pages_public"])

    def test_trigger_shapes(self) -> None:
        from host.runtime import audit_github_repo, github_repo_audit

        # A plain substring search for the dangerous trigger names, whatever
        # the YAML shape; benign triggers are not collected (the workflow's
        # presence already warns) and false positives are acceptable.
        cases = {
            "on: push": [],
            "on: [push, pull_request]": [],
            "'on': [workflow_dispatch]": [],
            "on: {push: {branches: [main]}, pull_request_target: {types: [opened]}}": [
                "pull_request_target",
            ],
            "on:\n  push:\n    branches: [main]\n  workflow_run:\n    workflows: [ci]": [],
            "on:\n  - push\n  - pull_request_target": ["pull_request_target"],
            "# mentions pull_request_target in a comment": ["pull_request_target"],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(audit_github_repo._triggers(text), expected)
        # The critical secrets-exposure warning keys off exactly these
        # trigger names, in every YAML shape above.
        warnings = github_repo_audit._warnings(
            {"visibility": "private", "pages_public": False,
             "workflows": [{"path": "w.yml", "triggers": ["pull_request_target"]}]}
        )
        self.assertEqual(
            [w["code"] for w in warnings],
            ["secrets_exposed_to_pr_workflows", "workflows_execute_pushes"],
        )
        workflow_run_warnings = github_repo_audit._warnings(
            {"visibility": "private", "pages_public": False,
             "workflows": [{"path": "w.yml", "triggers": ["workflow_run"]}]}
        )
        self.assertEqual([w["code"] for w in workflow_run_warnings], ["workflows_execute_pushes"])
        # A private repo with a public Pages site is the same exfiltration
        # sink as a public write repository.
        pages = github_repo_audit._warnings({"visibility": "private", "pages_public": True})
        self.assertEqual([(w["code"], w["severity"]) for w in pages], [("public_pages_site", "critical")])
        unknown_pages = github_repo_audit._warnings({"visibility": "private", "pages_public": None})
        self.assertEqual(
            [(w["code"], w["severity"]) for w in unknown_pages],
            [("pages_visibility_unknown", "warning")],
        )
        public = github_repo_audit._warnings(
            {"visibility": "public", "permissions": {"push": True}, "default_branch_protected": False}
        )
        self.assertEqual(
            [(w["code"], w["severity"]) for w in public],
            [("public_repository", "critical"), ("unprotected_default_branch", "warning")],
        )
        # A pull-only token on a private repository with no workflows raises
        # nothing.
        self.assertEqual(
            [w["code"] for w in github_repo_audit._warnings(
                {"visibility": "private", "pages_public": False, "permissions": {"pull": True}}
            )],
            [],
        )


class TokenStalenessTests(unittest.TestCase):
    def test_token_staleness_uses_refresh_margin(self) -> None:
        now = datetime.now(timezone.utc)
        fresh = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        nearly = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertFalse(github_credential._token_stale(fresh))
        self.assertTrue(github_credential._token_stale(nearly))
        self.assertTrue(github_credential._token_stale(None))
        self.assertTrue(github_credential._token_stale("not a timestamp"))


if __name__ == "__main__":
    unittest.main()
