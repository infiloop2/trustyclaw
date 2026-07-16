"""Unit tests for provider-neutral OAuth 2.0 state and credential guards."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from host.tools.shared import oauth2
from host.tools.shared.oauth2 import IntegrationReconnectRequired

from test_tools import FakeHostAPI


class OAuthStateTests(unittest.TestCase):
    def test_signed_state_round_trips_and_reserved_fields_cannot_be_overridden(self) -> None:
        with patch.object(oauth2, "now", return_value=1_000):
            state = oauth2.signed_state(
                secret="secret",
                tool_id="twitter",
                extra={"code_verifier": "verifier", "tool_id": "other", "issued_at": 1},
            )
            payload = oauth2.verify_state(state, secret="secret", tool_id="twitter")
        self.assertEqual(payload["tool_id"], "twitter")
        self.assertEqual(payload["issued_at"], 1_000)
        self.assertEqual(payload["code_verifier"], "verifier")

    def test_state_rejects_tampering_wrong_tool_expiry_and_far_future_timestamp(self) -> None:
        with patch.object(oauth2, "now", return_value=1_000):
            state = oauth2.signed_state(secret="secret", tool_id="linkedin")
        cases = (
            (state[:-1] + ("A" if state[-1] != "A" else "B"), "secret", "linkedin", 1_000),
            (state, "secret", "twitter", 1_000),
            (state, "secret", "linkedin", 1_000 + oauth2.OAUTH_STATE_MAX_AGE_SECONDS + 1),
            (state, "secret", "linkedin", 900),
        )
        for candidate, secret, tool_id, current_time in cases:
            with self.subTest(tool_id=tool_id, current_time=current_time), patch.object(
                oauth2, "now", return_value=current_time
            ):
                with self.assertRaises(ValueError):
                    oauth2.verify_state(candidate, secret=secret, tool_id=tool_id)


class OAuthCredentialGuardTests(unittest.TestCase):
    def test_stale_refresh_cannot_overwrite_or_clear_a_reconnected_account(self) -> None:
        api = FakeHostAPI()
        loaded = {
            "account": {"id": "one", "label": "one", "scopes": []},
            "secret": {"access_token": "old", "expires_at": 1},
            "metadata": {"created_at": 1, "updated_at": 1},
        }
        reconnected = {
            "account": {"id": "two", "label": "two", "scopes": []},
            "secret": {"access_token": "new", "expires_at": 2_000_000_000},
            "metadata": {"created_at": 2, "updated_at": 2},
        }
        api.credentials.save(reconnected)
        with self.assertRaises(IntegrationReconnectRequired):
            oauth2.save_if_still_connected(
                api,
                loaded,  # type: ignore[arg-type]
                loaded,  # type: ignore[arg-type]
                reconnect_message="reconnect",
            )
        oauth2.clear_if_still_loaded(api, loaded)  # type: ignore[arg-type]
        self.assertEqual(api.credentials.load(), reconnected)


if __name__ == "__main__":
    unittest.main()
