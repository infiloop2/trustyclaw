"""Action results: how a tool action call resolves.

``Tool.execute`` returns an ``ActionResult``: the action either ran directly
(``ActionExecuted`` with a JSON result or ``StreamingAsset`` with one binary
file), was queued for a user decision (``ActionPendingApproval``), or failed
(``ActionFailed``). JSON and streamed bytes are mutually exclusive result
kinds.

``Tool.execute_approved`` returns an ``ApprovalResult``: a previously approved
action either ran (``ApprovalExecuted`` with a user-visible message) or failed
(``ActionFailed``). It never queues another approval.

Result payloads, messages, and error strings are agent- and user-visible: they
must already be redacted (no tokens, no secrets, no raw third-party error bodies).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Callable

from host.tools.json_types import JSONObject


@dataclass(frozen=True)
class ActionExecuted:
    """A direct action ran against the third-party service and produced a result.

    ``result`` is a JSON object validated against the action's ``output_schema``
    and shown to the agent.
    """

    result: JSONObject


@dataclass(frozen=True)
class OpenedStreamingAsset:
    """One opened binary result and its bounded response metadata."""

    filename: str
    media_type: str
    size_bytes: int
    source: BinaryIO


@dataclass(frozen=True)
class StreamingAsset:
    """A direct action whose entire result is one lazily opened binary file.

    The host enters ``open_stream`` before committing response headers and
    closes it after streaming. Packages must raise ``StreamingAssetError``
    with a redacted, agent-safe message for expected open or transfer failures.
    """

    open_stream: Callable[[], AbstractContextManager[OpenedStreamingAsset]]


class StreamingAssetError(RuntimeError):
    """A redacted streaming failure safe to return to the agent."""


@dataclass(frozen=True)
class ActionPendingApproval:
    """The action is sensitive and was queued for a user decision.

    The tool requested a host-owned approval workflow record via
    ``HostAPI.approvals`` and returns the tool-agnostic approval id so the
    caller (agent or gateway) can report the pending state. The host calls
    ``Tool.execute_approved(approval_id, api)`` if the user approves; a denial
    is terminal and handled by the host.
    """

    approval_id: str
    # Short human-readable summary of what was queued, safe to display.
    summary: str = ""


@dataclass(frozen=True)
class ApprovalExecuted:
    """A previously approved action ran.

    The outcome of an approved action is only ever shown to the user, so it is a
    single user-visible ``message`` string (for example ``"Sent your Gmail
    message to a@b.com."``) rather than a JSON result. The message must be
    redacted: no tokens, secrets, or raw third-party payloads.
    """

    message: str


@dataclass(frozen=True)
class ActionFailed:
    """The action could not run. ``error`` is safe to show to users/agents."""

    error: str
    # Set when the failure is fixable by the user reconnecting the tool
    # (expired/revoked credentials, missing scopes).
    reconnect_required: bool = False


# Returned by Tool.execute.
ActionResult = ActionExecuted | StreamingAsset | ActionPendingApproval | ActionFailed
# Returned by Tool.execute_approved.
ApprovalResult = ApprovalExecuted | ActionFailed
