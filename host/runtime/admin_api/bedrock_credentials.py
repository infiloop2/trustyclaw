"""The Bedrock provider's shared credential surface.

Pi and Hermes share one operator credential: an IAM access key pair stored
encrypted in the admin database (secretbox), one STS liveness/identity read,
and one shared database row.

There is no login flow and no on-disk credential: the operator pastes an IAM
access key pair, AWS STS reports which account it belongs to, and the admin API
stores it encrypted only after validation succeeds. Because only the operator
API can write that row and the agent has no database access, storing the
credential is itself the approval — there is no separate anchor or paste gate.
Static keys never rotate on their own, so there is no per-task repin
convergence either.

The privileged STS call runs in the root ``read-aws-account`` helper because
only root has egress; the admin service passes either a new in-memory
candidate or the connected secret to the helper through its environment, so
validation stores only successful candidates and plaintext never touches
disk. The trusted proxy reads the same row and decrypts it only when
re-signing an allowed request.

Cost deliberately has no read here: the operator-facing estimate comes from
the token usage the proxy meters out of each Bedrock response
(``host.network_integrations.bedrock.usage``), not from a billing API.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from host.runtime.core import state

DEFAULT_ACCOUNT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/read-aws-account"]
# Named to match the env_keep entry in the sudoers file. Only the
# read-aws-account root helper consumes these: both launchers inject fixed
# dummy values instead, and the proxy re-signs with the real key.
ACCESS_KEY_ID_ENV = "TRUSTYCLAW_BEDROCK_AWS_ACCESS_KEY_ID"
SECRET_ACCESS_KEY_ENV = "TRUSTYCLAW_BEDROCK_AWS_SECRET_ACCESS_KEY"
# The attest helper makes one signed HTTPS round trip (15s inside the helper).
AWS_HELPER_TIMEOUT_SECONDS = 30
# The helper exits 3 when AWS itself rejected the credential or permission —
# the caller's signal for a final credential error rather than a retryable
# infrastructure error.
AWS_CREDENTIAL_REJECTED_EXIT = 3


class BedrockCredentialsError(RuntimeError):
    pass


class BedrockAuthenticationError(BedrockCredentialsError):
    pass


def credential_env(credential: tuple[str, str] | None = None) -> dict[str, str] | None:
    """The environment additions that carry the shared connected key
    pair to the read-aws-account root helper, or None when nothing is
    connected. Decrypts in the admin process (which owns the secretbox key).
    The agent-side launchers never receive these: both harnesses sign with the
    shared routing identity and the proxy re-signs."""
    if credential is None:
        credential = state.read_bedrock_credential_secret()
    if credential is None:
        return None
    access_key_id, secret_access_key = credential
    return {ACCESS_KEY_ID_ENV: access_key_id, SECRET_ACCESS_KEY_ENV: secret_access_key}


def account_status() -> tuple[str, str | None, dict[str, Any] | None]:
    """The connected-credential state: (status, error message, account).

    Credential submission stores the key and its STS identity/spend metadata
    atomically, so steady-state status is a local database read."""
    access_key_id = state.read_bedrock_access_key_id()
    if not access_key_id:
        return "awaiting_login", None, None
    account = state.read_bedrock_account()
    if account.get("access_key_id") != access_key_id:
        return "error", "AWS account metadata is unavailable; submit the credentials again", None
    return "active", None, account


def read_attested_identity(
    command: list[str] | None = None,
    *,
    credential: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """AWS-attested identity of the integration's connected credential.

    The root helper signs one ``sts:GetCallerIdentity`` with the key pair the
    admin service passes it, so the returned identity (account_id, arn,
    user_id, plus the access_key_id it belongs to) is bound to the credential
    by AWS, and STS accepting the signature is the live validation. Raises
    BedrockAuthenticationError when AWS rejected the credential and
    BedrockCredentialsError for any other failure."""
    value = _run_aws_helper(
        command or [*DEFAULT_ACCOUNT_COMMAND, "--attest"],
        "attest",
        credential=credential,
    )
    if not isinstance(value.get("account_id"), str) or not isinstance(value.get("arn"), str):
        raise BedrockCredentialsError("AWS account attestation response is incomplete")
    return value


def _run_aws_helper(
    argv: list[str],
    operation: str,
    *,
    credential: tuple[str, str] | None = None,
) -> dict[str, Any]:
    env = credential_env(credential)
    if env is None:
        raise BedrockCredentialsError("no AWS credentials are connected")
    try:
        proc = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=AWS_HELPER_TIMEOUT_SECONDS,
            env={**os.environ, **env},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BedrockCredentialsError(f"could not {operation} AWS credentials: {exc}") from exc
    if proc.returncode == AWS_CREDENTIAL_REJECTED_EXIT:
        detail = (proc.stderr or "").strip()[:500]
        raise BedrockAuthenticationError(detail or "AWS rejected the credential")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise BedrockCredentialsError(detail or f"AWS {operation} helper failed")
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise BedrockCredentialsError(f"AWS {operation} helper returned invalid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("access_key_id"), str):
        raise BedrockCredentialsError(f"AWS {operation} helper response is incomplete")
    return value
