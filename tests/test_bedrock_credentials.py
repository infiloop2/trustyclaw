from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from host.runtime.admin_api import bedrock_credentials


class BedrockCredentialsTests(unittest.TestCase):
    def test_account_status_reports_a_connected_credential(self) -> None:
        account = {
            "access_key_id": "AKIAEXAMPLEKEY000001",
            "account_id": "123456789012",
        }
        with (
            patch.object(
                bedrock_credentials.state,
                "read_bedrock_access_key_id",
                return_value="AKIAEXAMPLEKEY000001",
            ),
            patch.object(bedrock_credentials.state, "read_bedrock_account", return_value=account),
        ):
            self.assertEqual(
                bedrock_credentials.account_status(),
                ("active", None, account),
            )

    def test_account_status_without_a_credential_awaits_connection(self) -> None:
        with patch.object(
            bedrock_credentials.state, "read_bedrock_access_key_id", return_value=None
        ):
            self.assertEqual(
                bedrock_credentials.account_status(),
                ("awaiting_login", None, None),
            )

    def test_account_status_rejects_inconsistent_metadata(self) -> None:
        with (
            patch.object(
                bedrock_credentials.state,
                "read_bedrock_access_key_id",
                return_value="AKIAEXAMPLEKEY000001",
            ),
            patch.object(
                bedrock_credentials.state,
                "read_bedrock_account",
                return_value={"access_key_id": "AKIADIFFERENTKEY00001"},
            ),
        ):
            status, error, account = bedrock_credentials.account_status()
        self.assertEqual(status, "error")
        self.assertIn("submit the credentials again", error or "")
        self.assertIsNone(account)

    def test_credential_env_carries_the_decrypted_key_pair(self) -> None:
        with patch.object(
            bedrock_credentials.state,
            "read_bedrock_credential_secret",
            return_value=("AKIAEXAMPLEKEY000001", "S" * 40),
        ):
            self.assertEqual(
                bedrock_credentials.credential_env(),
                {
                    bedrock_credentials.ACCESS_KEY_ID_ENV: "AKIAEXAMPLEKEY000001",
                    bedrock_credentials.SECRET_ACCESS_KEY_ENV: "S" * 40,
                },
            )

    def test_credential_env_is_none_without_a_credential(self) -> None:
        with patch.object(bedrock_credentials.state, "read_bedrock_credential_secret", return_value=None):
            self.assertIsNone(bedrock_credentials.credential_env())

    def test_credential_env_uses_an_in_memory_candidate_without_reading_storage(self) -> None:
        with patch.object(
            bedrock_credentials.state,
            "read_bedrock_credential_secret",
            side_effect=AssertionError("candidate validation must not read storage"),
        ):
            self.assertEqual(
                bedrock_credentials.credential_env(("AKIACANDIDATEKEY00001", "N" * 40)),
                {
                    bedrock_credentials.ACCESS_KEY_ID_ENV: "AKIACANDIDATEKEY00001",
                    bedrock_credentials.SECRET_ACCESS_KEY_ENV: "N" * 40,
                },
            )

    def _fake_env(self):  # type: ignore[no-untyped-def]
        return patch.object(
            bedrock_credentials,
            "credential_env",
            return_value={
                bedrock_credentials.ACCESS_KEY_ID_ENV: "AKIAEXAMPLEKEY000001",
                bedrock_credentials.SECRET_ACCESS_KEY_ENV: "S" * 40,
            },
        )

    def test_read_attested_identity_passes_creds_via_env_and_parses_json(self) -> None:
        # The helper receives the key pair in its environment; the fake echoes
        # the id it was given, proving the env reached the subprocess.
        command = [
            sys.executable,
            "-c",
            (
                "import json, os; print(json.dumps({"
                "'access_key_id': os.environ['TRUSTYCLAW_BEDROCK_AWS_ACCESS_KEY_ID'],"
                " 'account_id': '123456789012', 'arn': 'arn:aws:iam::123456789012:user/pi'}))"
            ),
        ]
        with self._fake_env():
            self.assertEqual(
                bedrock_credentials.read_attested_identity(command),
                {
                    "access_key_id": "AKIAEXAMPLEKEY000001",
                    "account_id": "123456789012",
                    "arn": "arn:aws:iam::123456789012:user/pi",
                },
            )

    def test_read_attested_identity_maps_exit_3_to_authentication_error(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import sys; print('AWS rejected the credential: HTTP 403', file=sys.stderr); sys.exit(3)",
        ]
        with self._fake_env():
            with self.assertRaises(bedrock_credentials.BedrockAuthenticationError):
                bedrock_credentials.read_attested_identity(command)

    def test_helper_without_a_connected_credential_fails(self) -> None:
        with patch.object(bedrock_credentials, "credential_env", return_value=None):
            with self.assertRaisesRegex(bedrock_credentials.BedrockCredentialsError, "no AWS credentials"):
                bedrock_credentials.read_attested_identity([sys.executable, "-c", "pass"])

if __name__ == "__main__":
    unittest.main()
