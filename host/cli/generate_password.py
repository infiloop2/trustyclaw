"""Generate an admin password and its SHA-256 digest.

Entrypoint: ``python3 -m host.cli.generate_password``. The lifecycle commands
take only ``--admin-password-sha256``, so this is the one place a password is
ever produced: store the password in your password manager and pass the
sha256 value to deploy or reconfigure. This command touches nothing else — no
config, no AWS, no files.
"""

from __future__ import annotations

import hashlib
import secrets


def main() -> int:
    password = secrets.token_urlsafe(32)
    digest = hashlib.sha256(password.encode()).hexdigest()
    print(f"password: {password}")
    print(f"sha256:   {digest}")
    print()
    print("Store the password in your password manager. Pass the sha256 value to")
    print("deploy or reconfigure as --admin-password-sha256.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
