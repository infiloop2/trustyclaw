"""Agent runtime orchestration: the worker pool that runs queued tasks and the
background poller that keeps the cached runtime status fresh. The admin API
delegates here; nothing in this module speaks HTTP.

Concurrency model: each runtime has its own ``WORKER_COUNT_PER_RUNTIME`` claim
cap, and up to ``WORKER_COUNT`` total tasks can run at once across runtimes.
Every task turn runs on a fresh runtime process: Codex turns resume their
provider thread by id on a new app-server; Claude Code, Pi, and Hermes turns
resume by recorded session id. Tasks on the same user thread are serialized,
while tasks on different threads run in parallel.

How the synchronization fits together:

- Two locks. The mutation lock (private to state.py, entered through
  ``state.mutation()``) guards every admin-state write cycle; reads are
  lock-free queries. ``_LIVE_LOCK`` guards ``_LIVE``, the registry of live
  runtime processes. The only place both are held is ``_claim_next_task``, and
  the nesting order there — the mutation lock, then _LIVE_LOCK — is the one
  legal order; no code path acquires them the other way around, so they cannot
  deadlock. Slow work (starting a runtime process, running a turn, closing a
  process) always happens with neither lock held. ``_REFRESH_LOCKS`` sits
  outside this pair: it serializes ``refresh_runtime_status`` per runtime, is
  deliberately held across slow provider probes, and is never acquired while
  holding (or by) anything else.
- Provider account trust is anchored in the database, not in locks: the
  stored provider account row is the operator-approved anchor. It is written
  only inside the refresh commit mutation (first capture requires an
  unexpired operator OAuth login; afterwards the probed account id must
  match) and only an operator reset (``reset_linked_account``) clears it.
  The anchor check, the anchor save, the proxy pin write, and the reset clear
  all run inside mutations, so whichever of a refresh and a reset commits
  second sees the other's state — a stale probe result can never re-approve
  an account (or republish a pin for one) that a concurrent reset just
  cleared. Claude identity is additionally server-attested: whenever the
  probed token hash differs from the anchored one, the account uuid comes
  from api.anthropic.com for that token (via the root helper), so
  agent-writable metadata is never what gets anchored.
- ``WORKER_WAKE`` is advisory, never load-bearing. One ``Event`` is shared by
  all workers, so worker A can consume a wakeup meant for worker B; every wait
  therefore uses a 5s timeout and re-checks the queue from scratch.
  Correctness never depends on a wakeup arriving — wakeups only reduce
  latency.
- A user thread is *unavailable* for claiming while a task on it is RUNNING
  in state, or while it has a ``_LIVE`` entry — its turn process is running,
  or a previous process for it is still shutting down (an entry outlives its
  task until the close completes). Both are checked in one place, under both
  locks, in ``_claim_next_task``; together they guarantee at most one live
  process per user thread.
- Cross-thread mutation of a running turn happens only through
  the runtime process ``close()`` method (the kill path). The worker that owns
  the turn then observes the dead process as an error; it never needs to be
  signalled directly.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any

from host.config import AGENT_RUNTIMES
from host.runtime.core import app_platform, network_policy, state
from host.runtime.admin_api import (
    bedrock_credentials,
    claude_code,
    codex_app_server,
    github_credential,
    github_repo_audit,
    hermes_agent,
    pi_agent,
    task_status,
)
from host.runtime.core.state import (
    read_claude_account,
    read_openai_account,
    save_bedrock_account,
    save_claude_account,
    save_openai_account,
    utc_now,
)
from host.runtime.admin_api.task_status import COMPLETED, FAILED, RUNNING

WORKER_COUNT_PER_RUNTIME = 3
# Every runtime owns an independent three-turn pool. The total grows with the
# runtime inventory so adding a harness cannot take capacity from its peers.
WORKER_COUNT = WORKER_COUNT_PER_RUNTIME * len(AGENT_RUNTIMES)
RUNTIME_RECHECK_SECONDS = 300  # re-verify an active agent login this often (it can expire)
RUNTIME_PENDING_RECHECK_SECONDS = 5  # poll more often while loading / awaiting login
# Live Claude probe results younger than this are reused, so the pre-task
# refresh and the five-second non-active poll stay local; under
# RUNTIME_RECHECK_SECONDS so the scheduled five-minute recheck always probes.
CLAUDE_LIVE_PROBE_RETRY_SECONDS = 240
WORKER_WAKE = threading.Event()
_MANAGED_PROVIDER_BY_RUNTIME = {
    "codex": "openai",
    "claude_code": "claude",
    "pi": "bedrock",
    "hermes": "bedrock",
}
CLAUDE_IDENTITY_ATTESTATION = "anthropic_oauth_profile"
OPENAI_OPERATOR_APPROVAL = "codex_device_login"
RUNTIME_LABELS = {"codex": "Codex", "claude_code": "Claude Code", "pi": "Pi", "hermes": "Hermes"}
# The Bedrock-backed harness runtimes have separate task processes and counters,
# but one provider connection, validation verdict, and derived health status.
BEDROCK_RUNTIMES = ("pi", "hermes")
OAUTH_RUNTIMES = ("codex", "claude_code")
DEACTIVATED_REASON = "agent runtime deactivated because its managed network provider is disabled"


@dataclass
class _Turn:
    """One live runtime process, registered at claim and removed only after
    its close completes (``closing`` marks the close owner)."""

    server: Any
    runtime_type: str
    thread_id: str
    task_id: str
    closing: bool = False


# runtime/thread key -> live turn process. An entry exists from task claim
# until the process close completes, so the claim rule can serialize per-user
# thread work through it.
_LIVE: dict[str, _Turn] = {}
_LIVE_LOCK = threading.Lock()
# Cached provider status, in process memory on purpose: it is derived health,
# re-computed from the provider CLIs within seconds of startup, so
# persisting it would only serve stale answers across restarts (a fresh
# process reports "loading" until the first poll). Writers replace whole
# records under _RUNTIME_STATUS_LOCK and never hold it around database work.
# Pi and Hermes both project the one ``bedrock`` record into their runtime
# counters; no independent Bedrock harness activation state exists.
# readers take the current record lock-free (records are never mutated in
# place), so no path holds this lock while entering state.mutation() and the
# lock graph stays acyclic.
_RUNTIME_STATUSES: dict[str, dict[str, str]] = {}
_RUNTIME_STATUS_LOCK = threading.Lock()
# One in-flight refresh per runtime. Pi is the canonical Bedrock status target;
# Hermes reads the same cached provider record and does not run its own poll.
_REFRESH_LOCKS: dict[str, Any] = {runtime_type: threading.Lock() for runtime_type in AGENT_RUNTIMES}
# Credential validation is slow and happens before the database mutation. Keep
# connect and disconnect as ordered product actions so an older request cannot
# publish after a newer reset or replacement has completed.
_BEDROCK_CONNECTION_LOCK = threading.Lock()
# The last live Claude probe verdict, keyed by the probed token hash:
# {"token_hash", "status", "error_message", "usage", "at"}. An awaiting_login
# verdict is final for that token (recovery is an operator login, which mints a
# new token); active and error verdicts expire after
# CLAUDE_LIVE_PROBE_RETRY_SECONDS. Written only under the claude refresh lock;
# in memory on purpose so a restart revalidates once from scratch.
_CLAUDE_LIVE_PROBE: dict[str, Any] | None = None
# The last Claude token attestation, keyed by token hash: a token's identity
# never changes, so one successful fetch answers every recheck of that token
# (a runtime parked in account-mismatch error rechecks every five seconds).
# A failed fetch is retried after CLAUDE_LIVE_PROBE_RETRY_SECONDS.
_CLAUDE_ATTESTATION_MEMO: tuple[str, dict[str, Any] | None, str | None, float] | None = None
class ProviderAccountTrustError(RuntimeError):
    pass


class ProviderAccountNotApproved(ProviderAccountTrustError):
    """Active agent-side credentials with no operator-approved anchor.

    Not an error state: the runtime simply awaits an operator login. The proxy
    pin stays cleared, so the unapproved credentials cannot reach the provider
    in the meantime."""


def runtime_status(runtime_type: str) -> str:
    return runtime_status_record(runtime_type)["status"]


def runtime_status_record(runtime_type: str) -> dict[str, str]:
    key = "bedrock" if runtime_type in BEDROCK_RUNTIMES else runtime_type
    return _RUNTIME_STATUSES.get(key, {"status": "loading"})


def all_runtime_status_records() -> dict[str, dict[str, str]]:
    return {runtime_type: runtime_status_record(runtime_type) for runtime_type in AGENT_RUNTIMES}


def _set_runtime_status(runtime_type: str, status: str, error_message: str | None = None) -> str:
    """Replace the provider status record and return its previous status."""
    record = {"status": status}
    if error_message is not None:
        record["error_message"] = error_message
    key = "bedrock" if runtime_type in BEDROCK_RUNTIMES else runtime_type
    with _RUNTIME_STATUS_LOCK:
        previous = _RUNTIME_STATUSES.get(key, {"status": "loading"})["status"]
        _RUNTIME_STATUSES[key] = record
    return previous


def agent_runtime_status() -> dict[str, Any]:
    # Reads only cached, in-memory status — never spawns an agent process — so
    # the request path (and /v1/health) is always fast. A background thread
    # keeps the status fresh.
    statuses = all_runtime_status_records()
    running = state.running_tasks()
    runtimes = []
    for runtime_type in sorted(AGENT_RUNTIMES):
        record = statuses.get(runtime_type, {})
        status = str(record.get("status", "loading"))
        active = sorted(
            (task["task_id"] for task in running if task["agent_runtime"] == runtime_type),
            key=_task_number,
        )
        response = {"type": runtime_type, "status": status, "active_task_ids": active}
        error_message = record.get("error_message")
        if status == "error" and error_message:
            response["error_message"] = error_message
        runtimes.append(response)
    return {"runtimes": runtimes}


def refresh_runtime_status(runtime_type: str, *, force_provider_probe: bool = False) -> str:
    """Re-derive the agent runtime status and cache it in memory. Runs the
    provider check outside the state transaction so a slow runtime process
    never blocks requests. Serialized per provider connection by
    _REFRESH_LOCKS; Pi and Hermes share the Bedrock lock."""
    with _REFRESH_LOCKS[runtime_type]:
        return _refresh_runtime_status_serialized(
            runtime_type, force_provider_probe=force_provider_probe
        )


def _refresh_runtime_status_serialized(runtime_type: str, *, force_provider_probe: bool = False) -> str:
    if not _runtime_network_enabled(runtime_type):
        return _mark_runtime_deactivated(runtime_type)
    provider = _provider_module(runtime_type)
    try:
        if runtime_type == "codex" and force_provider_probe:
            status, error_message, account = provider.account_status(force_provider_probe=True)
        else:
            status, error_message, account = provider.account_status()
    except Exception as exc:
        status, error_message, account = "error", f"unexpected error checking {runtime_type}: {exc!r}", None
    if runtime_type == "claude_code" and status == "active" and isinstance(account, dict):
        status, error_message, account = _live_claude_status(account, force_probe=force_provider_probe)
    # The status poll is the sole reader of the parked login server, so it has
    # now recorded any completed-login notification. Capture the first trusted
    # anchor here, in the same refresh, so this refresh's commit publishes the
    # proxy pin the moment the login lands instead of a poll cycle later.
    _capture_completed_codex_login(runtime_type)
    account_value = _active_account_value(runtime_type, status, account)
    attested: dict[str, Any] | None = None
    attest_error: str | None = None
    if runtime_type == "claude_code" and status == "active" and account_value:
        attested, attest_error = _claude_attestation(account_value)
    deactivated = False
    codex_login_to_close: str | None = None
    with state.mutation() as cur:
        if not _runtime_network_enabled(runtime_type):
            # The one policy re-check, inside the mutation: a policy disable
            # that landed while the slow probe ran must not be overwritten by
            # the probe's stale result. The deactivated status, the OAuth
            # clear, and the pin clear commit in this same transaction.
            _mark_runtime_deactivated_in(cur, runtime_type)
            deactivated = True
        else:
            if status == "active" and runtime_type in OAUTH_RUNTIMES:
                # The anchor check, the anchor save, and the pin write below
                # share this mutation, so they serialize with an operator
                # reset: a slow probe that started before the reset sees the
                # anchor already cleared here and cannot re-approve the old
                # account or republish its pin.
                try:
                    account_value = _trusted_active_account(cur, runtime_type, account_value, attested, attest_error)
                except ProviderAccountNotApproved:
                    status, error_message, account_value = "awaiting_login", None, None
                except ProviderAccountTrustError as exc:
                    status, error_message, account_value = "error", str(exc), None
            previous = _set_runtime_status(
                runtime_type, status, error_message if status == "error" and error_message else None
            )
            if status == "active":
                # The device code is spent (or moot) once the account is active.
                # Without this, a later session expiry would resurface the stale
                # record instead of letting the operator start a fresh login.
                if runtime_type == "codex":
                    completed_login = state.oauth_login("codex", cur)
                    codex_login_to_close = _string_field(completed_login, "login_id") if completed_login else None
                if runtime_type == "claude_code":
                    state.set_oauth_login(cur, "claude", None)
                    save_claude_account(account_value, cur)
                elif runtime_type in BEDROCK_RUNTIMES:
                    # The Bedrock account row is written once at credential
                    # submission and only cleared by a disconnect; the status
                    # refresh has nothing to store for it.
                    pass
                else:
                    state.set_oauth_login(cur, "codex", None)
                    _stamp_usage_checked_at(account_value, "codex_usage", utc_now())
                    save_openai_account(account_value, cur)
            if runtime_type in OAUTH_RUNTIMES:
                _sync_runtime_proxy_pin_in(cur, runtime_type, account_value if status == "active" else None)
            if runtime_type in OAUTH_RUNTIMES and previous == "awaiting_login" and status == "active":
                state.append_agent_event(cur, "agent_runtime.login_completed", None, {"agent_runtime": runtime_type})
            if previous != "active" and status == "active":
                for changed_runtime in (
                    BEDROCK_RUNTIMES if runtime_type in BEDROCK_RUNTIMES else (runtime_type,)
                ):
                    state.append_agent_event(
                        cur,
                        "agent_runtime.active",
                        None,
                        {"agent_runtime": changed_runtime},
                    )
    if deactivated:
        deactivate_runtime(runtime_type, DEACTIVATED_REASON)
        return "deactivated"
    if status == "active":
        # The login flow (first login or reauth) has landed, so its parked
        # device-login server is done. Close the one for this login id, scoped so
        # a login started meanwhile survives, or later status checks would keep
        # polling the leftover login process instead of short-lived servers.
        if codex_login_to_close:
            codex_app_server.close_completed_login_server(codex_login_to_close)
        if runtime_type == "claude_code":
            _backfill_claude_usage(account_value)
    return status


def _capture_completed_codex_login(runtime_type: str) -> None:
    """Persist the first trusted OpenAI anchor once the login has completed.

    Runs right after the status poll, which is the sole reader of the parked
    device-code app-server and has therefore recorded the successful
    account/login/completed notification. A stored OAuth row means the operator
    saw a device code, not that the login completed, so capture still requires
    that completion for the exact login id; the account id itself is read from
    the provider-signed login tokens promptly after completion (see
    read_completed_device_login_account_id). The surrounding refresh publishes
    the proxy pin when it commits, right after this capture.
    """
    if runtime_type != "codex":
        return
    if _trusted_openai_account_id(read_openai_account()):
        return  # anchor already established; nothing to capture
    login = _current_oauth_login("codex")
    login_id = _string_field(login, "login_id") if login else None
    if not login_id:
        return
    try:
        account_id = codex_app_server.read_completed_device_login_account_id(login_id)
    except codex_app_server.CodexAppServerError:
        # A helper hiccup must not fail the refresh; the poller already
        # classified the runtime state and the next refresh retries the capture.
        return
    if account_id:
        _capture_completed_codex_oauth_login(login_id, account_id)


def _capture_completed_codex_oauth_login(login_id: str, account_id: str) -> bool:
    """Persist the first OpenAI anchor only for the completed device-code flow."""
    with state.mutation() as cur:
        trusted_account_id = _trusted_openai_account_id(read_openai_account(cur))
        if trusted_account_id:
            return trusted_account_id == account_id
        login = _current_oauth_login("codex", cur)
        current_login_id = _string_field(login, "login_id") if login else None
        if login is None or current_login_id != login_id:
            return False
        state.set_oauth_login(cur, "codex", login | {"status": "completed"})
        save_openai_account(_with_openai_operator_approval({"account_id": account_id}), cur)
        return True


def _active_account_value(runtime_type: str, status: str, account: Any) -> dict[str, Any] | None:
    if status != "active":
        return None
    if isinstance(account, dict):
        return account
    if runtime_type == "codex" and isinstance(account, str) and account:
        return {"account_id": account}
    return None


def _live_claude_status(
    account: dict[str, Any],
    *,
    force_probe: bool = False,
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Validate a steady Claude credential through the CLI that owns refresh.

    First capture and an already-observed token rotation skip this probe because
    the root profile attestation below is their live validation. For a steady
    token, `/usage` authenticates through the agent proxy and gives Claude Code
    a chance to refresh. A refresh writes the new token before its first
    data-plane retry, which the old proxy pin correctly denies. Detect that hash
    change after either success or failure and pass the candidate onward for
    provider attestation and atomic repinning instead of misclassifying it as a
    broken login.

    Each probe's verdict is memoized per token hash (_CLAUDE_LIVE_PROBE), so
    the probe itself runs at most once per CLAUDE_LIVE_PROBE_RETRY_SECONDS:
    pre-task refreshes and the five-second non-active poll reuse the verdict
    instead of generating provider traffic. An explicit operator refresh
    bypasses the memo. An awaiting_login verdict never expires on automatic
    checks; the token is rejected, and recovery is an operator login, account
    reset, or an operator-forced recheck that succeeds.
    """
    global _CLAUDE_LIVE_PROBE
    token_hash = _string_field(account, "access_token_sha256")
    stored = read_claude_account()
    if (
        not token_hash
        or not _trusted_claude_account_id(stored)
        or _string_field(stored, "access_token_sha256") != token_hash
    ):
        return "active", None, account
    memo = _CLAUDE_LIVE_PROBE
    if not force_probe and memo is not None and memo["token_hash"] == token_hash:
        if memo["status"] == "awaiting_login":
            return "awaiting_login", None, None
        if time.monotonic() - memo["at"] < CLAUDE_LIVE_PROBE_RETRY_SECONDS:
            if memo["status"] == "error":
                return "error", memo["error_message"], None
            refreshed = dict(account)
            if memo["usage"]:
                refreshed["claude_usage"] = dict(memo["usage"])
            return "active", None, refreshed
    return _probe_claude_status(account, token_hash)


def _probe_claude_status(
    account: dict[str, Any], token_hash: str
) -> tuple[str, str | None, dict[str, Any] | None]:
    global _CLAUDE_LIVE_PROBE
    usage: dict[str, Any] = {}
    probe_error: claude_code.ClaudeCodeError | None = None
    try:
        usage = claude_code.read_claude_usage()
    except claude_code.ClaudeCodeError as exc:
        probe_error = exc
    try:
        current = claude_code.read_claude_account()
    except claude_code.ClaudeCodeError as exc:
        return _memo_claude_probe(
            token_hash, "error", f"could not read Claude account after live authentication: {exc}"
        )
    if not current:
        return _memo_claude_probe(
            token_hash, "error", "Claude OAuth token metadata disappeared during live authentication"
        )
    refreshed = dict(account)
    refreshed.update(current)
    usage = _checked_claude_usage(usage)
    current_hash = _string_field(refreshed, "access_token_sha256")
    if current_hash != token_hash:
        # The CLI rotated the token during the probe: no verdict is memoized
        # for either hash. This refresh attests and commits the new token, and
        # its first scheduled recheck probes it.
        if usage:
            refreshed["claude_usage"] = usage
        return "active", None, refreshed
    if isinstance(probe_error, claude_code.ClaudeAuthenticationError):
        return _memo_claude_probe(token_hash, "awaiting_login", None)
    if probe_error is not None:
        return _memo_claude_probe(
            token_hash, "error", f"could not validate Claude authentication: {probe_error}"
        )
    if usage:
        refreshed["claude_usage"] = usage
    _memo_claude_probe(token_hash, "active", None, usage)
    return "active", None, refreshed


def _memo_claude_probe(
    token_hash: str, status: str, error_message: str | None, usage: dict[str, Any] | None = None
) -> tuple[str, str | None, dict[str, Any] | None]:
    global _CLAUDE_LIVE_PROBE
    _CLAUDE_LIVE_PROBE = {
        "token_hash": token_hash,
        "status": status,
        "error_message": error_message,
        "usage": dict(usage) if usage else {},
        "at": time.monotonic(),
    }
    return status, error_message, None


def _backfill_claude_usage(account: dict[str, Any] | None) -> None:
    """One usage read for an active token the steady probe has not covered.

    A first-capture or just-rotated token is validated by attestation, not the
    usage probe: its proxy pin only goes live when the refresh commits, so the
    probe would have been denied. Run it once now, right after that commit,
    so the admin UI shows usage immediately instead of after the next
    five-minute recheck. Metadata only: failures are ignored and never touch
    the runtime status; the next scheduled probe classifies auth state."""
    token_hash = _string_field(account, "access_token_sha256") if account else None
    if not token_hash or not account or "claude_usage" in account:
        return
    memo = _CLAUDE_LIVE_PROBE
    if memo is not None and memo["token_hash"] == token_hash:
        return  # this round's steady probe already answered for this token
    try:
        usage = claude_code.read_claude_usage()
    except claude_code.ClaudeCodeError:
        return
    usage = _checked_claude_usage(usage)
    _memo_claude_probe(token_hash, "active", None, usage)
    if not usage:
        return
    with state.mutation() as cur:
        stored = read_claude_account(cur)
        if _string_field(stored, "access_token_sha256") != token_hash:
            return  # the credential moved on; let its own refresh fetch usage
        stored["claude_usage"] = dict(usage)
        save_claude_account(stored, cur)


def _checked_claude_usage(usage: dict[str, Any]) -> dict[str, Any]:
    if not usage:
        return {}
    checked = dict(usage)
    checked["last_checked_at"] = utc_now()
    return checked


def replace_and_validate_bedrock_credentials(
    access_key_id: str,
    secret_access_key: str,
    region: str,
) -> tuple[str, str | None]:
    """Validate and replace the connection as one ordered operator action."""
    with _BEDROCK_CONNECTION_LOCK:
        return _replace_and_validate_bedrock_credentials(
            access_key_id,
            secret_access_key,
            region,
        )


def _replace_and_validate_bedrock_credentials(
    access_key_id: str,
    secret_access_key: str,
    region: str,
) -> tuple[str, str | None]:
    """Replace and synchronously validate the one Bedrock credential.

    The credential candidate exists only in the admin/root-helper process
    environments until the STS identity read succeeds. Both the credential and
    its region and identity metadata are then stored in one transaction. A
    rejected replacement leaves the previous validated connection unchanged.
    """
    credential = (access_key_id, secret_access_key)
    try:
        identity = bedrock_credentials.read_attested_identity(credential=credential)
    except bedrock_credentials.BedrockAuthenticationError as exc:
        return "error", f"AWS rejected the credential: {exc}"
    except bedrock_credentials.BedrockCredentialsError as exc:
        return "error", f"could not validate AWS credentials: {exc}"
    if _string_field(identity, "access_key_id") != access_key_id:
        return "error", "AWS validation returned a different access key id"
    account: dict[str, Any] = {"access_key_id": access_key_id}
    for key in ("account_id", "arn", "user_id"):
        field = _string_field(identity, key)
        if field:
            account[key] = field
    with state.mutation() as cur:
        state.save_bedrock_credential(access_key_id, secret_access_key, region, cur)
        save_bedrock_account(account, cur)
    return "active", None


def _claude_attestation(account: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Attest the probed Claude token when its hash is not already anchored.

    The profile call is a network round trip, so it runs out here only when the
    token still needs attestation: a token recorded on a server-attested anchor
    was attested when it was first seen, and steady-state refreshes stay local.
    A token's attested identity never changes, so the result is memoized per
    token hash; a runtime parked in account-mismatch error therefore rechecks
    against the memo instead of refetching the profile every five seconds.
    The attestation itself is a read-only profile fetch — the trust
    decision that consumes it runs later, under the commit mutation, against
    the then-current anchor. Returns (attested identity, None) or (None,
    error message)."""
    global _CLAUDE_ATTESTATION_MEMO
    token_hash = _string_field(account, "access_token_sha256")
    if not token_hash:
        return None, None  # the trust check rejects the account outright
    if not _claude_attestation_allowed(token_hash):
        return None, None
    memo = _CLAUDE_ATTESTATION_MEMO
    if memo is not None and memo[0] == token_hash:
        if memo[1] is not None:
            return memo[1], None
        if time.monotonic() - memo[3] < CLAUDE_LIVE_PROBE_RETRY_SECONDS:
            return None, memo[2]
    try:
        attested = claude_code.read_attested_identity(expected_token_sha256=token_hash)
    except claude_code.ClaudeCodeError as exc:
        _CLAUDE_ATTESTATION_MEMO = (token_hash, None, str(exc), time.monotonic())
        return None, str(exc)
    _CLAUDE_ATTESTATION_MEMO = (token_hash, attested, None, time.monotonic())
    return attested, None


def _claude_attestation_allowed(token_hash: str) -> bool:
    stored = read_claude_account()
    trusted_account_id = _trusted_claude_account_id(stored)
    if trusted_account_id:
        # A steady-state token still on the anchor needs no re-attestation; a
        # rotated token does.
        return _string_field(stored, "access_token_sha256") != token_hash
    return _claude_first_capture_approved(token_hash)


def _claude_first_capture_approved(token_hash: str, cur: Any = None) -> bool:
    """First capture is approved only for the token the operator's completed
    login produced. Completion records sha256(accessToken) read right after
    the login helper finished, so agent credentials swapped after that moment
    do not inherit the approval (the remaining swap window is the milliseconds
    between the CLI writing the file and the completion read; the linked
    account is also shown to the operator once pinned). A completion whose
    hash read failed carries no usable first-capture approval."""
    login = _completed_oauth_login("claude", cur)
    if login is None:
        return False
    approved_hash = _string_field(login, "access_token_sha256")
    return approved_hash == token_hash


def _trusted_active_account(
    cur: Any,
    runtime_type: str,
    account: dict[str, Any] | None,
    attested: dict[str, Any] | None = None,
    attest_error: str | None = None,
) -> dict[str, Any]:
    """Validate a probed active account against the operator-approved anchor.

    The anchor is the stored account id: first captured only while an operator
    OAuth login is in flight, immutable afterwards until an operator reset
    clears it. OpenAI's anchor is additionally enforced per request by the
    proxy header guard. Claude's identity is server-attested: the anchor is
    the account uuid api.anthropic.com reports for the token itself, checked
    whenever the token changes, so agent-writable metadata is never trusted.
    """
    if not account:
        raise ProviderAccountTrustError(f"{runtime_type} reported active without account metadata")
    if runtime_type == "claude_code":
        return _trusted_claude_account(cur, account, attested, attest_error)
    return _trusted_openai_account(cur, account)


def _trusted_openai_account(cur: Any, account: dict[str, Any]) -> dict[str, Any]:
    account_id = _string_field(account, "account_id")
    if not account_id:
        raise ProviderAccountTrustError("OpenAI account id is not available")
    trusted_account_id = _trusted_openai_account_id(read_openai_account(cur))
    if trusted_account_id:
        if account_id != trusted_account_id:
            raise ProviderAccountTrustError("OpenAI account changed; reset the linked account under Internet Access and Tools in the admin UI")
        return _with_openai_operator_approval(account)
    raise ProviderAccountNotApproved("OpenAI account is not operator-approved; start OAuth login from the admin UI")


def _trusted_openai_account_id(account: dict[str, Any]) -> str | None:
    if _string_field(account, "operator_approval") != OPENAI_OPERATOR_APPROVAL:
        return None
    return _string_field(account, "account_id")


def _with_openai_operator_approval(account: dict[str, Any]) -> dict[str, Any]:
    approved = dict(account)
    approved["operator_approval"] = OPENAI_OPERATOR_APPROVAL
    return approved


def _trusted_claude_account(
    cur: Any, account: dict[str, Any], attested: dict[str, Any] | None, attest_error: str | None
) -> dict[str, Any]:
    token_hash = _string_field(account, "access_token_sha256")
    if not token_hash:
        raise ProviderAccountTrustError("Claude account token is not available")
    stored = read_claude_account(cur)
    # Only a server-attested row is a trusted anchor; anything else re-captures
    # through the first-capture gate below, exactly like a fresh box.
    trusted_account_id = _trusted_claude_account_id(stored)
    if (
        trusted_account_id
        and _string_field(stored, "access_token_sha256") == token_hash
    ):
        # This exact token was attested when it was first anchored; its
        # identity comes from that attestation, never from the agent's local
        # metadata (which the probe may carry, forged or stale).
        return _with_identity(account, trusted_account_id, stored)
    if not trusted_account_id and not _claude_first_capture_approved(token_hash, cur):
        raise ProviderAccountNotApproved("Claude account is not operator-approved; start OAuth login from the admin UI")
    if attested is None:
        raise ProviderAccountTrustError(
            attest_error or "Claude account is not attested yet; retrying on the next refresh"
        )
    if _string_field(attested, "access_token_sha256") != token_hash:
        raise ProviderAccountTrustError("Claude token changed while attesting; retrying on the next refresh")
    attested_uuid = _string_field(attested, "account_uuid")
    if not attested_uuid:
        raise ProviderAccountTrustError("Claude account attestation has no account uuid")
    if trusted_account_id and attested_uuid != trusted_account_id:
        raise ProviderAccountTrustError("Claude account changed; reset the linked account under Internet Access and Tools in the admin UI")
    return _with_identity(account, attested_uuid, attested)


def _with_identity(account: dict[str, Any], account_id: str, source: dict[str, Any]) -> dict[str, Any]:
    """The probed account with its identity fields replaced by trusted ones
    (the stored anchor or a fresh attestation; attestations use the
    ``organization_uuid`` key, stored anchors ``organization_id``)."""
    merged = dict(account)
    merged["account_id"] = account_id
    email = _string_field(source, "email")
    if email:
        merged["email"] = email
    organization_id = _string_field(source, "organization_id") or _string_field(source, "organization_uuid")
    if organization_id:
        merged["organization_id"] = organization_id
    if _string_field(source, "account_uuid"):
        merged["identity_attestation"] = CLAUDE_IDENTITY_ATTESTATION
        merged["identity_attested_at"] = utc_now()
    elif _claude_anchor_is_server_attested(source):
        merged["identity_attestation"] = CLAUDE_IDENTITY_ATTESTATION
        attested_at = _string_field(source, "identity_attested_at")
        if attested_at:
            merged["identity_attested_at"] = attested_at
    return merged


def _claude_anchor_is_server_attested(account: dict[str, Any]) -> bool:
    return _string_field(account, "identity_attestation") == CLAUDE_IDENTITY_ATTESTATION


def _trusted_claude_account_id(account: dict[str, Any]) -> str | None:
    """The Claude anchor id, or None when the row is not a trusted anchor.

    Mirrors _trusted_openai_account_id: the operator-approval marker is the
    server attestation, so a row without it is not an anchor and re-captures
    through a fresh operator login."""
    if not _claude_anchor_is_server_attested(account):
        return None
    return _string_field(account, "account_id")


def _current_oauth_login(key: str, cur: Any = None) -> dict[str, Any] | None:
    login = state.oauth_login(key, cur)
    expires_at = login.get("expires_at") if login else None
    if not isinstance(expires_at, str) or expires_at <= utc_now():
        if login is not None:
            if cur is not None:
                state.set_oauth_login(cur, key, None)
            else:
                with state.mutation() as fresh:
                    state.set_oauth_login(fresh, key, None)
        return None
    return login


def _completed_oauth_login(key: str, cur: Any = None) -> dict[str, Any] | None:
    login = _current_oauth_login(key, cur)
    if login is None or login.get("status") != "completed":
        return None
    return login


def mark_oauth_login_completed(key: str, access_token_sha256: str | None = None) -> bool:
    with state.mutation() as cur:
        login = _current_oauth_login(key, cur)
        if login is None:
            return False
        completed = login | {"status": "completed"}
        if access_token_sha256:
            completed["access_token_sha256"] = access_token_sha256
        state.set_oauth_login(cur, key, completed)
        return True


def _sync_runtime_proxy_pin_in(cur: Any, runtime_type: str, account: dict[str, Any] | None) -> None:
    """Write the runtime's proxy pin inside the caller's mutation, so pin and
    anchor/status commit in one transaction."""
    if runtime_type == "claude_code":
        state.save_proxy_claude_account(account, cur)
        return
    account_id = _string_field(account, "account_id") if account else None
    state.save_proxy_openai_account_id(account_id, cur)


def _string_field(value: dict[str, Any], key: str) -> str | None:
    field = value.get(key)
    return field if isinstance(field, str) and field else None


def _stamp_usage_checked_at(account: dict[str, Any] | None, usage_key: str, checked_at: str) -> None:
    if not account:
        return
    usage = account.get(usage_key)
    if isinstance(usage, dict):
        usage["last_checked_at"] = checked_at


def _mark_runtime_deactivated(runtime_type: str) -> str:
    with state.mutation() as cur:
        _mark_runtime_deactivated_in(cur, runtime_type)
    deactivate_runtime(runtime_type, DEACTIVATED_REASON)
    return "deactivated"


def _mark_runtime_deactivated_in(cur: Any, runtime_type: str) -> None:
    previous = _set_runtime_status(runtime_type, "deactivated")
    _clear_oauth_login_in(cur, runtime_type)
    if runtime_type in OAUTH_RUNTIMES:
        _sync_runtime_proxy_pin_in(cur, runtime_type, None)
    if previous != "deactivated":
        for changed_runtime in (
            BEDROCK_RUNTIMES if runtime_type in BEDROCK_RUNTIMES else (runtime_type,)
        ):
            state.append_agent_event(
                cur,
                "agent_runtime.deactivated",
                None,
                {"agent_runtime": changed_runtime},
            )


def _clear_oauth_login_in(cur: Any, runtime_type: str) -> None:
    # The Bedrock runtimes have no OAuth flow (their credential lives encrypted
    # in the database), so there is no login record to clear for them.
    if runtime_type in BEDROCK_RUNTIMES:
        return
    state.set_oauth_login(cur, "claude" if runtime_type == "claude_code" else "codex", None)


def runtime_network_enabled(runtime_type: str) -> bool:
    return _runtime_network_enabled(runtime_type)


def reconcile_runtime_status_after_policy_change() -> None:
    """Synchronize cached runtime state after a policy update.

    Disabled runtimes are deactivated synchronously because that fails running
    tasks and closes their processes; deactivation never probes, so it skips
    the provider-connection refresh serialization rather than wait out an in-flight
    slow probe (which re-checks the policy inside its own commit anyway).
    Enabled runtimes are refreshed in the background: a policy change may have
    re-enabled a runtime whose poller still has a stale long active-runtime
    deadline, but the network-policy request path must not block on provider
    CLI checks.
    """
    enabled: list[str] = []
    for runtime_type in ("codex", "claude_code", "pi"):
        if not _runtime_network_enabled(runtime_type):
            _mark_runtime_deactivated(runtime_type)
        else:
            enabled.append(runtime_type)
    if enabled:
        threading.Thread(target=_refresh_runtimes, args=(tuple(enabled),), daemon=True).start()


def _refresh_runtimes(runtime_types: tuple[str, ...]) -> None:
    for runtime_type in runtime_types:
        try:
            refresh_runtime_status(runtime_type)
        except Exception:
            continue


def deactivate_runtime(runtime_type: str, reason: str) -> None:
    """Stop every live process for a non-active runtime and fail its in-flight
    tasks. Queued tasks need no handling here: the next claim fails each one
    with the runtime's non-active status."""
    for stopped_runtime in (
        BEDROCK_RUNTIMES if runtime_type in BEDROCK_RUNTIMES else (runtime_type,)
    ):
        _stop_runtime_processes(stopped_runtime, reason)


def reset_linked_account(runtime_type: str) -> None:
    """Operator reset: delete the linked-account guard and stop old sessions.

    One mutation clears the trusted account anchor, its proxy pin, and any
    pending OAuth approval; a best-effort close of a parked login flow
    follows. Live runtime processes are closed and running tasks are failed
    so no process from the old linked account keeps executing while the caller
    clears local auth files and refreshes status. A device login parked at
    the same instant is torn down with everything else (starting a login
    while resetting the account is contradictory; the operator just starts
    a fresh login)."""
    if runtime_type not in OAUTH_RUNTIMES:
        raise ValueError("linked-account reset is only available for OAuth runtimes")
    global _CLAUDE_LIVE_PROBE
    _reset_linked_account_in_state(runtime_type)
    # The reset replaces the credential any remembered live-validation verdict
    # was about; the next login validates from scratch.
    if runtime_type == "claude_code":
        _CLAUDE_LIVE_PROBE = None
    else:
        codex_app_server.clear_live_validation_failure()
    _close_login_flow(runtime_type)
    _stop_runtime_processes(runtime_type, "linked provider account was reset by the operator")


def _reset_linked_account_in_state(runtime_type: str) -> None:
    with state.mutation() as cur:
        next_status = "awaiting_login" if _runtime_network_enabled(runtime_type) else "deactivated"
        _set_runtime_status(runtime_type, next_status)
        _clear_oauth_login_in(cur, runtime_type)
        if runtime_type == "claude_code":
            save_claude_account(None, cur)
        else:
            save_openai_account(None, cur)
        _sync_runtime_proxy_pin_in(cur, runtime_type, None)
        state.append_agent_event(cur, "agent_runtime.linked_account_reset", None, {"agent_runtime": runtime_type})


def disconnect_bedrock_connection() -> None:
    """Disconnect the one AWS connection and stop both harnesses' work."""
    with _BEDROCK_CONNECTION_LOCK:
        with state.mutation() as cur:
            save_bedrock_account(None, cur)
            state.delete_bedrock_credential(cur)
            cur.execute("SELECT 1 FROM managed_integrations WHERE integration = 'bedrock'")
            enabled = cur.fetchone() is not None
            next_status = "awaiting_login" if enabled else "deactivated"
            error_message = None
            _set_runtime_status("pi", next_status, error_message)
            for runtime_type in BEDROCK_RUNTIMES:
                state.append_agent_event(
                    cur,
                    "agent_runtime.linked_account_reset",
                    None,
                    {"agent_runtime": runtime_type},
                )
        for runtime_type in BEDROCK_RUNTIMES:
            _stop_runtime_processes(
                runtime_type,
                "shared AWS Bedrock connection was reset by the operator",
            )


def _close_login_flow(runtime_type: str) -> None:
    # Best-effort: the pending OAuth record is already gone, so a parked login
    # process that resists closing is inert; never fail the caller over it.
    try:
        if runtime_type == "codex":
            codex_app_server.close_login_server()
        elif runtime_type == "claude_code":
            claude_code.close_login_process()
        # The Bedrock runtimes (pi, hermes) have no login process to close.
    except Exception:
        pass


def _stop_runtime_processes(runtime_type: str, reason: str) -> None:
    with state.mutation() as cur:
        for task_id in state.fail_running_tasks(cur, reason, runtime=runtime_type):
            state.append_agent_event(cur, "task.failed", task_id, {"error_message": reason})
    with _LIVE_LOCK:
        turns = [turn for turn in _LIVE.values() if turn.runtime_type == runtime_type]
    for turn in turns:
        _close_turn(_live_key(turn.runtime_type, turn.thread_id), turn.server)
    WORKER_WAKE.set()


def runtime_status_loop() -> None:
    refresh_targets = ("codex", "claude_code", "pi")
    next_check_at = {runtime_type: 0.0 for runtime_type in refresh_targets}
    while True:
        now = time.monotonic()
        try:
            for runtime_type in refresh_targets:
                if now < next_check_at[runtime_type]:
                    continue
                status = refresh_runtime_status(runtime_type)
                delay = RUNTIME_RECHECK_SECONDS if status == "active" else RUNTIME_PENDING_RECHECK_SECONDS
                next_check_at[runtime_type] = time.monotonic() + delay
        except Exception:
            # Keep the loop alive; retry soon because the failed refresh did
            # not update that runtime's cached state.
            time.sleep(RUNTIME_PENDING_RECHECK_SECONDS)
            continue
        sleep_for = min(max(0.0, due - time.monotonic()) for due in next_check_at.values())
        time.sleep(min(max(sleep_for, 0.1), RUNTIME_PENDING_RECHECK_SECONDS))


def start_workers() -> None:
    # Converge GitHub credentials before any worker can claim a task: after a
    # restart the persisted App installation token may already be expired, and
    # a task's first git/gh call must not run against a stale token file.
    # This blocks startup by at most one mint (~1s) and does not hold up the
    # other runtimes.
    try:
        github_credential.reconcile()
        converged = True
    except Exception:
        # reconcile() records its own failures; reaching here means it could
        # not even run (the database briefly unavailable during startup), so
        # the refresh loop retries quickly rather than waiting a full cycle.
        converged = False
    for _ in range(WORKER_COUNT):
        threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=runtime_status_loop, daemon=True).start()
    threading.Thread(target=github_credential_refresh_loop, args=(converged,), daemon=True).start()


GITHUB_CREDENTIAL_REFRESH_CHECK_SECONDS = 300
GITHUB_CREDENTIAL_RETRY_SECONDS = 10


def github_credential_refresh_loop(converged: bool) -> None:
    # Converge the installed token file every cycle: App installation tokens
    # live one hour and are re-minted inside the refresh margin, and any
    # earlier failure (mint, install, disable-time removal) is retried here.
    # The initial convergence ran synchronously in start_workers before any
    # worker could claim a task; while a convergence attempt cannot run at
    # all (the database unavailable), retry on the short interval so workers
    # are not claiming tasks against stale credential files for a full cycle.
    while True:
        time.sleep(GITHUB_CREDENTIAL_REFRESH_CHECK_SECONDS if converged else GITHUB_CREDENTIAL_RETRY_SECONDS)
        try:
            github_credential.reconcile()
            github_repo_audit.refresh()
            converged = True
        except Exception:
            converged = False


def worker_loop() -> None:
    # All workers share WORKER_WAKE, so this worker may consume a wakeup meant
    # for another (clear() after a single wait). That is fine by design: the
    # 5s timeout re-checks the queue regardless, so a lost wakeup costs at
    # most 5s of latency, never a stuck task.
    while True:
        WORKER_WAKE.wait(timeout=5)
        WORKER_WAKE.clear()
        try:
            run_next_task()
        except Exception:
            # run_next_task fails its claimed task internally; anything that
            # still escapes (e.g. state file I/O errors) must not kill the
            # worker thread — back off briefly and keep serving the queue.
            time.sleep(2)


def run_next_task() -> None:
    claimed = _claim_next_task()
    if claimed is None:
        return
    task_id, runtime_type, thread_id, input_message, model, effort, provider_session_id = claimed
    provider = _provider_module(runtime_type)

    def steers() -> list[str]:
        return state.task_steers(task_id)

    def steer_delivered(message: str) -> None:
        # Drop a delivered steer from the task's pending queue. Its content is
        # already preserved as a task.message event, so nothing is lost, and
        # the queue stays bounded: task_steers holds only undelivered steers.
        with state.mutation() as cur:
            state.pop_task_steer(cur, task_id, message)

    def on_agent_message(message: str) -> None:
        state.record_agent_event("task.message", task_id, {"message": message, "source": "agent"})

    # Everything from here is inside one try: the task was claimed (marked
    # RUNNING) above, so ANY exception — including a failure to create or
    # start the server — must fail the task. An exception that escaped to
    # worker_loop instead would leave the task RUNNING forever with no worker
    # attached.
    server: Any = None
    try:
        # A non-active runtime fails the task here, loudly and immediately —
        # queued work never parks behind a missing login, and a cached
        # non-active status fails without any provider work (a refresh could
        # only delay the failure behind a slow status helper, or resurrect a
        # task the fail-fast contract says fails). The current network policy
        # is checked directly, so a task never starts against a provider
        # whose disable has not reached the cached status yet. A cached-active
        # Claude task still runs the refresh: memory-only in steady state,
        # and the repin convergence point when the CLI rotated the token
        # since the last poll, so routine rotation never fails a task at the
        # proxy. Codex needs no per-task convergence.
        if not _runtime_network_enabled(runtime_type):
            status = "deactivated"
        else:
            status = runtime_status(runtime_type)
            if status == "active" and runtime_type == "claude_code":
                status = refresh_runtime_status(runtime_type)
        if status != "active":
            label = RUNTIME_LABELS.get(runtime_type, runtime_type)
            raise RuntimeError(f"{label} runtime is {status}; tasks run only while it is active")
        # Register the (unstarted) process entry before start(), so a kill can
        # close the server mid-boot. The thread's RUNNING task keeps it
        # unavailable to other claims until this entry exists; a kill arriving
        # before it finds no entry, and the post-start status re-check below
        # abandons the turn instead.
        server = _new_agent_server(runtime_type, thread_id)
        # App instructions come from the host-validated manifest associated
        # with this app-scoped thread, never from task request content. Runtime
        # adapters deliver them at their developer/system instruction boundary.
        server.app_instructions = _app_instructions(thread_id)
        with _LIVE_LOCK:
            _LIVE[_live_key(runtime_type, thread_id)] = _Turn(server, runtime_type, thread_id, task_id)
        server.start()
        # If a kill cancelled this task while the server was starting, abandon
        # it rather than running a full turn the operator thinks was stopped.
        current = state.get_task(task_id)
        if current is None or current["status"] != RUNNING:
            return
        new_provider_session_id, output = provider.run_turn(
            server,
            input_message,
            provider_session_id,
            model,
            effort,
            steers,
            on_agent_message,
            steer_delivered,
        )
        _finish_task(
            task_id,
            COMPLETED,
            output=output,
            runtime_type=runtime_type,
            thread_id=thread_id,
            provider_session_id=new_provider_session_id,
        )
    except Exception as exc:
        _finish_task(task_id, FAILED, error_message=str(exc), runtime_type=runtime_type, thread_id=thread_id)
    finally:
        if server is not None:
            _close_turn(_live_key(runtime_type, thread_id), server)
        WORKER_WAKE.set()


def _claim_next_task() -> tuple[str, str, str, str, str, str, str | None] | None:
    """Atomically claim the first runnable queued task. Returns
    (task_id, runtime_type, thread_id, input_message, model, effort,
    provider session/thread id or None)."""
    with state.mutation() as cur:
        running = state.running_tasks(cur)
        if len(running) >= WORKER_COUNT:
            return None
        running_by_runtime: dict[str, int] = {}
        for task in running:
            runtime_type = task["agent_runtime"]
            running_by_runtime[runtime_type] = running_by_runtime.get(runtime_type, 0) + 1
        running_threads = {_live_key(task["agent_runtime"], task["thread_id"]) for task in running}
        # A thread is unavailable while a task on it is RUNNING (state) or
        # while it still has a live process entry (a finished task's worker
        # that has not closed its process yet — status goes terminal in
        # _finish_task BEFORE the finally closes — or a close still in
        # flight). Checking both here, under both locks, is what guarantees
        # at most one live process per user thread.
        with _LIVE_LOCK:
            if len(_LIVE) >= WORKER_COUNT:
                return None
            live_by_runtime: dict[str, int] = {}
            for turn in _LIVE.values():
                live_by_runtime[turn.runtime_type] = live_by_runtime.get(turn.runtime_type, 0) + 1
            unavailable = running_threads | set(_LIVE)
        # Queued tasks come back in claim order without their messages; only
        # the claimed task's input is fetched. Runtime status is not a claim
        # condition: a task claimed against a non-active runtime fails
        # immediately in the worker with that status as its error, instead of
        # parking queued work behind a login nobody may notice is missing.
        claimable = next(
            (
                t for t in state.queued_tasks_brief(cur)
                if running_by_runtime.get(t["agent_runtime"], 0) < WORKER_COUNT_PER_RUNTIME
                and live_by_runtime.get(t["agent_runtime"], 0) < WORKER_COUNT_PER_RUNTIME
                and _live_key(t["agent_runtime"], t["thread_id"]) not in unavailable
            ),
            None,
        )
        if claimable is None:
            return None
        claimed = state.get_task(claimable["task_id"], cur)
        if claimed is None or claimed["status"] != "queued":
            return None
        runtime_type = claimed["agent_runtime"]
        task_status.set_status(claimed, RUNNING, now=utc_now())
        state.save_task(cur, claimed)
        state.append_agent_event(cur, "task.started", claimed["task_id"], {})
        state.append_agent_event(
            cur,
            "task.message",
            claimed["task_id"],
            {"message": claimed["input_message"], "source": "user"},
        )
        session = state.thread_session(cur, runtime_type, claimed["thread_id"])
        provider_session_id = session["provider_session_id"] if session else None
        return (
            claimed["task_id"],
            runtime_type,
            claimed["thread_id"],
            claimed["input_message"],
            claimed["model"],
            claimed["effort"],
            provider_session_id,
        )


def close_task_server(task_id: str) -> None:
    """Kill the runtime process running ``task_id`` (the kill-task path). The
    worker blocked in run_turn surfaces the dead server as an error and finds
    the task already cancelled, so the cancellation sticks."""
    with _LIVE_LOCK:
        turn = next((t for t in _LIVE.values() if t.task_id == task_id), None)
    if turn is not None:
        _close_turn(_live_key(turn.runtime_type, turn.thread_id), turn.server)


def _close_turn(key: str, server: Any) -> None:
    """Close a turn's runtime process and free its thread. The kill path and
    the worker's finally can both get here for the same turn; exactly one wins
    ownership (``closing``) and the entry stays registered until its close
    completes, so no new turn can start on this thread while the old process
    may still be dying. The close itself runs outside _LIVE_LOCK: shutdown can
    take seconds and must not block claiming."""
    with _LIVE_LOCK:
        turn = _LIVE.get(key)
        if turn is None or turn.server is not server or turn.closing:
            return
        turn.closing = True
    try:
        server.close()
    except Exception:
        # The task has already been finished/failed, so operator flows can
        # continue. Keep the entry fenced: we cannot safely claim same-thread
        # work while the old process may live.
        return
    with _LIVE_LOCK:
        if _LIVE.get(key) is turn:
            del _LIVE[key]
    WORKER_WAKE.set()  # tasks queued on this thread are claimable again


def _finish_task(
    task_id: str,
    status: str,
    *,
    output: str | None = None,
    error_message: str | None = None,
    runtime_type: str,
    thread_id: str,
    provider_session_id: str | None = None,
) -> None:
    with state.mutation() as cur:
        task = state.get_task(task_id, cur)
        if task is None or task["status"] != RUNNING:
            return  # killed while the turn was in flight
        task_status.set_status(task, status, now=utc_now())
        if status == COMPLETED:
            task["output_message"] = output
            state.save_task(cur, task)
            state.save_thread_session(
                cur,
                runtime_type,
                thread_id,
                provider_session_id,
                utc_now(),
                task["model"],
                task["effort"],
            )
            state.append_agent_event(cur, "task.completed", task_id, {})
        else:
            task["error_message"] = error_message
            state.save_task(cur, task)
            state.append_agent_event(cur, "task.failed", task_id, {"error_message": error_message})


def _task_number(task_id: str) -> int:
    try:
        return int(str(task_id).rsplit("_", 1)[-1])
    except ValueError:
        return 0


def _provider_module(runtime_type: str | None = None) -> Any:
    if runtime_type == "claude_code":
        return claude_code
    if runtime_type == "pi":
        return pi_agent
    if runtime_type == "hermes":
        return hermes_agent
    return codex_app_server


def _app_for_thread(thread_id: str) -> tuple[app_platform.AppManifest, str] | None:
    """Installed app and app-visible id for an app-scoped host thread."""
    app_id, separator, visible_thread_id = thread_id.partition(app_platform.APP_SCOPED_ID_SEPARATOR)
    if not separator or not app_id or not visible_thread_id:
        return None
    app = app_platform.app_by_id(app_id)
    return (app, visible_thread_id) if app is not None else None


def _app_instructions(thread_id: str) -> str | None:
    app_thread = _app_for_thread(thread_id)
    return app_thread[0].agent_instructions if app_thread is not None else None


def _new_agent_server(runtime_type: str, thread_id: str) -> Any:
    # Every task turn runs inside a scope named after its host thread. App
    # threads carry the host-reserved `<app_id>__` prefix, so the agent-app
    # service derives ownership directly from kernel-owned cgroup state.
    # Turns on one thread are serialized, and --collect removes the scope
    # before the same unit name can be used by its next turn.
    if runtime_type == "claude_code":
        return claude_code.ClaudeCodeSession(thread_id=thread_id)
    if runtime_type == "pi":
        return pi_agent.PiSession(thread_id=thread_id)
    if runtime_type == "hermes":
        return hermes_agent.HermesSession(thread_id=thread_id)
    return codex_app_server.CodexAppServer(thread_id=thread_id)


def _live_key(runtime_type: str, thread_id: Any) -> str:
    return f"{runtime_type}:{thread_id}"


def _runtime_network_enabled(runtime_type: str) -> bool:
    provider = _MANAGED_PROVIDER_BY_RUNTIME.get(runtime_type)
    integrations = network_policy.load_policy().get("network_integrations", {})
    if not provider or not isinstance(integrations, dict):
        return False
    integration = integrations.get(provider)
    return isinstance(integration, dict) and integration.get("enabled") is True
