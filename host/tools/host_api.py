"""The host API: everything a tool package gets from its host.

A tool package contains pure tool logic. Everything deployment-specific —
where credentials live, how they are encrypted, and how approvals are decided —
is provided by the host behind these interfaces. Tool code receives a
``HostAPI`` on every call and must use it exclusively:

- no direct environment/secret reads (use ``config``),
- no direct database or file access for credentials (use ``credentials``),
- no tool-owned approval bookkeeping (use ``approvals``).

Every ``HostAPI`` instance is already scoped to one tool on one local
TrustyClaw host. Credentials and approval records are implicitly partitioned by
tool; a tool can never see another tool's data.

See docs/architecture/tools/tool-contract.md for the full specification and the
rules of the boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Literal, Protocol, TypedDict

from host.tools.json_types import JSONObject

# Single-use lifecycle: pending -> denied | expired (terminal, host-owned), or
# pending -> approved -> executed | failed (execute_approved ran exactly once).
ApprovalStatus = Literal["pending", "approved", "denied", "expired", "executed", "failed"]


class ConnectionAccount(TypedDict):
    """The connected third-party account for an OAuth tool.

    This is the explicit, non-secret account shape every OAuth tool returns and
    the host stores/displays. ``id`` is the stable provider account identifier
    (for example a Google ``sub``) used to bind approvals to the account that
    was connected when they were proposed; ``label`` is the human-readable
    account (an email); ``scopes`` are the granted OAuth scopes.
    """

    id: str
    label: str
    scopes: list[str]


class StoredCredential(TypedDict):
    """One tool's persisted OAuth credential.

    ``account`` is the non-secret connected-account metadata (safe to surface in
    the UI). ``secret`` is the provider token material (access/refresh tokens,
    expiry) — opaque to the host and encrypted at rest. ``metadata`` is any
    remaining non-secret tool bookkeeping (verification flags, timestamps).
    """

    account: ConnectionAccount
    secret: JSONObject
    metadata: JSONObject


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    action_id: str
    status: ApprovalStatus
    # The exact payload to execute if approved, as supplied to
    # Approvals.request. Stored by the host (encrypted at rest in hosted
    # deployments) and handed back to Tool.execute_approved verbatim.
    payload: JSONObject
    # Redacted, user-displayable summary of the proposed action.
    summary: str
    created_at: int
    decided_at: int = 0


class Credentials(Protocol):
    """The tool's OAuth credential store — the only place tool state lives.

    OAuth tools are the only tools that persist state, and all they persist is a
    single connected-account credential. Rather than a generic key/value store,
    the host exposes this purpose-built, typed service: one ``StoredCredential``
    per tool. The tool decides *what* the credential is; the host decides
    *where and how* it is stored (partitioning, encryption at rest). Enable-only
    tools never call this service.
    """

    def load(self) -> StoredCredential | None:
        """Return the stored credential for this tool, or ``None`` if absent."""
        ...

    def save(self, credential: StoredCredential) -> None:
        """Persist (replace) this tool's credential."""
        ...

    def clear(self) -> None:
        """Delete this tool's stored credential. A no-op if absent."""
        ...


class Approvals(Protocol):
    """Host-owned workflow records for sensitive tool actions.

    The tool requests a decision for one proposed action and returns
    ``ActionPendingApproval(approval_id, summary)``. The decision resolves
    asynchronously in the host's approval surface. On approval the host calls
    ``Tool.execute_approved(record, api)`` with the loaded, already-verified
    record. Tools never store approval payloads themselves.
    """

    def request(self, *, action_id: str, summary: str, payload: JSONObject) -> ApprovalRecord:
        """Create a pending approval and return the host-assigned approval id.

        ``action_id`` is the manifest action's id. ``summary`` must be redacted
        and displayable. ``payload`` is the exact JSON the tool will execute if
        the user approves.
        """
        ...


@dataclass(frozen=True)
class AssetMetadata:
    """Non-secret metadata for one tools-owned staged asset."""

    asset_id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    expires_at: int


class Assets(Protocol):
    """Tool-scoped access to bytes streamed into the tools service.

    The caller supplies only an opaque asset id. The host owns storage and
    returns an already-open binary stream, so tool packages never receive or
    open an agent-controlled pathname.
    """

    def describe(self, asset_id: str) -> AssetMetadata: ...

    def open(self, asset_id: str) -> AbstractContextManager[BinaryIO]: ...

    def delete(self, asset_id: str) -> None: ...

class Outbound(Protocol):
    """Host-owned guard for agent-controlled free-text request parameters.

    Tools pass each decoded free-text value bound for a public or third-party
    endpoint through this guard before request construction. It returns the
    value unchanged or raises ``ValueError`` with a descriptive, value-free
    message the tool surfaces verbatim so the agent can rephrase and retry.
    See docs/architecture/tools/outbound-request-filtering.md for which
    fields are guarded and why.
    """

    def guard_request_parameter_string(self, value: str, *, allow_identifiers: bool = False) -> str:
        """Guard an agent-controlled free-text request value. ``allow_identifiers=True``
        skips the personal-identifier rules for a query against an account the
        operator already connected (a mailbox search), where identifiers are
        legitimate search syntax; secret/credential shapes are still denied."""
        ...


class HostAPI(Protocol):
    """The bundle handed to every tool call, scoped to one tool."""

    @property
    def credentials(self) -> Credentials: ...

    @property
    def config(self) -> Mapping[str, str]: ...

    @property
    def approvals(self) -> Approvals: ...

    @property
    def assets(self) -> Assets: ...

    @property
    def outbound(self) -> Outbound: ...
