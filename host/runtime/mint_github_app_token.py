"""Root helper behind ``mint-github-app-token``.

Mints a short-lived, installation-wide GitHub App token: it covers every
repository the App installation grants (operator-managed on GitHub), and
per-repository enforcement is the network proxy's GitHub guard. It runs as
root because only root (and the proxy, for policy-approved agent traffic) has
outbound network access; the admin service that owns the credential has none.
The App private key only ever moves through pipes — admin service stdin in,
openssl stdin on — and is never written anywhere.

Input (stdin JSON):
``{"app_id": "...", "installation_id": "...", "private_key_pem": "..."}``

Output: ``{"token": "...", "expires_at": "..."}`` or
``{"error": {"message": "..."}}`` with exit code 2.

The RS256 app JWT is signed by shelling out to ``/usr/bin/openssl`` — the
standard-library-only posture used by the rest of the host (Python has no
in-tree RSA). This is the same two-step GitHub App flow (signed app JWT →
installation access token) infiverse's ghapp-broker performs.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import time
from typing import Any, NoReturn
import urllib.error
import urllib.request

OPENSSL_BIN = "/usr/bin/openssl"
GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 30
# GitHub rejects JWTs issued in the future; back-date iat and keep exp short.
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 540


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict):
        _fail("payload must be an object")
    app_id = payload.get("app_id")
    installation_id = payload.get("installation_id")
    private_key_pem = payload.get("private_key_pem")
    if not isinstance(app_id, str) or not app_id.isdigit():
        _fail("app_id must be the numeric GitHub App id")
    if not isinstance(installation_id, str) or not installation_id.isdigit():
        _fail("installation_id must be the numeric installation id")
    if not isinstance(private_key_pem, str) or not private_key_pem.startswith("-----BEGIN"):
        _fail("private_key_pem must be a PEM private key")
    try:
        result = mint_installation_token(app_id, installation_id, private_key_pem)
    except MintError as exc:
        _fail(str(exc))
        return
    print(json.dumps(result, sort_keys=True))


class MintError(Exception):
    pass


def mint_installation_token(app_id: str, installation_id: str, private_key_pem: str) -> dict[str, Any]:
    jwt = _app_jwt(app_id, private_key_pem)
    request = urllib.request.Request(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            minted = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise MintError(f"GitHub token mint failed with {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise MintError(f"GitHub token mint failed: {exc}") from exc
    token = minted.get("token") if isinstance(minted, dict) else None
    expires_at = minted.get("expires_at") if isinstance(minted, dict) else None
    if not isinstance(token, str) or not token or not isinstance(expires_at, str) or not expires_at:
        raise MintError("GitHub token mint returned an unexpected response shape")
    return {"token": token, "expires_at": expires_at}


def _app_jwt(app_id: str, private_key_pem: str) -> str:
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64url(
        json.dumps(
            {"iat": now - JWT_BACKDATE_SECONDS, "exp": now + JWT_LIFETIME_SECONDS, "iss": app_id},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode()
    signature = _rs256_sign(signing_input, private_key_pem)
    return f"{header}.{claims}.{_b64url(signature)}"


def _rs256_sign(data: bytes, private_key_pem: str) -> bytes:
    # The signing input (public JWT header+claims) goes in a temp file and
    # the private key arrives on openssl's stdin, so the key never touches
    # a filesystem.
    with tempfile.NamedTemporaryFile(prefix=".github-app-jwt.") as signing_input:
        signing_input.write(data)
        signing_input.flush()
        proc = subprocess.run(
            [OPENSSL_BIN, "dgst", "-sha256", "-sign", "/dev/stdin", signing_input.name],
            input=private_key_pem.encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=REQUEST_TIMEOUT_SECONDS,
            check=False,
        )
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", "replace")[:200]
        raise MintError(f"openssl RS256 signing failed: {detail}")
    return proc.stdout


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _fail(message: str) -> NoReturn:
    print(json.dumps({"error": {"message": message}}, sort_keys=True))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
