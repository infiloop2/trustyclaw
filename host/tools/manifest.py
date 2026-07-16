"""Tool manifest: the static, declarative description of a tool package.

The manifest is everything a host needs to know about a tool before running it:
which actions exist, their schemas and per-action data policy, what deployment
configuration the tool requires, and how the operator sets the tool up.

The host reads the manifest to build tool definitions for agents (e.g. MCP tool
listings or model tool configs), expose configuration state, and show the
operator each action's data policy and setup instructions.
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
ApprovalKind = Literal["direct", "operator"]

# Tool and action ids are used in credential/config partitions, approval
# records, audit records, and agent-facing tool names. Keep them stable and
# portable across those surfaces.
TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ACTION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
GUIDE_IMAGE_RE = re.compile(r"^/guide-assets/[a-z0-9][a-z0-9._-]*\.png$")


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
    approval: ApprovalKind = "direct"


@dataclass(frozen=True)
class SetupStep:
    """One operator step shown in a tool's Integration Guides entry.

    ``show_callback`` renders this host's OAuth callback URI inside the step;
    ``show_config`` renders the tool's configuration keys inside the step. Set
    them on the step where the operator actually needs those values.
    """

    title: str
    description: str
    link_url: str = ""
    link_label: str = ""
    image_path: str = ""
    image_alt: str = ""
    show_callback: bool = False
    show_config: bool = False


@dataclass(frozen=True)
class DataSummaryPoint:
    """One short labeled fact inside a guide data card."""

    label: str
    text: str


@dataclass(frozen=True)
class DataSummaryLink:
    """An authoritative policy link attached to the fact it supports."""

    label: str
    url: str


@dataclass(frozen=True)
class DataSummaryCard:
    """One card in an Integration Guide's data section."""

    title: str
    description: str = ""
    points: tuple[DataSummaryPoint, ...] = ()
    links: tuple[DataSummaryLink, ...] = ()


@dataclass(frozen=True)
class DataSummary:
    """The concise user-facing account of data crossing a tool boundary.

    Always four cards, in this order: what leaves this host, where it can
    go, what the third party can do with it, and how long it retains it.
    """

    cards: tuple[DataSummaryCard, DataSummaryCard, DataSummaryCard, DataSummaryCard]


@dataclass(frozen=True)
class ConfigRequirement:
    """A deployment configuration value the tool needs the host to supply.

    These are deployment-level values such as an OAuth client id/secret or a
    third-party API key. They are always treated as secrets: the host stores
    them write-only and never returns their values. Tools read them only
    through ``HostAPI.config``, never from environment variables or files.
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
    data_summary: DataSummary
    config: tuple[ConfigRequirement, ...] = ()
    # Short, concrete safeguards for the summary popover and full guide.
    protections: tuple[str, ...] = ()
    # Implementation details shown only in Integration Guides. Keep summary
    # popovers operator-facing; put protocol and payload mechanics here.
    technical_details: tuple[str, ...] = ()
    # Ordered provider-side and TrustyClaw setup. Empty when enablement is the
    # only step.
    setup_steps: tuple[SetupStep, ...] = ()

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
            if spec.approval not in ("direct", "operator"):
                raise ValueError(f"ActionSpec.approval must be direct or operator for {self.tool_id}:{spec.id}.")
            seen_actions.add(spec.id)
        for index, protection in enumerate(self.protections):
            if not protection.strip():
                raise ValueError(f"ToolManifest.protections[{index}] must be non-empty for {self.tool_id}.")
        for index, detail in enumerate(self.technical_details):
            if not detail.strip():
                raise ValueError(f"ToolManifest.technical_details[{index}] must be non-empty for {self.tool_id}.")
        for index, step in enumerate(self.setup_steps):
            if not step.title.strip() or not step.description.strip():
                raise ValueError(f"ToolManifest.setup_steps[{index}] must have a title and description for {self.tool_id}.")
            if bool(step.link_url) != bool(step.link_label):
                raise ValueError(f"ToolManifest.setup_steps[{index}] link_url and link_label must be set together for {self.tool_id}.")
            if step.link_url and not step.link_url.startswith("https://"):
                raise ValueError(f"ToolManifest.setup_steps[{index}] link_url must use HTTPS for {self.tool_id}.")
            if bool(step.image_path) != bool(step.image_alt):
                raise ValueError(f"ToolManifest.setup_steps[{index}] image_path and image_alt must be set together for {self.tool_id}.")
            if step.image_path and not GUIDE_IMAGE_RE.fullmatch(step.image_path):
                raise ValueError(f"ToolManifest.setup_steps[{index}] image_path must name a local PNG guide asset for {self.tool_id}.")
        if len(self.data_summary.cards) != 4:
            raise ValueError(f"ToolManifest.data_summary must have exactly four cards for {self.tool_id}.")
        for card_index, card in enumerate(self.data_summary.cards):
            if not card.title.strip() or not (card.description.strip() or card.points):
                raise ValueError(f"ToolManifest.data_summary.cards[{card_index}] must have a title and content for {self.tool_id}.")
            for point_index, point in enumerate(card.points):
                if not point.label.strip() or not point.text.strip():
                    raise ValueError(f"ToolManifest.data_summary.cards[{card_index}].points[{point_index}] must be non-empty for {self.tool_id}.")
            for link_index, link in enumerate(card.links):
                if not link.label.strip() or not link.url.startswith("https://"):
                    raise ValueError(f"ToolManifest.data_summary.cards[{card_index}].links[{link_index}] must have a label and HTTPS URL for {self.tool_id}.")

    def action(self, action_id: str) -> ActionSpec | None:
        for spec in self.actions:
            if spec.id == action_id:
                return spec
        return None
