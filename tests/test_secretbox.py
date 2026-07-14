"""Secretbox tests: openssl plus the scratch database that holds the key."""

from __future__ import annotations

import unittest

import pg_harness

from host.runtime import db, secretbox, state


class SecretBoxTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()

    def test_round_trip_and_ciphertext_shape(self) -> None:
        secret = "github_pat_11ABCDEF_secret-value"
        encrypted = secretbox.encrypt(secret)
        self.assertTrue(encrypted.startswith("enc:v1:"))
        self.assertNotIn(secret, encrypted)
        self.assertNotRegex(encrypted, r"\s")
        self.assertEqual(secretbox.decrypt(encrypted), secret)

    def test_multiline_pem_round_trips(self) -> None:
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIB\nlines\n-----END RSA PRIVATE KEY-----\n"
        self.assertEqual(secretbox.decrypt(secretbox.encrypt(pem)), pem)

    def test_salted_ciphertexts_differ_per_call(self) -> None:
        self.assertNotEqual(secretbox.encrypt("same"), secretbox.encrypt("same"))

    def test_decrypt_refuses_values_without_the_ciphertext_prefix(self) -> None:
        # Every writer encrypts, so an unprefixed value is corruption, not a
        # legacy row.
        with self.assertRaises(secretbox.SecretBoxError):
            secretbox.decrypt("not-ciphertext")

    def test_key_row_comes_from_the_migration_and_is_reused(self) -> None:
        # The schema migration creates the key, so it exists before any
        # encrypt runs; every encrypt reuses it.
        with db.transaction() as cur:
            cur.execute("SELECT key_hex FROM secret_keys")
            rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        key_before = rows[0][0]
        self.assertRegex(key_before, r"^[0-9a-f]{64}$")
        first = secretbox.encrypt("value")
        self.assertEqual(secretbox.decrypt(first), "value")
        secretbox.encrypt("another")
        with db.transaction() as cur:
            cur.execute("SELECT key_hex FROM secret_keys")
            self.assertEqual(cur.fetchone()[0], key_before)

    def test_encrypt_without_key_row_fails_closed(self) -> None:
        # No lazy re-creation: a missing key row is a hard error, not a
        # signal to mint a new key (which would strand existing ciphertext).
        with db.transaction() as cur:
            cur.execute("DELETE FROM secret_keys")
        with self.assertRaises(secretbox.SecretBoxError):
            secretbox.encrypt("value")

    def test_decrypt_with_wrong_key_fails_closed(self) -> None:
        encrypted = secretbox.encrypt("value")
        with db.transaction() as cur:
            cur.execute("UPDATE secret_keys SET key_hex = %s", ("0" * 64,))
        with self.assertRaises(secretbox.SecretBoxError):
            secretbox.decrypt(encrypted)

    def test_decrypt_without_key_row_fails_closed(self) -> None:
        encrypted = secretbox.encrypt("value")
        with db.transaction() as cur:
            cur.execute("DELETE FROM secret_keys")
        with self.assertRaises(secretbox.SecretBoxError):
            secretbox.decrypt(encrypted)

    def test_corrupt_ciphertext_fails_closed(self) -> None:
        secretbox.encrypt("prime the key")
        with self.assertRaises(secretbox.SecretBoxError):
            secretbox.decrypt("enc:v1:bm90IHJlYWwgY2lwaGVydGV4dA==")

    def test_save_config_encrypts_the_tunnel_token(self) -> None:
        # Every deploy/upgrade re-saves config through save_config, which
        # encrypts the tunnel token at rest — so no startup sweep is needed
        # to migrate a pre-secretbox plaintext row.
        state.save_config(
            {
                "agent_name": "trustyclaw-test",
                "admin_password_sha256": "a" * 64,
                "operator_connections": [
                    {"mode": "cloudflare_access", "hostname": "trustyclaw.example.com", "tunnel_token": "tok-value"}
                ],
            }
        )
        with db.transaction() as cur:
            cur.execute("SELECT tunnel_token FROM operator_connections")
            raw = cur.fetchone()[0]
        self.assertTrue(raw.startswith("enc:v1:"))
        self.assertNotIn("tok-value", raw)
        self.assertEqual(
            state.load_config()["operator_connections"][0]["tunnel_token"],
            "tok-value",
        )


if __name__ == "__main__":
    unittest.main()
