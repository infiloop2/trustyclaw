"""Agent runtime orchestration: the worker pool that runs queued tasks, the
runtime process cache, and the background poller that keeps the cached runtime
status fresh. The admin API delegates here; nothing in this module speaks HTTP.

Concurrency model: each runtime has its own ``WORKER_COUNT_PER_RUNTIME`` claim
cap, and up to ``WORKER_COUNT`` total tasks can run at once across runtimes.
A Codex server is dedicated to one user-supplied thread id and kept alive after
its turn, so a follow-up task on the same thread skips the app-server boot.
Claude Code turn processes normally exit after one turn and resume by session
id. Tasks on the same thread are serialized, while tasks on different threads
run in parallel.

How the synchronization fits together:

- Two locks. The mutation lock (private to state.py, entered through
  ``state.mutation()``) guards every admin-state write cycle; reads are
  lock-free queries. ``_POOL_LOCK`` guards ``_POOL`` and ``_CLOSING_THREADS``.
  The only place both are held is ``_claim_next_task``, and the nesting order
  there — the mutation lock, then _POOL_LOCK — is the one legal order; no code
  path acquires them the other way around, so they cannot deadlock. Slow work
  (spawning a runtime process, running a turn, closing a process) always
  happens with neither lock held. ``_REFRESH_LOCKS`` sits outside this pair:
  it serializes ``refresh_runtime_status`` per runtime, is deliberately held
  across the slow provider probe, and is never acquired while holding (or by)
  anything else.
- Provider account trust is anchored in the database, not in locks: the
  stored provider account row is the operator-approved anchor. It is written
  only inside the refresh commit mutation (first capture requires an
  unexpired operator OAuth login; afterwards the probed account id must
  match) and only an operator reset (``reset_linked_account``) clears it.
  Because the anchor comparison, the anchor save, and the reset clear all
  run under the mutation lock, a stale probe result can never re-approve an
  account a concurrent reset just cleared. Claude identity is additionally
  server-attested: whenever the probed token hash differs from the anchored
  one, the account uuid comes from api.anthropic.com for that token (via the
  root helper), so agent-writable metadata is never what gets anchored.
- ``WORKER_WAKE`` is advisory, never load-bearing. One ``Event`` is shared by
  all workers, so worker A can consume a wakeup meant for worker B; every wait
  therefore uses a 5s timeout and re-checks the queue from scratch.
  Correctness never depends on a wakeup arriving — wakeups only reduce
  latency.
- A user thread is *unavailable* for claiming while any of three things is
  true of it: a task on it is RUNNING in state, its pool slot is busy (its
  worker has not yet released — a task can already be terminal in state while
  its worker is still unwinding), or a previous server for it is still
  shutting down (``_CLOSING_THREADS``). All three are checked in one place,
  under both locks, in ``_claim_next_task``; together they guarantee at most
  one live process per user thread.
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
from host.runtime import (
    claude_code,
    codex_app_server,
    github_credential,
    github_repo_audit,
    proxy_state_client,
    state,
    task_status,
)
from host.runtime.state import (
    read_claude_account,
    read_openai_account,
    save_claude_account,
    save_openai_account,
    utc_now,
)
from host.runtime.task_status import COMPLETED, FAILED, RUNNING

WORKER_COUNT_PER_RUNTIME = 3
WORKER_COUNT = WORKER_COUNT_PER_RUNTIME * len(AGENT_RUNTIMES)
RUNTIME_RECHECK_SECONDS = 300  # re-verify an active agent login this often (it can expire)
RUNTIME_PENDING_RECHECK_SECONDS = 5  # poll more often while loading / awaiting login
WORKER_WAKE = threading.Event()
_MANAGED_PROVIDER_BY_RUNTIME = {"codex": "openai", "claude_code": "claude"}
CLAUDE_IDENTITY_ATTESTATION = "anthropic_oauth_profile"
OPENAI_OPERATOR_APPROVAL = "codex_device_login"


@dataclass
class _Slot:
    """One pooled runtime process, bound to a user thread id for its lifetime."""

    server: Any
    runtime_type: str
    thread_id: str
    busy: bool
    last_used: float
    task_id: str | None  # the running task while busy, for the kill path


# runtime/thread key -> slot. Slot count never exceeds WORKER_COUNT for long:
# busy slots are bounded by the running-task claim and idle slots are evicted
# on demand.
_POOL: dict[str, _Slot] = {}
_POOL_LOCK = threading.Lock()
# user thread id -> closes in flight. A thread with a server still shutting
# down must not start a new turn: the dying process may still be flushing the
# runtime conversation, and a resume that races it can fail and silently fork
# the conversation onto a fresh provider session. The claim rule
# treats these threads as busy until the close completes.
_CLOSING_THREADS: dict[str, int] = {}
# Cached per-runtime status, in process memory on purpose: it is derived
# health, re-computed from the provider CLIs within seconds of startup, so
# persisting it would only serve stale answers across restarts (a fresh
# process reports "loading" until the first poll). Writers replace whole
# records under _RUNTIME_STATUS_LOCK and never hold it around database work;
# readers take the current record lock-free (records are never mutated in
# place), so no path holds this lock while entering state.mutation() and the
# lock graph stays acyclic.
_RUNTIME_STATUSES: dict[str, dict[str, str]] = {}
_RUNTIME_STATUS_LOCK = threading.Lock()
# One in-flight refresh per runtime. Concurrent refreshes would duplicate slow
# provider probes, and their post-mutation pin writes could interleave so a
# stale clear lands on top of a fresher pin (or the reverse). Held around the
# whole probe-and-commit cycle; nothing acquires it while holding another lock.
_REFRESH_LOCKS: dict[str, threading.Lock] = {runtime_type: threading.Lock() for runtime_type in AGENT_RUNTIMES}
# Latest successful Claude token attestation, keyed by token hash (single
# entry). Purely a network-call dedupe for the 5s error-recheck loop; the
# durable dedupe is the anchor row itself. Only the claude_code refresh
# touches it, and refreshes are serialized per runtime.
_CLAUDE_ATTESTATIONS: dict[str, dict[str, Any]] = {}
# Serializes the direct Claude profile helper with the operator account reset.
# A reset that clears the Claude anchor while a refresh is between approval
# check and helper egress must make the refresh re-check approval before root
# egress.
_CLAUDE_ATTESTATION_RESET_LOCK = threading.Lock()


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
    return _RUNTIME_STATUSES.get(runtime_type, {"status": "loading"})


def all_runtime_status_records() -> dict[str, dict[str, str]]:
    records = dict(_RUNTIME_STATUSES)
    for runtime_type in AGENT_RUNTIMES:
        records.setdefault(runtime_type, {"status": "loading"})
    return records


def _set_runtime_status(runtime_type: str, status: str, error_message: str | None = None) -> str:
    """Replace the runtime's status record; returns the previous status so
    callers can emit transition events."""
    record = {"status": status}
    if error_message is not None:
        record["error_message"] = error_message
    with _RUNTIME_STATUS_LOCK:
        previous = _RUNTIME_STATUSES.get(runtime_type, {"status": "loading"})["status"]
        _RUNTIME_STATUSES[runtime_type] = record
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


def refresh_runtime_status(runtime_type: str) -> str:
    """Re-derive the agent runtime status and cache it in memory. Runs the
    provider check outside the state transaction so a slow runtime process
    never blocks requests. Serialized per runtime by _REFRESH_LOCKS."""
    with _REFRESH_LOCKS[runtime_type]:
        return _refresh_runtime_status_serialized(runtime_type)


def _refresh_runtime_status_serialized(runtime_type: str) -> str:
    if not _runtime_network_enabled(runtime_type):
        return _mark_runtime_deactivated(runtime_type)
    provider = _provider_module(runtime_type)
    _seed_runtime_proxy_pin_for_status_check(runtime_type)
    if not _runtime_network_enabled(runtime_type):
        # The seed writes a proxy-readable pin, so re-check the policy after it
        # (same pattern as the post-status pin write below): a disable that
        # landed while seeding must clear the pin, not leave it standing.
        return _mark_runtime_deactivated(runtime_type)
    try:
        status, error_message, account = provider.account_status()
    except Exception as exc:
        status, error_message, account = "error", f"unexpected error checking {runtime_type}: {exc!r}", None
    # The status poll is the sole reader of the parked login server, so it has now
    # recorded any completed-login notification. Capture the first trusted anchor
    # here, in the same refresh, so the proxy pin lands the moment the login lands
    # instead of a poll cycle later. The network re-check below then clears the pin
    # if a disable raced this write, exactly as it does after the pre-status seed.
    _capture_completed_codex_login(runtime_type)
    if not _runtime_network_enabled(runtime_type):
        # A policy replacement may have disabled this runtime while the provider
        # CLI check was running. Do not let that stale result overwrite the
        # deactivated state or make OAuth login available under a denied policy.
        return _mark_runtime_deactivated(runtime_type)
    account_value = _active_account_value(runtime_type, status, account)
    attested: dict[str, Any] | None = None
    attest_error: str | None = None
    if runtime_type == "claude_code" and status == "active" and account_value:
        attested, attest_error = _claude_attestation(account_value)
    deactivated = False
    codex_login_to_close: str | None = None
    with state.mutation() as cur:
        if not _runtime_network_enabled(runtime_type):
            # The final provider-policy check is inside the mutation so a stale
            # slow probe cannot race a disable and commit active/login state
            # after the deactivation write.
            _mark_runtime_deactivated_in(cur, runtime_type)
            deactivated = True
        else:
            if status == "active":
                # The anchor check and the anchor save share the status commit
                # mutation, so they serialize with an operator reset: a slow
                # probe that started before the reset sees the anchor already
                # cleared here and cannot re-approve the old account.
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
                state.set_oauth_login(cur, _oauth_key(runtime_type), None)
                usage_key = "claude_usage" if runtime_type == "claude_code" else "codex_usage"
                _stamp_usage_checked_at(account_value, usage_key, utc_now())
                if runtime_type == "claude_code":
                    save_claude_account(account_value, cur)
                else:
                    save_openai_account(account_value, cur)
            if previous == "awaiting_login" and status == "active":
                state.append_agent_event(cur, "agent_runtime.login_completed", None, {"agent_runtime": runtime_type})
            if previous != "active" and status == "active":
                state.append_agent_event(cur, "agent_runtime.active", None, {"agent_runtime": runtime_type})
    if deactivated:
        _finish_runtime_deactivation(runtime_type)
        return "deactivated"
    _sync_runtime_proxy_pin(runtime_type, account_value if status == "active" else None)
    if status == "active" and not _account_anchor_matches(runtime_type, account_value):
        # The pin table is written outside the mutation above. If an operator
        # reset landed in between, the anchor is gone and both the pin just
        # written and the just-committed "active" are stale: clear the pin and
        # reclassify as awaiting a fresh login. Same pattern as the policy
        # re-check below.
        _sync_runtime_proxy_pin(runtime_type, None)
        _set_runtime_status(runtime_type, "awaiting_login")
        return "awaiting_login"
    if not _runtime_network_enabled(runtime_type):
        # Account pins live in the proxy-readable pin table, outside the
        # admin mutation above. If a policy disable landed after the mutation but
        # before the pin write above, immediately clear the stale pin and
        # return to deactivated.
        return _mark_runtime_deactivated(runtime_type)
    if status == "active":
        # The login flow (first login or reauth) has landed, so its parked
        # device-login server is done. Close the one for this login id, scoped so
        # a login started meanwhile survives, or later status checks would keep
        # polling the leftover login process instead of short-lived servers.
        if codex_login_to_close:
            codex_app_server.close_completed_login_server(codex_login_to_close)
        WORKER_WAKE.set()  # tasks queued while logged out can start now
    return status


def _seed_runtime_proxy_pin_for_status_check(runtime_type: str) -> None:
    """Break the Codex pin/status circular dependency before the status check.

    account_status() makes guarded chatgpt.com requests (rate limits), which the
    proxy denies while proxy_provider_pins has no OpenAI account id. Seed the
    proxy from the operator-approved account anchor when one exists. The
    first-login capture that establishes that anchor runs after the status poll
    (see _capture_completed_codex_login), because the poller is the reader that
    surfaces the completed login from the parked device-code app-server.
    """
    if runtime_type != "codex":
        return
    account_id = _trusted_openai_account_id(read_openai_account())
    if not account_id:
        return
    if runtime_status(runtime_type) != "active" and _runtime_has_running_tasks(runtime_type):
        return
    proxy_state_client.sync_openai_account_id(account_id)
    if not _account_anchor_matches("codex", {"account_id": account_id}):
        proxy_state_client.sync_openai_account_id(None)


def _capture_completed_codex_login(runtime_type: str) -> None:
    """Persist the first trusted OpenAI anchor once the login has completed.

    Runs right after the status poll, which is the sole reader of the parked
    device-code app-server and has therefore recorded the successful
    account/login/completed notification. A stored OAuth row means the operator
    saw a device code, not that the login completed, so capture still requires
    that completion for the exact login id; the account id itself is read from
    the provider-signed login tokens promptly after completion (see
    read_completed_device_login_account_id).
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
    if account_id and _capture_completed_codex_oauth_login(login_id, account_id):
        # Seed the pin now that the anchor exists; the parked login server is
        # closed once the refresh commits "active" (see the active branch above),
        # which also covers reauth where this first-login capture is skipped.
        proxy_state_client.sync_openai_account_id(account_id)
        if not _account_anchor_matches("codex", {"account_id": account_id}):
            proxy_state_client.sync_openai_account_id(None)


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


def _runtime_has_running_tasks(runtime_type: str) -> bool:
    return any(task["agent_runtime"] == runtime_type for task in state.running_tasks())


def _active_account_value(runtime_type: str, status: str, account: Any) -> dict[str, Any] | None:
    if status != "active":
        return None
    if isinstance(account, dict):
        return account
    if runtime_type == "codex" and isinstance(account, str) and account:
        return {"account_id": account}
    return None


def _claude_attestation(account: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Attest the probed Claude token when its hash is not already anchored.

    The profile call is a network round trip, so it runs out here only when the
    token still needs attestation: a token recorded on a server-attested anchor
    was attested when it was first seen, and steady-state refreshes stay local.
    Successful attestations are cached by token hash so a runtime stuck in
    error (account mismatch) does not re-ask the profile endpoint on every 5s
    recheck; failures are never cached. Returns (attested identity, None) or
    (None, error message)."""
    token_hash = _string_field(account, "access_token_sha256")
    if not token_hash:
        return None, None  # the trust check rejects the account outright
    if not _claude_attestation_allowed(token_hash):
        return None, None
    cached = _CLAUDE_ATTESTATIONS.get(token_hash)
    if cached is not None:
        return cached, None
    # Serialize with operator account reset, then re-check policy and operator
    # approval immediately before the root helper opens direct host egress.
    with _CLAUDE_ATTESTATION_RESET_LOCK:
        if not _runtime_network_enabled("claude_code") or not _claude_attestation_allowed(token_hash):
            return None, None
        cached = _CLAUDE_ATTESTATIONS.get(token_hash)
        if cached is not None:
            return cached, None
        try:
            attested = claude_code.read_attested_identity(expected_token_sha256=token_hash)
        except claude_code.ClaudeCodeError as exc:
            return None, str(exc)
        attested_hash = _string_field(attested, "access_token_sha256")
        if attested_hash:
            _CLAUDE_ATTESTATIONS.clear()
            _CLAUDE_ATTESTATIONS[attested_hash] = attested
        return attested, None


def _claude_attestation_allowed(token_hash: str) -> bool:
    stored = read_claude_account()
    trusted_account_id = _trusted_claude_account_id(stored)
    if trusted_account_id:
        # A steady-state token still on the anchor needs no re-attestation; a
        # rotated token does. A row without a server-attested anchor (a
        # pre-attestation upgrade) is not trusted, so it re-captures through
        # the operator-login gate below, exactly like a fresh box.
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
    # Only a server-attested row is a trusted anchor. A pre-attestation upgrade
    # row (account id set by old agent-writable metadata, no attestation) is
    # treated as no anchor, so a plain operator re-login re-captures it through
    # the first-capture gate below, the same way an unapproved OpenAI row is
    # ignored. No separate reset is required.
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
    server attestation, so a row without it (a pre-attestation upgrade) is not
    an anchor and re-captures through a fresh operator login."""
    if not _claude_anchor_is_server_attested(account):
        return None
    return _string_field(account, "account_id")


def _account_anchor_matches(runtime_type: str, account: dict[str, Any] | None) -> bool:
    stored = read_claude_account() if runtime_type == "claude_code" else read_openai_account()
    if runtime_type == "claude_code":
        trusted_account_id = _trusted_claude_account_id(stored)
    else:
        trusted_account_id = _trusted_openai_account_id(stored)
    account_id = _string_field(account, "account_id") if account else None
    return trusted_account_id is not None and trusted_account_id == account_id


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


def _sync_runtime_proxy_pin(runtime_type: str, account: dict[str, Any] | None) -> None:
    if runtime_type == "claude_code":
        proxy_state_client.sync_claude_account(account)
        return
    account_id = _string_field(account, "account_id") if account else None
    proxy_state_client.sync_openai_account_id(account_id)


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
    _finish_runtime_deactivation(runtime_type)
    return "deactivated"


def _mark_runtime_deactivated_in(cur: Any, runtime_type: str) -> None:
    previous = _set_runtime_status(runtime_type, "deactivated")
    state.set_oauth_login(cur, _oauth_key(runtime_type), None)
    if previous != "deactivated":
        state.append_agent_event(cur, "agent_runtime.deactivated", None, {"agent_runtime": runtime_type})


def _oauth_key(runtime_type: str) -> str:
    return "claude" if runtime_type == "claude_code" else "codex"


def _finish_runtime_deactivation(runtime_type: str) -> None:
    _sync_runtime_proxy_pin(runtime_type, None)
    deactivate_runtime(runtime_type, "agent runtime deactivated because its managed network provider is disabled")


def runtime_network_enabled(runtime_type: str) -> bool:
    return _runtime_network_enabled(runtime_type)


def reconcile_runtime_status_after_policy_change() -> None:
    """Synchronize cached runtime state after a policy update.

    Disabled runtimes are deactivated synchronously because that fails running
    tasks and closes their processes; deactivation never probes, so it skips
    the per-runtime refresh serialization rather than wait out an in-flight
    slow probe (which re-checks the policy inside its own commit anyway).
    Enabled runtimes are refreshed in the background: a policy change may have
    re-enabled a runtime whose poller still has a stale long active-runtime
    deadline, but the network-policy request path must not block on provider
    CLI checks.
    """
    enabled: list[str] = []
    for runtime_type in sorted(AGENT_RUNTIMES):
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
    tasks. Queued tasks are left queued; if the runtime becomes active again,
    the normal claim path can run them."""
    _stop_runtime_processes(runtime_type, reason)


def reset_linked_account(runtime_type: str) -> None:
    """Operator reset: delete the linked-account guard and stop old sessions.

    One mutation clears the trusted account anchor and any pending OAuth
    approval; the proxy pin clear and a best-effort close of a parked login
    flow follow. Live runtime processes are closed and running tasks are failed
    so no process from the old linked account keeps executing while the caller
    clears local auth files and refreshes status."""
    # Snapshot the parked login server before clearing state opens the awaiting_login
    # window: a new device login can be parked before the close below runs, and we
    # must close only the server this reset saw, never the freshly started one.
    login_server = codex_app_server.current_login_server() if runtime_type == "codex" else None
    if runtime_type == "claude_code":
        with _CLAUDE_ATTESTATION_RESET_LOCK:
            _reset_linked_account_in_state(runtime_type)
    else:
        _reset_linked_account_in_state(runtime_type)
    _sync_runtime_proxy_pin(runtime_type, None)
    _close_login_flow(runtime_type, login_server)
    _stop_runtime_processes(runtime_type, "linked provider account was reset by the operator")


def _reset_linked_account_in_state(runtime_type: str) -> None:
    with state.mutation() as cur:
        next_status = "awaiting_login" if _runtime_network_enabled(runtime_type) else "deactivated"
        _set_runtime_status(runtime_type, next_status)
        state.set_oauth_login(cur, _oauth_key(runtime_type), None)
        if runtime_type == "claude_code":
            save_claude_account(None, cur)
        else:
            save_openai_account(None, cur)
        state.append_agent_event(cur, "agent_runtime.linked_account_reset", None, {"agent_runtime": runtime_type})


def _close_login_flow(runtime_type: str, expected_login_server: Any = None) -> None:
    # Best-effort: the pending OAuth record is already gone, so a parked login
    # process that resists closing is inert; never fail the caller over it.
    # expected_login_server scopes the close to the server the reset snapshotted,
    # so a login started during the reset is not torn down.
    try:
        if runtime_type == "codex":
            codex_app_server.close_login_server_if_current(expected_login_server)
        else:
            claude_code.close_login_process()
    except Exception:
        pass


def _stop_runtime_processes(runtime_type: str, reason: str) -> None:
    with state.mutation() as cur:
        for task_id in state.fail_running_tasks(cur, reason, runtime=runtime_type):
            state.append_agent_event(cur, "task.failed", task_id, {"error_message": reason})

    closing: list[tuple[str, Any]] = []
    with _POOL_LOCK:
        for key, slot in list(_POOL.items()):
            if slot.runtime_type != runtime_type:
                continue
            del _POOL[key]
            _begin_close_locked(slot.runtime_type, slot.thread_id)
            closing.append((key, slot.server))
    for key, server in closing:
        _finish_close(key, server)
    WORKER_WAKE.set()


def runtime_status_loop() -> None:
    next_check_at = {runtime_type: 0.0 for runtime_type in sorted(AGENT_RUNTIMES)}
    while True:
        now = time.monotonic()
        try:
            for runtime_type in sorted(AGENT_RUNTIMES):
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
    task_id, runtime_type, thread_id, input_message, provider_session_id = claimed
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

    # Everything from acquire onward is inside one try: the task was claimed
    # (marked RUNNING) above, so ANY exception from here — including a failure
    # to acquire or boot the server — must fail the task. An exception that
    # escaped to worker_loop instead would leave the task RUNNING forever with
    # no worker attached.
    server: Any | None = None
    healthy = False
    try:
        if runtime_type == "claude_code":
            status = refresh_runtime_status(runtime_type)
            if status != "active":
                raise RuntimeError(f"Claude Code runtime is {status}")
        # The slot is registered (with this task id) before the possibly slow
        # start(), so a concurrent kill can close the server mid-boot.
        # Otherwise a kill in the start() window finds no server, closes
        # nothing, and the turn runs on after the operator believes it was
        # stopped.
        server, needs_start = _acquire_server(runtime_type, thread_id, task_id)
        assert server is not None
        if needs_start:
            server.start()
        # If a kill cancelled this task while the server was starting, abandon
        # it rather than running a full turn the operator thinks was stopped.
        current = state.get_task(task_id)
        if current is None or current["status"] != RUNNING:
            return
        new_provider_session_id, output = provider.run_turn(
            server, input_message, provider_session_id, steers, on_agent_message, steer_delivered
        )
        healthy = True
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
            _release_server(runtime_type, thread_id, server, healthy)
        WORKER_WAKE.set()


def _claim_next_task() -> tuple[str, str, str, str, str | None] | None:
    """Atomically claim the first runnable queued task. Returns
    (task_id, runtime_type, thread_id, input_message, provider session/thread id or None)."""
    with state.mutation() as cur:
        running = state.running_tasks(cur)
        if len(running) >= WORKER_COUNT:
            return None
        running_by_runtime: dict[str, int] = {}
        for task in running:
            runtime_type = task["agent_runtime"]
            running_by_runtime[runtime_type] = running_by_runtime.get(runtime_type, 0) + 1
        statuses = all_runtime_status_records()
        active_runtimes = {
            runtime_type
            for runtime_type, record in statuses.items()
            if record.get("status") == "active" and _runtime_network_enabled(runtime_type)
        }
        running_threads = {_pool_key(task["agent_runtime"], task["thread_id"]) for task in running}
        # A thread is unavailable while: a task on it is RUNNING (state), its
        # slot is busy (a finished task's worker that has not released yet —
        # status goes terminal in _finish_task BEFORE the finally releases the
        # slot), or its previous server is still shutting down. Checking all
        # three here, under both locks, is what guarantees at most one live
        # process per user thread.
        with _POOL_LOCK:
            busy_slots = set()
            busy_by_runtime: dict[str, int] = {}
            for slot in _POOL.values():
                if not slot.busy:
                    continue
                busy_slots.add(_pool_key(slot.runtime_type, slot.thread_id))
                busy_by_runtime[slot.runtime_type] = busy_by_runtime.get(slot.runtime_type, 0) + 1
            if len(busy_slots) >= WORKER_COUNT:
                return None
            unavailable = running_threads | busy_slots | set(_CLOSING_THREADS)
        # Queued tasks come back in claim order without their messages; only
        # the claimed task's input is fetched.
        claimable = next(
            (
                t for t in state.queued_tasks_brief(cur)
                if t["agent_runtime"] in active_runtimes
                and running_by_runtime.get(t["agent_runtime"], 0) < WORKER_COUNT_PER_RUNTIME
                and busy_by_runtime.get(t["agent_runtime"], 0) < WORKER_COUNT_PER_RUNTIME
                and _pool_key(t["agent_runtime"], t["thread_id"]) not in unavailable
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
        return claimed["task_id"], runtime_type, claimed["thread_id"], claimed["input_message"], provider_session_id


def _acquire_server(runtime_type: str, thread_id: str, task_id: str) -> tuple[Any, bool]:
    """Return (server, needs_start): a warm server already bound to this
    thread, or a fresh one (evicting the least recently used idle slot when
    the pool is full). Closes evicted/dead servers outside the pool lock."""
    evicted: list[tuple[str, Any]] = []
    key = _pool_key(runtime_type, thread_id)
    with _POOL_LOCK:
        slot = _POOL.get(key)
        if slot is not None and not slot.busy and slot.server.alive():
            slot.busy = True
            slot.task_id = task_id
            return slot.server, False
        if slot is not None and not slot.busy:
            # The warm server died while idle; replace it.
            del _POOL[key]
            evicted.append((_pool_key(slot.runtime_type, slot.thread_id), slot.server))
            _begin_close_locked(slot.runtime_type, slot.thread_id)
        # A still-BUSY slot for this thread cannot happen here — the claim rule
        # refuses a thread whose slot is busy. If a future change breaks that
        # invariant, the fall-through below overwrites the slot and the stale
        # worker's release closes its own (now unpooled) server: degraded but
        # not corrupting.
        if len(_POOL) >= WORKER_COUNT:
            idle = [s for s in _POOL.values() if not s.busy]
            if idle:
                lru = min(idle, key=lambda s: s.last_used)
                del _POOL[_pool_key(lru.runtime_type, lru.thread_id)]
                evicted.append((_pool_key(lru.runtime_type, lru.thread_id), lru.server))
                _begin_close_locked(lru.runtime_type, lru.thread_id)
        server = _new_agent_server(runtime_type)
        _POOL[key] = _Slot(server, runtime_type, thread_id, True, time.monotonic(), task_id)
    for old_key, old_server in evicted:
        _finish_close(old_key, old_server)
    return server, True


def _release_server(runtime_type: str, thread_id: str, server: Any, healthy: bool) -> None:
    """Return a server to the warm pool after a turn, or close it: an unhealthy
    server (failed turn — it may be dead or wedged) must not poison the next
    task on this thread. A kill may already have removed the slot."""
    close = False
    key = _pool_key(runtime_type, thread_id)
    with _POOL_LOCK:
        slot = _POOL.get(key)
        if slot is None or slot.server is not server:
            close = True  # slot was killed or replaced; just make sure it dies
        elif healthy and server.alive():
            slot.busy = False
            slot.task_id = None
            slot.last_used = time.monotonic()
        else:
            del _POOL[key]
            close = True
        if close:
            _begin_close_locked(runtime_type, thread_id)
    if close:
        _finish_close(key, server)


def close_task_server(task_id: str) -> None:
    """Kill the runtime process running ``task_id`` (the kill-task path). The
    worker blocked in run_turn surfaces the dead server as an error and finds
    the task already cancelled, so the cancellation sticks."""
    with _POOL_LOCK:
        slot = next((s for s in _POOL.values() if s.task_id == task_id), None)
        if slot is not None:
            key = _pool_key(slot.runtime_type, slot.thread_id)
            del _POOL[key]
            _begin_close_locked(slot.runtime_type, slot.thread_id)
    if slot is not None:
        _finish_close(_pool_key(slot.runtime_type, slot.thread_id), slot.server)


def _begin_close_locked(runtime_type: str, thread_id: str) -> None:
    """Mark a thread's server as shutting down. Must run under _POOL_LOCK, in
    the same critical section that removed the slot — otherwise a claim could
    slip between removal and the mark and start a new turn on a runtime thread
    whose old process is still dying. The count handles concurrent closers
    (e.g. the kill path and the killed worker's release closing the same
    server)."""
    key = _pool_key(runtime_type, thread_id)
    _CLOSING_THREADS[key] = _CLOSING_THREADS.get(key, 0) + 1


def _finish_close(key: str, server: Any) -> None:
    """Close a server marked by _begin_close_locked and lift the mark. Runs
    outside _POOL_LOCK: shutdown can take seconds and must not block the pool."""
    try:
        server.close()
    except Exception:
        # The slot has been removed and any running task has been failed, so
        # reset/deactivation cleanup can continue. Keep _CLOSING_THREADS fenced:
        # we cannot safely claim same-thread work while the old process may live.
        return
    with _POOL_LOCK:
        remaining = _CLOSING_THREADS.get(key, 1) - 1
        if remaining <= 0:
            _CLOSING_THREADS.pop(key, None)
        else:
            _CLOSING_THREADS[key] = remaining
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
            state.save_thread_session(cur, runtime_type, thread_id, provider_session_id, utc_now())
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
    return claude_code if runtime_type == "claude_code" else codex_app_server


def _new_agent_server(runtime_type: str) -> Any:
    if runtime_type == "claude_code":
        return claude_code.ClaudeCodeSession()
    return codex_app_server.CodexAppServer()


def _pool_key(runtime_type: str, thread_id: Any) -> str:
    return f"{runtime_type}:{thread_id}"


def _runtime_network_enabled(runtime_type: str) -> bool:
    provider = _MANAGED_PROVIDER_BY_RUNTIME.get(runtime_type)
    policy = proxy_state_client.network_policy()
    integrations = policy.get("managed_network_integrations", {})
    if not provider or not isinstance(integrations, dict):
        return False
    integration = integrations.get(provider)
    return isinstance(integration, dict) and integration.get("enabled") is True
