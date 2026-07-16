"""Unit tests for the vendored RSA PKCS#1 v1.5 primitives."""

from __future__ import annotations

import base64
import hashlib
import secrets
import unittest

from host.tools.shared.rsa_pkcs1 import (
    RSAKeyError,
    RSAPrivateKey,
    decrypt_pkcs1_v1_5,
    load_rsa_private_key,
    sign_sha256_pkcs1_v1_5,
)

# A 1024-bit throwaway test key (never used anywhere real), in both PEM forms.
PKCS1_PEM = """-----BEGIN RSA PRIVATE KEY-----
MIICXgIBAAKBgQDP5H6sQSNk3zNf5TSWVh3t1ahspcbKIfDUw+WSNUJMTrQZ3WFN
9ppsrfnGcOsQkkFy9Zh2ET/ucG9G+HB6GhjCo4fyFEqKcnY0L/cHdVhvNn+jJLzL
H6hS3q9mKZLm/apnQmcudRgMmLHBDGiBceDtrLuF6ugP9mgzbkW5IhIcBwIDAQAB
AoGBAI5yuMF7GK+DqQYqXbAtbfCLmA5qQR47x3Nij6lxSO5Ud1/Jq2TqdsHFLALn
WIpQTPxigIdWJoJRFE6C6T8hJpjrnAZDJRwBt5ogOl3UkNtBvhGuL5qWMXwBQtor
kvgjVr8imI1ONeCWoyxKNFw4c8ISlIJJfohLm9Kucq/ZO85xAkEA6Y+yV0R7pqP/
+6cj0wwItcF75aYmqiXSf7T6EUWBflqdyHF3zhExVKVALSEKbJR1YQEtOpq2EyCw
D0UqGmROrQJBAOPdfXuVBA0Y86NZcYKWMUnKe1IB/V6sBmGPvaL/CPLlX4xFxqrW
7llh8RFCkjtO5TxyS3lmGAIzJfTPs4EP8AMCQQDIFwvxCUFpbJxzqifduTSJCX4s
KqB7KbXhJFkLjOE4L0d3HgZGKqJ5YqzNPL4icTjx5sEpsLsFPf62xkkgnQhtAkBJ
h4OiiWeRQmf8YjR6yzSEd05sHDBCiIhWmye6nUmp99JpVWrSXiDzvuMniq/da4wV
gVxRhFxi+VZaNVvbXeU5AkEAnOO8X74VgoWpimi/XwCEguseiQX7bWSCZ6ddGMO5
hieCdXnuKViFT9CHoDReMN0K+uCyDK7j5cRNC5De19Qq2w==
-----END RSA PRIVATE KEY-----"""

PKCS8_PEM = """-----BEGIN PRIVATE KEY-----
MIICeAIBADANBgkqhkiG9w0BAQEFAASCAmIwggJeAgEAAoGBAM/kfqxBI2TfM1/l
NJZWHe3VqGylxsoh8NTD5ZI1QkxOtBndYU32mmyt+cZw6xCSQXL1mHYRP+5wb0b4
cHoaGMKjh/IUSopydjQv9wd1WG82f6MkvMsfqFLer2Ypkub9qmdCZy51GAyYscEM
aIFx4O2su4Xq6A/2aDNuRbkiEhwHAgMBAAECgYEAjnK4wXsYr4OpBipdsC1t8IuY
DmpBHjvHc2KPqXFI7lR3X8mrZOp2wcUsAudYilBM/GKAh1YmglEUToLpPyEmmOuc
BkMlHAG3miA6XdSQ20G+Ea4vmpYxfAFC2iuS+CNWvyKYjU414JajLEo0XDhzwhKU
gkl+iEub0q5yr9k7znECQQDpj7JXRHumo//7pyPTDAi1wXvlpiaqJdJ/tPoRRYF+
Wp3IcXfOETFUpUAtIQpslHVhAS06mrYTILAPRSoaZE6tAkEA4919e5UEDRjzo1lx
gpYxScp7UgH9XqwGYY+9ov8I8uVfjEXGqtbuWWHxEUKSO07lPHJLeWYYAjMl9M+z
gQ/wAwJBAMgXC/EJQWlsnHOqJ925NIkJfiwqoHspteEkWQuM4TgvR3ceBkYqonli
rM08viJxOPHmwSmwuwU9/rbGSSCdCG0CQEmHg6KJZ5FCZ/xiNHrLNIR3TmwcMEKI
iFabJ7qdSan30mlVatJeIPO+4yeKr91rjBWBXFGEXGL5Vlo1W9td5TkCQQCc47xf
vhWChamKaL9fAISC6x6JBfttZIJnp10Yw7mGJ4J1ee4pWIVP0IegNF4w3Qr64LIM
ruPlxE0LkN7X1Crb
-----END PRIVATE KEY-----"""

# DigestInfo prefix for SHA-256, used to check the signature padding.
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def encrypt_pkcs1_v1_5(key: RSAPrivateKey, message: bytes) -> bytes:
    """RSAES-PKCS1-v1_5 encryption using the key's public half (test helper)."""
    padding_length = key.byte_length - len(message) - 3
    assert padding_length >= 8
    padding = bytes(secrets.choice(range(1, 256)) for _ in range(padding_length))
    encoded = b"\x00\x02" + padding + b"\x00" + message
    return pow(int.from_bytes(encoded, "big"), key.e, key.n).to_bytes(key.byte_length, "big")


class RSAKeyLoadingTests(unittest.TestCase):
    def test_pkcs1_and_pkcs8_parse_to_the_same_key(self) -> None:
        pkcs1 = load_rsa_private_key(PKCS1_PEM)
        pkcs8 = load_rsa_private_key(PKCS8_PEM)
        self.assertEqual(pkcs1, pkcs8)
        self.assertEqual(pkcs1.byte_length, 128)
        self.assertEqual(pkcs1.e, 65537)

    def test_single_line_paste_without_armor_parses(self) -> None:
        body = "".join(
            line for line in PKCS8_PEM.splitlines() if "PRIVATE KEY" not in line
        )
        self.assertEqual(load_rsa_private_key(body), load_rsa_private_key(PKCS8_PEM))
        # Newlines flattened to spaces (what pasting into a single-line field does).
        self.assertEqual(load_rsa_private_key(PKCS1_PEM.replace("\n", " ")), load_rsa_private_key(PKCS1_PEM))

    def test_invalid_material_raises_key_error(self) -> None:
        for text in ("", "not base64!!", base64.b64encode(b"\x30\x03\x02\x01\x00").decode()):
            with self.assertRaises(RSAKeyError):
                load_rsa_private_key(text)


class RSASignAndDecryptTests(unittest.TestCase):
    def test_signature_verifies_with_the_public_key(self) -> None:
        key = load_rsa_private_key(PKCS1_PEM)
        message = b"base-string-to-sign"
        signature = sign_sha256_pkcs1_v1_5(key, message)
        self.assertEqual(len(signature), key.byte_length)
        recovered = pow(int.from_bytes(signature, "big"), key.e, key.n).to_bytes(key.byte_length, "big")
        expected_tail = SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
        self.assertTrue(recovered.startswith(b"\x00\x01\xff"))
        self.assertTrue(recovered.endswith(b"\x00" + expected_tail))

    def test_decrypt_round_trip(self) -> None:
        key = load_rsa_private_key(PKCS8_PEM)
        secret = bytes(range(1, 26))
        self.assertEqual(decrypt_pkcs1_v1_5(key, encrypt_pkcs1_v1_5(key, secret)), secret)

    def test_decrypt_rejects_garbage(self) -> None:
        key = load_rsa_private_key(PKCS1_PEM)
        with self.assertRaises(RSAKeyError):
            decrypt_pkcs1_v1_5(key, b"\x00" * key.byte_length)
        with self.assertRaises(RSAKeyError):
            decrypt_pkcs1_v1_5(key, b"\x01")


if __name__ == "__main__":
    unittest.main()
