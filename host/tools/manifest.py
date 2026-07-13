"""Tool manifest: the static, declarative description of a tool package.

The manifest is everything a host needs to know about a tool before running it:
which actions exist, their schemas and per-action data policy, what deployment
configuration the tool requires, and how the operator sets the tool up.

The host reads the manifest to build tool definitions for agents (e.g. MCP tool
listings or model tool configs), to gate enablement on required config, and to
show the operator each action's data policy before they enable it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Literal

from host.tools.json_types import JSONObject

# How the operator connects the tool.
# - "oauth": the tool owns an operator third-party OAuth flow (see
#   host.tools.tool.CredentialFlow); tokens live in host-provided credential
#   storage.
# - "enable_only": no operator credentials; the operator just enables/disables the
#   tool and it runs on deployment configuration (e.g. a service API key).
ConnectionKind = Literal["oauth", "enable_only"]

# Tool and action ids are used in credential/config partitions, approval
# records, audit records, and agent-facing tool names. Keep them stable and
# portable across those surfaces.
TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ACTION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class ActionSpec:
    """One callable action exposed by a tool.

    ``data_policy`` is per action (not per tool): it states, for this specific
    action, what data the action handles, what can leave the host, and whether
    it queues an approval before changing third-party state. The operator sees
    it next to the action before enabling the tool.

    ``output_schema`` describes the JSON ``ActionExecuted.result`` of a direct
    action and may be empty (``{}``) for approval-gated actions, which return a
    user-visible ``ApprovalExecuted`` message rather than a JSON result.
    """

    id: str
    description: str
    data_policy: str
    input_schema: JSONObject
    output_schema: JSONObject = field(default_factory=dict)


@dataclass(frozen=True)
class ConfigRequirement:
    """A deployment configuration value the tool needs the host to supply.

    These are deployment-level values such as an OAuth client id/secret or a
    third-party API key. They are always treated as secrets: the host stores
    them write-only and never returns their values. Tools read them only
    through ``HostAPI.config`` — never from environment variables or files.
    """

    key: str
    description: str


@dataclass(frozen=True)
class ToolManifest:
    """The complete static contract of a tool package."""

    # Stable identifier, e.g. "gmail" or "google_calendar".
    # Used in credential/config partitioning, approval records, host audit
    # records, and agent-facing tool naming. Must never change once released.
    tool_id: str
    display_name: str
    description: str
    actions: tuple[ActionSpec, ...]
    connection: ConnectionKind
    config: tuple[ConfigRequirement, ...] = ()
    # One operator-facing setup string: what the operator does on the provider
    # side (create an OAuth client, register the redirect URI, obtain an API
    # key) before configuring and connecting the tool. Empty when no external
    # setup is needed.
    setup_guide: str = ""

    def __post_init__(self) -> None:
        if not TOOL_ID_RE.fullmatch(self.tool_id):
            raise ValueError(
                "ToolManifest.tool_id must be 1-64 characters: lowercase ASCII letter first, "
                "then only lowercase ASCII letters, digits, or underscore."
            )
        seen_actions: set[str] = set()
        for spec in self.actions:
            if not ACTION_ID_RE.fullmatch(spec.id):
                raise ValueError(
                    "ActionSpec.id must be 1-128 characters containing only ASCII letters, "
                    "digits, dot, underscore, colon, or hyphen."
                )
            if spec.id in seen_actions:
                raise ValueError(f"Duplicate ActionSpec.id in {self.tool_id}: {spec.id}")
            if not spec.data_policy.strip():
                raise ValueError(f"ActionSpec.data_policy must be non-empty for {self.tool_id}:{spec.id}.")
            seen_actions.add(spec.id)

    def action(self, action_id: str) -> ActionSpec | None:
        for spec in self.actions:
            if spec.id == action_id:
                return spec
        return None
