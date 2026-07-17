"""Admin-side GitHub credential management.

The single fixed credential lives in the admin database (``github_credential``
table, no proxy grant). Two credential modes:

- ``pat``: the operator pastes a fine-grained personal access token.
- ``app``: the operator provides a GitHub App id, installation id, and PEM
  private key; the host mints short-lived installation tokens and refreshes
  them before their one-hour expiry.

Minted app tokens are installation-wide: which repositories a token can reach
is bounded by the App installation (operator-managed on GitHub), and every
request is enforced against the policy's repository list by the network
proxy's GitHub guard. The token is deliberately not re-scoped to the policy —
the proxy is the per-repository boundary, so the credential never has to
track, verify, or chase the repository list.

The agent never holds the token. The active working token lives in the
proxy-readable ``proxy_github_token`` row, and the proxy injects it into
policy-approved GitHub requests (stripping whatever ``Authorization`` the
agent sent) — see the GitHub guard's ``rewrite_request_headers``. There is no
agent-readable credential file, so there is nothing the agent can copy and
exfiltrate through other allowed egress, and revocation is one row delete.

Credential health and GitHub enablement are deliberately separate concerns:
the policy decides whether GitHub domains are reachable, the credential row
decides what token (if any) the proxy injects, and ``validation`` reports
only the credential's own health. The two meet in exactly one place:
``reconcile()`` keeps the working-token row present exactly while GitHub is
enabled with a credential stored. It runs after every credential change and
policy publish (``mint_fresh=True`` when the GitHub integration changed, because an
installation token only covers the repositories and App permissions granted
at mint time) and from the orchestrator poller every cycle. Any failure —
a mint, a row write — records itself in ``validation``, clears the working
token (fail closed: a token that may not match the stored credential or the
published repository list must not stay injectable), and is retried on the
next cycle. The working-token row is the only copy — there is no separate
mint cache that could drift from it.

Minting needs outbound network access, which the admin service does not have,
so GitHub API calls go through the fixed root helper
(``mint-github-app-token``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import subprocess
import threading
from typing import Any

from host.runtime import state
from host.runtime.network_policy import managed_integration

MINT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/trustyclaw-host/mint-github-app-token"]
HELPER_TIMEOUT_SECONDS = 60
# Re-mint when the current app token has less than this left; installation
# tokens live one hour, so this refreshes roughly every 45 minutes.
REFRESH_MARGIN = timedelta(minutes=15)
# One lock serializes every credential mutation (set/delete/reconcile) across
# the API threads and the poller: without it, a reconcile that is mid-mint
# when an operator deletes the credential or disables GitHub would publish
# its token after the removal. Mints normally take about a second; the worst
# case a blocked DELETE waits is the helper timeout.
_CREDENTIAL_LOCK = threading.RLock()


def metadata() -> dict[str, Any]:
    return state.read_github_credential_metadata()


def set_pat(token: str) -> dict[str, Any]:
    with _CREDENTIAL_LOCK:
        state.save_github_credential(
            {
                "mode": "pat",
                "token": token,
                "updated_at": state.utc_now(),
                "validation": {"status": "not_checked"},
            }
        )
        reconcile(mint_fresh=True)
        return metadata()


def set_app(app_id: str, installation_id: str, private_key_pem: str) -> dict[str, Any]:
    with _CREDENTIAL_LOCK:
        state.save_github_credential(
            {
                "mode": "app",
                "app_id": app_id,
                "installation_id": installation_id,
                "private_key_pem": private_key_pem,
                "updated_at": state.utc_now(),
                "validation": {"status": "not_checked"},
            }
        )
        reconcile(mint_fresh=True)
        return metadata()


def delete() -> dict[str, Any]:
    """Withdraw the working token first, then drop the stored credential:
    the proxy stops injecting from the moment the row clears."""
    with _CREDENTIAL_LOCK:
        state.save_proxy_github_token(None)
        state.save_github_credential(None)
        return metadata()


def reconcile(mint_fresh: bool = False) -> None:
    """Converge the proxy's working-token row: present exactly while GitHub
    is enabled and a credential is stored, absent otherwise. The row is the
    only copy of the working token — there is no separate mint cache to keep
    in step. ``mint_fresh`` forces a new App-mode mint (credential changes
    and any GitHub-integration change); the poller keeps the published
    token until it nears expiry. Never raises: any failure records itself in
    the credential's validation status, clears the working token (fail
    closed), and the next poller cycle retries."""
    with _CREDENTIAL_LOCK:
        credential = state.read_github_credential()
        if not credential or not _github_integration_enabled():
            state.save_proxy_github_token(None)
            return
        try:
            if credential.get("mode") == "pat":
                state.save_proxy_github_token(_pat_token(credential))
            else:
                published = state.read_proxy_github_token_record()
                if mint_fresh or published is None or _token_stale(published.get("expires_at")):
                    token, expires_at = _mint_app_token(credential)
                    state.save_proxy_github_token(token, expires_at)
        except HelperError as exc:
            state.set_github_credential_validation(
                {"status": "error", "message": str(exc), "checked_at": state.utc_now()}
            )
            state.save_proxy_github_token(None)
            return
        state.set_github_credential_validation({"status": "ok", "checked_at": state.utc_now()})


def _pat_token(credential: dict[str, Any]) -> str:
    token = credential.get("token")
    if not isinstance(token, str) or not token:
        raise HelperError("stored PAT credential has no token")
    return token


def _mint_app_token(credential: dict[str, Any]) -> tuple[str, str]:
    minted = _run_helper_json(
        MINT_COMMAND,
        {
            "app_id": credential.get("app_id"),
            "installation_id": credential.get("installation_id"),
            "private_key_pem": credential.get("private_key_pem"),
        },
    )
    token, expires_at = minted.get("token"), minted.get("expires_at")
    if not isinstance(token, str) or not token or not isinstance(expires_at, str):
        raise HelperError("mint returned an unexpected shape")
    return token, expires_at


class HelperError(Exception):
    pass


def _run_helper_json(
    command: list[str],
    payload: dict[str, Any],
    *,
    timeout: int = HELPER_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HelperError(f"{command[-1]} failed: {exc}") from exc
    try:
        value = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise HelperError(f"{command[-1]} returned invalid JSON") from exc
    if proc.returncode != 0:
        error = value.get("error", {}) if isinstance(value, dict) else {}
        message = error.get("message") if isinstance(error, dict) else None
        raise HelperError(message or proc.stderr.strip() or f"{command[-1]} failed")
    if not isinstance(value, dict):
        raise HelperError(f"{command[-1]} returned invalid JSON")
    return value


def _github_integration_enabled() -> bool:
    return managed_integration("github").get("enabled") is True


def _token_stale(expires_at: Any) -> bool:
    """Within the refresh margin (or past expiry): re-mint proactively."""
    if not isinstance(expires_at, str) or not expires_at:
        return True
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) + REFRESH_MARGIN >= expiry
