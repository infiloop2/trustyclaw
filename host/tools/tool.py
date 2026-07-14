"""The Tool contract: what a tool package implements.

A tool package is pure tool logic with no UI and no storage of its own.
Everything tool-specific — actions, schemas, third-party API calls, and third-party
auth — lives in the package; everything deployment-specific lives behind the
``HostAPI`` the host provides.

See docs/architecture/tools/tool-contract.md for the full specification.
"""

from __future__ import annotations

from typing import Protocol, TypedDict

from host.tools.host_api import ApprovalRecord, ConnectionAccount, HostAPI
from host.tools.json_types import JSONObject
from host.tools.manifest import ToolManifest
from host.tools.results import ActionResult, ApprovalResult


class OAuthStartConnectParams(TypedDict):
    """Host-supplied inputs to begin an OAuth authorization-code connect.

    ``redirect_uri`` is the callback URL the host will handle after the provider
    redirects the operator back. These fields are host-fixed and tool-agnostic:
    every OAuth tool receives exactly this shape, so a tool never declares its
    own connect-parameter schema.
    """

    redirect_uri: str


class OAuthStartConnectResult(TypedDict):
    """What a tool returns to start a connect.

    ``authorization_url`` is the provider URL the host redirects the operator
    to. ``state`` is the tool's opaque CSRF/anti-forgery value: the tool mints
    it in ``start_connect`` and re-verifies it in ``complete_connect`` (the host
    round-trips it through the provider callback unchanged and never
    interprets it).
    """

    authorization_url: str
    state: str


class OAuthCompleteConnectParams(TypedDict):
    """The provider callback values the host hands back to finish a connect.

    Host-fixed and tool-agnostic like ``OAuthStartConnectParams``: the standard
    authorization-code fields, validated by the host before the tool sees them.
    """

    code: str
    state: str
    redirect_uri: str


class OAuthCompleteConnectResult(TypedDict):
    """What a tool returns after a successful connect: the connected account."""

    account: ConnectionAccount


class ConnectionStatus(TypedDict, total=False):
    """Non-secret current connection state for the host UI.

    ``connected`` is always present. ``account`` is present only when
    ``connected`` is ``True``.
    """

    connected: bool
    account: ConnectionAccount


class CredentialFlow(Protocol):
    """Tool-owned operator third-party auth (manifest ``connection == "oauth"``).

    Third-party auth is tool-owned because it is provider-specific: scope
    selection, authorization-URL construction, ``state`` verification, token
    exchange, refresh, revocation, and account lookup all live in the tool. The
    host contributes only the tool-agnostic connect boundary — the fixed
    ``OAuth*`` param/result shapes above and the callback plumbing — so new
    OAuth tools reuse it without inventing their own connect API. Tokens persist
    only through ``api.credentials``.
    """

    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult:
        """Begin the connect flow: return the provider authorization URL and the
        opaque ``state`` the host round-trips back to ``complete_connect``."""
        ...

    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        """Finish the connect flow with the provider callback params. Verifies
        ``state``, exchanges ``code`` for tokens, persists them via
        ``api.credentials``, and returns the connected ``account``."""
        ...

    def disconnect(self, api: HostAPI) -> None:
        """Revoke third-party tokens where possible and clear stored credentials."""
        ...

    def connection_status(self, api: HostAPI) -> ConnectionStatus:
        """Non-secret current connection state for the host UI."""
        ...


class Tool(Protocol):
    """A loadable tool package.

    Implementations must be stateless across calls: the only persistent state is
    an OAuth credential through ``api.credentials``, so the same package can be
    loaded by a local runtime or another host implementation interchangeably.
    """

    @property
    def manifest(self) -> ToolManifest: ...

    @property
    def credentials(self) -> CredentialFlow | None:
        """The tool's connect flow, or None when ``connection == "enable_only"``."""
        ...

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        """Run one manifest action for the calling user.

        ``tool_input`` has already been schema-validated by the host against
        the manifest's ``input_schema``; the tool still owns semantic
        validation (limits, formats, referenced-id checks) and returns a
        specific ``ActionFailed`` when it fails.

        Actions that gate on approval must not touch third-party state here:
        they validate, build the exact execution payload and a redacted summary,
        call ``api.approvals.request``, and return ``ActionPendingApproval``.
        """
        ...

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        """Execute a previously approved action.

        Called by the host after the user approves — at most once per
        approval (see the lifecycle in docs/architecture/tools/tool-contract.md).
        The host hands the loaded record: its status is guaranteed
        ``approved`` and it belongs to this tool. The tool re-verifies the
        payload's preconditions (the bound third-party account and any mutable
        objects it references), executes the stored payload exactly as
        proposed, and returns ``ApprovalExecuted`` (a user-visible message) or
        ``ActionFailed`` — never ``ActionPendingApproval``.
        """
        ...
