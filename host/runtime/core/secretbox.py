"""At-rest encryption for stored secrets (GitHub tokens/keys, tunnel tokens).

Secrets in the admin database are AES-256-CBC encrypted (PBKDF2 key
derivation, via ``/usr/bin/openssl`` — the host's standard-library-only
posture has no in-tree cipher) with a key that lives in the ``secret_keys``
table, created by the schema migration so it exists from the moment the
schema does. Keeping the key in the database keeps all admin state in one
place; upgrade and recovery carry key and ciphertext together by
construction.

This is an accidental-exposure control, not a root or offline defense: a
stray ``SELECT *`` on a secret-bearing table (or a pasted table dump) no
longer reveals credential material, because reading the ciphertext columns
alone is useless without the key row. A full database dump necessarily
includes the key.

Values are stored as ``enc:v1:<base64>``; ``decrypt`` refuses anything
without that prefix, since every writer encrypts.
"""

from __future__ import annotations

import os
import subprocess

from host.runtime.core import db

OPENSSL_BIN = "/usr/bin/openssl"
PREFIX = "enc:v1:"
PBKDF2_ITERATIONS = "100000"
OPENSSL_TIMEOUT_SECONDS = 30
_KEY_ENV = "TRUSTYCLAW_SECRETBOX_KEY"


class SecretBoxError(Exception):
    pass


def _load_key() -> str:
    """The stored key. The schema migration creates it, so a missing row is
    a real error, never a first-use condition."""
    with db.transaction() as cur:
        cur.execute("SELECT key_hex FROM secret_keys")
        row = cur.fetchone()
    if row is None:
        raise SecretBoxError("the secret_keys row is missing (the schema migration creates it)")
    return str(row[0])


def encrypt(value: str) -> str:
    return PREFIX + _run_openssl(["-base64", "-A"], value.encode(), _load_key())


def decrypt(value: str) -> str:
    if not value.startswith(PREFIX):
        raise SecretBoxError("stored secret is not secretbox ciphertext")
    return _run_openssl(["-d", "-base64", "-A"], value[len(PREFIX):].encode(), _load_key())


def _run_openssl(mode_args: list[str], data: bytes, key: str) -> str:
    # The key reaches openssl through the child's environment (-pass env:),
    # never argv (visible in ps) and never a file. Another user cannot read a
    # foreign process's environment without root.
    try:
        proc = subprocess.run(
            [
                OPENSSL_BIN, "enc", "-aes-256-cbc", "-pbkdf2",
                "-iter", PBKDF2_ITERATIONS, "-salt",
                "-pass", f"env:{_KEY_ENV}",
                *mode_args,
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=OPENSSL_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, _KEY_ENV: key},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretBoxError(f"openssl secret encryption failed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace")[:200]
        raise SecretBoxError(f"openssl secret encryption failed: {detail}")
    try:
        return proc.stdout.decode()
    except UnicodeDecodeError as exc:
        raise SecretBoxError("decrypted secret is not valid text") from exc
