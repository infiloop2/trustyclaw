"""Tests for host.runtime.write_config: the bootstrap step that computes the
effective host config, stores it in the config table, and echoes it for the
root-only bootstrap steps.

The mode semantics pinned here used to live inline in bootstrap.sh: deploy and
reconfigure take credentials from the payload; upgrade and recover carry them
over from the stored config and never accept new ones.
"""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

import pg_harness

from host.runtime import write_config
from host.runtime.state import load_config, save_config

# The config table constrains the hash to 64 hex characters.
PAYLOAD_HASH = "a" * 64
STORED_HASH = "b" * 64
KEEP_HASH = "c" * 64
SSH_CONNECTION = {"mode": "ssh", "ssh_public_key": "ssh-ed25519 AAAATEST operator@example"}
CLOUDFLARE_CONNECTION = {
    "mode": "cloudflare_access",
    "hostname": "admin.example.com",
    "tunnel_token": "token-value",
}


def run(mode: str, runtime_config: dict) -> tuple[int, str, str]:
    stdin = io.StringIO(json.dumps({"mode": mode, "runtime_config": runtime_config}))
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch("sys.stdin", stdin), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
        code = write_config.main()
    return code, stdout.getvalue(), stderr.getvalue()


class WriteConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()

    def payload(self, **overrides) -> dict:
        value = {
            "agent_name": "trustyclaw-test",
            "admin_password_sha256": PAYLOAD_HASH,
            "operator_connections": [SSH_CONNECTION],
        }
        value.update(overrides)
        return value

    def test_deploy_and_reconfigure_take_credentials_from_the_payload(self) -> None:
        for mode in ("deploy", "reconfigure"):
            with self.subTest(mode=mode):
                save_config({"admin_password_sha256": STORED_HASH, "operator_connections": [CLOUDFLARE_CONNECTION]})
                code, stdout, stderr = run(mode, self.payload())
                self.assertEqual(code, 0, stderr)
                stored = load_config()
                self.assertEqual(stored["admin_password_sha256"], PAYLOAD_HASH)
                self.assertEqual(stored["operator_connections"], [SSH_CONNECTION])
                self.assertEqual(json.loads(stdout), stored)

    def test_upgrade_and_recover_carry_over_the_stored_credentials(self) -> None:
        for mode in ("upgrade", "recover"):
            with self.subTest(mode=mode):
                save_config(
                    {
                        "agent_name": "old-name",
                        "admin_password_sha256": STORED_HASH,
                        "operator_connections": [CLOUDFLARE_CONNECTION],
                    }
                )
                code, stdout, stderr = run(mode, self.payload())
                self.assertEqual(code, 0, stderr)
                stored = load_config()
                # agent_name always comes from the payload; credentials never do.
                self.assertEqual(stored["agent_name"], "trustyclaw-test")
                self.assertEqual(stored["admin_password_sha256"], STORED_HASH)
                self.assertEqual(stored["operator_connections"], [CLOUDFLARE_CONNECTION])
                self.assertEqual(json.loads(stdout), stored)

    def test_upgrade_without_stored_credentials_fails(self) -> None:
        code, _, stderr = run("upgrade", self.payload())
        self.assertEqual(code, 1)
        self.assertIn("admin_password_sha256", stderr)

    def test_unknown_mode_is_rejected(self) -> None:
        code, _, stderr = run("sideways", self.payload())
        self.assertEqual(code, 1)
        self.assertIn("unknown deploy operation mode", stderr)

    def test_operator_connections_are_validated(self) -> None:
        cases = [
            ([], "missing operator_connections"),
            ([{"mode": "ssh", "ssh_public_key": "not-a-key"}], "valid ssh_public_key"),
            ([{"mode": "cloudflare_access", "hostname": "", "tunnel_token": "t"}], "missing hostname"),
            (
                [{"mode": "cloudflare_access", "hostname": "h", "tunnel_token": "two words"}],
                "single-line tunnel_token",
            ),
            ([{"mode": "carrier-pigeon"}], "unsupported operator connection mode"),
            ([SSH_CONNECTION, SSH_CONNECTION], "duplicate operator connection mode"),
        ]
        for connections, message in cases:
            with self.subTest(message=message):
                code, _, stderr = run("deploy", self.payload(operator_connections=connections))
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)
                self.assertEqual(load_config(), {})

    def test_failed_validation_leaves_existing_config_untouched(self) -> None:
        save_config({"agent_name": "keep", "admin_password_sha256": KEEP_HASH, "operator_connections": [SSH_CONNECTION]})
        code, _, _ = run("reconfigure", self.payload(operator_connections=[]))
        self.assertEqual(code, 1)
        self.assertEqual(load_config()["admin_password_sha256"], KEEP_HASH)


if __name__ == "__main__":
    unittest.main()
