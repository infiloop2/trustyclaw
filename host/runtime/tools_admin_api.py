"""Operator-facing tools routes for the admin API.

The admin API owns the operator surface for bundled tools (list, config,
enable/disable, the OAuth connect flow, and approval decisions) but tool code and
third-party egress live in the dedicated ``trustyclaw-tools`` service. So the
routes here that need that service's egress or run tool code over third-party data
-- the whole OAuth connect flow and approval decisions -- are reverse-proxied to
the tools socket (peer-gated to the admin uid), the same way ``app_api_proxy``
reverse-proxies app-backend requests. The rest (list, config, enable/disable,
reading approvals) touch only stored state and run here.

``admin_api.route`` dispatches ``/v1/tools`` paths here; ``ApiError`` comes
from the shared ``admin_errors`` module, so there is no import back into
``admin_api``.
"""

from __future__ import annotations

import http.client
from http import HTTPStatus
import json
import os
import re
import socket
from typing import Any

from host.runtime import state, tools_api, tools_host
from host.runtime.admin_errors import ApiError

# Tool code and third-party egress live in the dedicated trustyclaw-tools
# service; the admin service forwards the operator operations that need that
# service's egress (OAuth code exchange, token revoke, approved-action
# execution) to its socket and holds no internet egress itself.
TOOLS_SOCKET_PATH = os.environ.get("TRUSTYCLAW_TOOLS_SOCKET", tools_api.DEFAULT_SOCKET_PATH)
# Approving an action runs execute_approved synchronously in the tools service,
# which can make several sequential bounded third-party calls (identity refresh,
# precondition re-verification, then the write), each up to a 30s provider
# timeout. Keep this proxy timeout above that worst case so a slow-but-successful
# approval is not reported to the operator as a failure while the tools service
# actually completes the side effect.
TOOLS_OPERATOR_TIMEOUT_SECONDS = 180


class _ToolsSocketConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost", timeout=TOOLS_OPERATOR_TIMEOUT_SECONDS)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


def _tools_operator_request(path: str, body: Any = None) -> Any:
    """Forward one operator operation to the trustyclaw-tools service and return
    its JSON, re-raising its HTTP status as an ApiError so the operator sees the
    same errors as before the split."""
    connection = _ToolsSocketConnection(TOOLS_SOCKET_PATH)
    try:
        payload = json.dumps(body).encode("utf-8") if body is not None else b"{}"
        connection.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        data = json.loads(raw) if raw else {}
        if response.status != 200:
            message = data.get("error") if isinstance(data, dict) else None
            raise ApiError(HTTPStatus(response.status), message or f"tools service error {response.status}")
        return data
    except (ConnectionError, FileNotFoundError, OSError) as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, "tools service unavailable") from exc
    finally:
        connection.close()


TOOL_APPROVAL_LIST_LIMIT = tools_host.PENDING_APPROVAL_LIMIT
TOOL_PATH_RE = re.compile(
    r"^/v1/tools/([a-z0-9_]{1,64})/"
    r"(enable|disable|oauth_connect/start|oauth_connect/complete|oauth_connect/disconnect)$"
)
# Approvals are addressed under their tool so the operator UI shows each tool's
# approvals in its own row rather than one unified list.
TOOL_APPROVALS_LIST_RE = re.compile(r"^/v1/tools/([a-z0-9_]{1,64})/approvals$")
TOOL_APPROVAL_GET_RE = re.compile(r"^/v1/tools/([a-z0-9_]{1,64})/approvals/([A-Za-z0-9._:-]{1,128})$")
TOOL_APPROVAL_DECIDE_RE = re.compile(r"^/v1/tools/([a-z0-9_]{1,64})/approvals/([A-Za-z0-9._:-]{1,128})/(approve|deny)$")
TOOL_CONFIG_PATH_RE = re.compile(r"^/v1/tools/([a-z0-9_]{1,64})/config$")


def tools_route(method: str, path: str, body: Any) -> Any:
    if path == "/v1/tools" and method == "GET":
        return list_tools()
    config_match = TOOL_CONFIG_PATH_RE.fullmatch(path)
    if config_match and method == "PUT":
        return put_tool_config(config_match.group(1), body)
    approvals_list = TOOL_APPROVALS_LIST_RE.fullmatch(path)
    if approvals_list and method == "GET":
        # Summary-only: an approval payload can be up to 64 KiB and this list is
        # polled while the row is open, so payloads are fetched on demand per
        # approval instead of transferred on every poll.
        approvals = [
            {key: value for key, value in approval.items() if key != "payload"}
            for approval in state.list_tool_approvals(TOOL_APPROVAL_LIST_LIMIT, tool_id=approvals_list.group(1))
        ]
        return {"approvals": approvals}
    approval_get = TOOL_APPROVAL_GET_RE.fullmatch(path)
    if approval_get and method == "GET":
        record = state.tool_approval(approval_get.group(2), tool_id=approval_get.group(1))
        if record is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "unknown approval")
        return {"approval": record}
    approval_decide = TOOL_APPROVAL_DECIDE_RE.fullmatch(path)
    if approval_decide and method == "POST":
        return decide_tool_approval(approval_decide.group(2), approval_decide.group(3), tool_id=approval_decide.group(1))
    tool_match = TOOL_PATH_RE.fullmatch(path)
    if tool_match and method == "POST":
        return tool_action_route(tool_match.group(1), tool_match.group(2), body)
    raise ApiError(HTTPStatus.NOT_FOUND, "unknown path")


def _bundled_tool(tool_id: str) -> Any:
    tool = tools_host.BUNDLED_TOOLS.get(tool_id)
    if tool is None:
        raise ApiError(HTTPStatus.NOT_FOUND, f"unknown tool: {tool_id}")
    return tool


def _tool_entry(tool: Any, enabled_ids: set[str], configured_keys: set[str]) -> dict[str, Any]:
    manifest = tool.manifest
    entry: dict[str, Any] = {
        "tool_id": manifest.tool_id,
        "display_name": manifest.display_name,
        "description": manifest.description,
        "connection": manifest.connection,
        "enabled": manifest.tool_id in enabled_ids,
        "actions": [
            {
                "id": spec.id,
                "description": spec.description,
                "data_policy": spec.data_policy,
                "approval": spec.approval,
                "input_schema": spec.input_schema,
                "output_schema": spec.output_schema,
            }
            for spec in manifest.actions
        ],
        # Config is per tool and all values are secrets: never leave the host,
        # the UI only sees which keys are set.
        "config": [
            {
                "key": requirement.key,
                "description": requirement.description,
                "set": requirement.key in configured_keys,
            }
            for requirement in manifest.config
        ],
        "protections": list(manifest.protections),
        "setup_steps": [
            {
                "title": step.title,
                "description": step.description,
                "link_url": step.link_url,
                "link_label": step.link_label,
                "image_path": step.image_path,
                "image_alt": step.image_alt,
                "show_callback": step.show_callback,
                "show_config": step.show_config,
            }
            for step in manifest.setup_steps
        ],
        "data_summary": {
            "cards": [
                {
                    "title": card.title,
                    "description": card.description,
                    "points": [{"label": point.label, "text": point.text} for point in card.points],
                    "links": [{"label": link.label, "url": link.url} for link in card.links],
                }
                for card in manifest.data_summary.cards
            ],
        },
    }
    if manifest.connection == "oauth":
        entry["connection_status"] = tool.credentials.connection_status(tools_host.host_api_for(tool))
    return entry


def list_tools() -> dict[str, Any]:
    enabled_ids = state.enabled_tool_ids()
    return {
        "tools": [
            _tool_entry(tool, enabled_ids, state.tool_config_keys(tool.manifest.tool_id))
            for tool in tools_host.BUNDLED_TOOLS.values()
        ]
    }


def put_tool_config(tool_id: str, body: Any) -> dict[str, Any]:
    tool = _bundled_tool(tool_id)
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
    key = body.get("key")
    value = body.get("value", "")
    declared_keys = {requirement.key for requirement in tool.manifest.config}
    if not isinstance(key, str) or key not in declared_keys:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"key must be a config key declared by {tool_id}")
    if not isinstance(value, str) or len(value.encode("utf-8")) > tools_host.CONFIG_VALUE_MAX_BYTES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "value must be a string of at most 16384 bytes")
    with state.mutation() as cur:
        state.save_tool_config_value(cur, tool_id, key, value.strip())
    is_set = bool(value.strip())
    state.record_tool_event(tool_id, "config", "set" if is_set else "cleared", key)
    return {"tool_id": tool_id, "key": key, "set": is_set}


def tool_action_route(tool_id: str, operation: str, body: Any) -> Any:
    _bundled_tool(tool_id)  # 404 for unknown tools before any dispatch
    if operation == "enable":
        # Enablement is not gated on config: a tool may be enabled with partial or
        # no config (its per-tool config status is visible in the listing), and an
        # action that needs a key that is not set fails when the tool reads it.
        with state.mutation() as cur:
            state.set_tool_enabled(cur, tool_id, True)
        state.record_tool_event(tool_id, "enablement", "enabled", "")
        return {"tool_id": tool_id, "enabled": True}
    if operation == "disable":
        with state.mutation() as cur:
            state.set_tool_enabled(cur, tool_id, False)
        state.record_tool_event(tool_id, "enablement", "disabled", "")
        return {"tool_id": tool_id, "enabled": False}
    # The connect flows are tool-owned OAuth, invoked from the admin UI (the only
    # exposed API: the browser callback lands on /oauth/callback here). The admin
    # service reverse-proxies the whole flow to the tools service verbatim, the
    # same way it reverse-proxies app-backend requests, so no OAuth tool code
    # runs here and the code exchange and token revoke use the tools service's
    # egress. The tools service is the single validator: it checks the params,
    # the connection kind, and the enabled gate (disconnect deliberately skips
    # the gate so stored tokens can always be revoked).
    return _tools_operator_request(f"/operator/tools/{tool_id}/{operation}", body)


def decide_tool_approval(approval_id: str, decision: str, tool_id: str) -> Any:
    # Deciding runs the approved payload (a third-party call needing egress), so
    # the tools service owns it; it checks that tool_id owns the approval before
    # spending it and maps a missing approval to 404, a non-pending one to 409.
    return _tools_operator_request(f"/operator/tools/{tool_id}/approvals/{approval_id}/{decision}")
