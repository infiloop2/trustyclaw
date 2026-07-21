"""Root helper behind ``read-aws-account``.

The admin service decrypts the Bedrock provider's connected AWS key pair
and hands it to this helper through the environment
(``TRUSTYCLAW_BEDROCK_AWS_ACCESS_KEY_ID`` / ``..._SECRET_ACCESS_KEY``); the
helper answers one question about it, never printing the secret:

- ``--attest``: ``{"access_key_id", "account_id", "arn", "user_id"}`` — who
  AWS says the credential belongs to, from ``sts:GetCallerIdentity``. The
  identity is bound to the credential by AWS itself, so agent-writable
  metadata is never what gets anchored. This is also the live validation:
  STS accepting the signature proves the key pair is valid right now.

Spend deliberately has no mode here: the operator-facing cost estimate is
computed live from the token usage the network proxy meters out of each
Bedrock response, so nothing polls Cost Explorer (which itself bills per
request and lags by hours).

Runs as root on purpose: the agent uid can only reach the local proxy (whose
Bedrock guard has no route for STS), and the admin uid has no egress at all,
while root egress is open. The secret arrives in this process's environment
and never leaves it.

Exit codes: 0 success, 1 failure, 2 usage, 3 AWS rejected the credential
(invalid key, bad signature, or missing permission) — the caller classifies
3 as an authentication problem rather than an infrastructure error.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, NoReturn
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from host.runtime.core.aws_sigv4 import sign_post

ACCESS_KEY_ID_ENV = "TRUSTYCLAW_BEDROCK_AWS_ACCESS_KEY_ID"
SECRET_ACCESS_KEY_ENV = "TRUSTYCLAW_BEDROCK_AWS_SECRET_ACCESS_KEY"
# STS is signed with its own fixed region: us-east-1 serves every account. It
# is not the Bedrock inference region, which only the proxy guard cares about.
STS_HOST = "sts.us-east-1.amazonaws.com"
REQUEST_TIMEOUT_SECONDS = 15
ACCESS_KEY_ID_RE = re.compile(r"^[A-Z0-9]{16,128}$")
# AWS rejects a bad credential or a missing permission with these codes.
_CREDENTIAL_REJECTED_MARKERS = (
    "invalidclienttokenid",
    "signaturedoesnotmatch",
    "unrecognizedclientexception",
    "accessdenied",
    "authfailure",
    "expiredtoken",
)


def main() -> None:
    if sys.argv[1:] != ["--attest"]:
        print("usage: read-aws-account --attest", file=sys.stderr)
        sys.exit(2)
    access_key_id, secret_access_key = _read_credentials()
    identity = _get_caller_identity(access_key_id, secret_access_key)
    identity["access_key_id"] = access_key_id
    print(json.dumps(identity, sort_keys=True))


def _read_credentials() -> tuple[str, str]:
    access_key_id = os.environ.get(ACCESS_KEY_ID_ENV, "").strip()
    secret_access_key = os.environ.get(SECRET_ACCESS_KEY_ENV, "").strip()
    if not ACCESS_KEY_ID_RE.fullmatch(access_key_id) or not secret_access_key:
        _fail("no usable AWS credentials in the helper environment")
    return access_key_id, secret_access_key


def _get_caller_identity(access_key_id: str, secret_access_key: str) -> dict[str, Any]:
    raw = _post_raw(
        host=STS_HOST,
        service="sts",
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        body=b"Action=GetCallerIdentity&Version=2011-06-15",
        content_type="application/x-www-form-urlencoded",
        extra_headers={},
    )
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        _fail("STS returned a response that is not XML")
    namespace = "{https://sts.amazonaws.com/doc/2011-06-15/}"
    result = root.find(f"{namespace}GetCallerIdentityResult")
    account = result.findtext(f"{namespace}Account") if result is not None else None
    arn = result.findtext(f"{namespace}Arn") if result is not None else None
    if not isinstance(account, str) or not re.fullmatch(r"\d{12}", account) or not isinstance(arn, str):
        _fail("STS returned an unexpected GetCallerIdentity response")
    identity: dict[str, Any] = {"account_id": account, "arn": arn}
    user_id = result.findtext(f"{namespace}UserId") if result is not None else None
    if isinstance(user_id, str) and user_id:
        identity["user_id"] = user_id
    return identity


def _post_raw(
    *,
    host: str,
    service: str,
    access_key_id: str,
    secret_access_key: str,
    body: bytes,
    content_type: str,
    extra_headers: dict[str, str],
) -> bytes:
    signed = sign_post(
        host=host,
        region="us-east-1",
        service=service,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        body=body,
        content_type=content_type,
        extra_headers=extra_headers,
    )
    request = urllib.request.Request(signed.url, data=signed.body, headers=signed.headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except OSError:
            pass
        normalized = detail.lower()
        if any(marker in normalized for marker in _CREDENTIAL_REJECTED_MARKERS):
            print(f"AWS rejected the credential: HTTP {exc.code}: {detail}", file=sys.stderr)
            sys.exit(3)
        _fail(f"AWS request to {host} failed: HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _fail(f"could not reach {host}: {exc}")


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
