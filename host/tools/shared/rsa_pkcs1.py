"""Minimal pure-stdlib RSA PKCS#1 v1.5 primitives.

IBKR's first-party OAuth 1.0a flow needs two RSA operations the stdlib lacks:
signing the live-session-token request (RSASSA-PKCS1-v1_5 with SHA-256) and
decrypting the portal-issued access token secret (RSAES-PKCS1-v1_5). This
module vendors exactly those two operations plus the key parsing they need,
so the repo stays dependency-free.

Scope note: both operations run on operator-supplied key material against a
provider the operator chose — there is no untrusted-ciphertext or
timing-oracle exposure here, so constant-time hardening is deliberately out
of scope. Do not reuse this module for adversarial inputs.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass

# DigestInfo prefix for SHA-256 (RFC 8017, EMSA-PKCS1-v1_5).
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
# id-rsaEncryption, expected in a PKCS#8 AlgorithmIdentifier.
_RSA_OID = bytes.fromhex("2a864886f70d010101")
_PEM_ARMOR_RE = re.compile(r"-----(BEGIN|END)[^-]+-----")


class RSAKeyError(ValueError):
    """The supplied key material could not be parsed as an RSA private key."""


@dataclass(frozen=True)
class RSAPrivateKey:
    n: int
    e: int
    d: int

    @property
    def byte_length(self) -> int:
        return (self.n.bit_length() + 7) // 8


class _DERReader:
    """Cursor over DER bytes; just enough ASN.1 for RSA key structures."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def _read_header(self) -> tuple[int, int]:
        if self.offset + 2 > len(self.data):
            raise RSAKeyError("Truncated DER structure.")
        tag = self.data[self.offset]
        length = self.data[self.offset + 1]
        self.offset += 2
        if length & 0x80:
            count = length & 0x7F
            if count == 0 or count > 4 or self.offset + count > len(self.data):
                raise RSAKeyError("Unsupported DER length encoding.")
            length = int.from_bytes(self.data[self.offset : self.offset + count], "big")
            self.offset += count
        if self.offset + length > len(self.data):
            raise RSAKeyError("Truncated DER structure.")
        return tag, length

    def read_tagged(self, expected_tag: int) -> bytes:
        tag, length = self._read_header()
        if tag != expected_tag:
            raise RSAKeyError(f"Unexpected DER tag 0x{tag:02x}.")
        value = self.data[self.offset : self.offset + length]
        self.offset += length
        return value

    def enter_sequence(self) -> "_DERReader":
        return _DERReader(self.read_tagged(0x30))

    def read_integer(self) -> int:
        return int.from_bytes(self.read_tagged(0x02), "big")

    def peek_tag(self) -> int:
        if self.offset >= len(self.data):
            raise RSAKeyError("Truncated DER structure.")
        return self.data[self.offset]


def _pkcs1_private_key(sequence: _DERReader) -> RSAPrivateKey:
    version = sequence.read_integer()
    if version != 0:
        raise RSAKeyError("Unsupported RSA key version.")
    n = sequence.read_integer()
    e = sequence.read_integer()
    d = sequence.read_integer()
    if n <= 0 or e <= 0 or d <= 0:
        raise RSAKeyError("Invalid RSA key parameters.")
    return RSAPrivateKey(n=n, e=e, d=d)


def load_rsa_private_key(text: str) -> RSAPrivateKey:
    """Parse an RSA private key from PEM or bare-base64 DER, in PKCS#1
    (``RSA PRIVATE KEY``) or PKCS#8 (``PRIVATE KEY``) form. All whitespace is
    ignored, so a PEM body pasted into a single-line field still parses."""
    body = _PEM_ARMOR_RE.sub("", text)
    body = "".join(body.split())
    if not body:
        raise RSAKeyError("The RSA private key value is empty.")
    try:
        der = base64.b64decode(body, validate=True)
    except Exception as exc:
        raise RSAKeyError("The RSA private key is not valid base64/PEM.") from exc
    outer = _DERReader(der).enter_sequence()
    version_or_algid = outer.peek_tag()
    if version_or_algid != 0x02:
        raise RSAKeyError("Unrecognized RSA key structure.")
    first_integer = outer.read_integer()
    if outer.peek_tag() == 0x30:
        # PKCS#8: version, AlgorithmIdentifier SEQUENCE, privateKey OCTET STRING.
        if first_integer != 0:
            raise RSAKeyError("Unsupported PKCS#8 version.")
        algorithm = outer.enter_sequence()
        oid = algorithm.read_tagged(0x06)
        if oid != _RSA_OID:
            raise RSAKeyError("The PKCS#8 key is not an RSA key.")
        inner = _DERReader(outer.read_tagged(0x04)).enter_sequence()
        return _pkcs1_private_key(inner)
    # PKCS#1: the first integer was the version; continue in place.
    if first_integer != 0:
        raise RSAKeyError("Unsupported RSA key version.")
    n = outer.read_integer()
    e = outer.read_integer()
    d = outer.read_integer()
    if n <= 0 or e <= 0 or d <= 0:
        raise RSAKeyError("Invalid RSA key parameters.")
    return RSAPrivateKey(n=n, e=e, d=d)


def sign_sha256_pkcs1_v1_5(key: RSAPrivateKey, message: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 signature over SHA-256(message) (RFC 8017 §8.2)."""
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    padding_length = key.byte_length - len(digest_info) - 3
    if padding_length < 8:
        raise RSAKeyError("The RSA key is too small to sign with SHA-256.")
    encoded = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), key.d, key.n)
    return signature.to_bytes(key.byte_length, "big")


def decrypt_pkcs1_v1_5(key: RSAPrivateKey, ciphertext: bytes) -> bytes:
    """RSAES-PKCS1-v1_5 decryption (RFC 8017 §7.2.2)."""
    if len(ciphertext) != key.byte_length:
        raise RSAKeyError("The RSA ciphertext length does not match the key.")
    decrypted = pow(int.from_bytes(ciphertext, "big"), key.d, key.n)
    encoded = decrypted.to_bytes(key.byte_length, "big")
    if encoded[:2] != b"\x00\x02":
        raise RSAKeyError("RSA decryption failed (bad padding).")
    separator = encoded.find(b"\x00", 2)
    if separator < 10:  # at least 8 nonzero padding bytes per the spec
        raise RSAKeyError("RSA decryption failed (bad padding).")
    return encoded[separator + 1 :]
