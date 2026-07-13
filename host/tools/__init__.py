"""Open tool framework for TrustyClaw.

UI-free tool packages implement the ``Tool`` contract against the
host-provided ``HostAPI``. Hosts import this package, provide the host API,
and load whichever tools they enable.
"""

from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import (
    ActionSpec,
    ConfigRequirement,
    ConnectionKind,
    ToolManifest,
)
from host.tools.results import (
    ActionExecuted,
    ActionFailed,
    ActionPendingApproval,
    ActionResult,
    ApprovalExecuted,
    ApprovalResult,
)
from host.tools.tool import (
    ConnectionStatus,
    CredentialFlow,
    OAuthCompleteConnectParams,
    OAuthCompleteConnectResult,
    OAuthStartConnectParams,
    OAuthStartConnectResult,
    Tool,
)
from host.tools.host_api import (
    ApprovalRecord,
    Approvals,
    ApprovalStatus,
    ConnectionAccount,
    Credentials,
    HostAPI,
    StoredCredential,
)

__all__ = [
    "ActionExecuted",
    "ActionFailed",
    "ActionPendingApproval",
    "ActionResult",
    "ActionSpec",
    "ApprovalExecuted",
    "ApprovalRecord",
    "ApprovalResult",
    "Approvals",
    "ApprovalStatus",
    "ConfigRequirement",
    "ConnectionAccount",
    "ConnectionKind",
    "ConnectionStatus",
    "CredentialFlow",
    "Credentials",
    "HostAPI",
    "JSONObject",
    "JSONValue",
    "OAuthCompleteConnectParams",
    "OAuthCompleteConnectResult",
    "OAuthStartConnectParams",
    "OAuthStartConnectResult",
    "StoredCredential",
    "Tool",
    "ToolManifest",
]
