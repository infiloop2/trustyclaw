"""The host side of the bundled tool framework.

This module is TrustyClaw's implementation of the host API contract in
docs/architecture/tools/tool-contract.md: it owns the registry of bundled tool
packages, builds the scoped ``HostAPI`` handed to every tool call (credentials
and approvals backed by admin state, config from the operator-supplied per-tool
config), validates action input against the manifest schemas, runs the
single-use approval lifecycle, and records every call as an audit event.

Each TrustyClaw host is single-operator, so credential and approval state only
need per-tool partitions.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
from pathlib import Path
import time
from typing import Any, Mapping, cast

from host.runtime import state
import host.tools
from host.tools import (
    ActionExecuted,
    ActionFailed,
    ActionPendingApproval,
    ActionResult,
    ApprovalExecuted,
    ApprovalRecord,
    ApprovalStatus,
    JSONObject,
    StoredCredential,
    Tool,
    ToolManifest,
)
from host.tools.manifest import TOOL_ID_RE

# Host approval policy: a pending decision is spent after 24 hours (swept by
# the admin API's hourly maintenance pass), and decided records are kept as
# bounded history like finished tasks.
APPROVAL_PENDING_TTL_SECONDS = 24 * 3600
PENDING_APPROVAL_LIMIT = 1000
APPROVAL_HISTORY_LIMIT = 10_000
# Size and format limits from docs/architecture/tools/tool-contract.md.
CREDENTIAL_VALUE_MAX_BYTES = 64 * 1024
PAYLOAD_MAX_BYTES = 64 * 1024
SUMMARY_MAX_BYTES = 500
CONFIG_VALUE_MAX_BYTES = 16_384

# Packages under host/tools that are framework helpers, not tools. Every other
# package must expose BUNDLED_TOOL, so a new tool cannot be silently skipped.
_NON_TOOL_PACKAGES = frozenset({"shared"})


def _tool_package_names(root: Path) -> tuple[str, ...]:
    names: list[str] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if child.name == "__pycache__":
            continue
        if child.is_symlink():
            raise RuntimeError(f"{child}: tool package directory must not be a symlink")
        if not child.is_dir():
            continue
        if not TOOL_ID_RE.fullmatch(child.name):
            raise RuntimeError(f"{child}: tool package directory must match {TOOL_ID_RE.pattern}")
        init_path = child / "__init__.py"
        if not init_path.is_file() or init_path.is_symlink():
            raise RuntimeError(f"{child}: tool package must contain a regular __init__.py file")
        if child.name not in _NON_TOOL_PACKAGES:
            names.append(child.name)
    return tuple(names)


def _discover_bundled_tools(
    root: Path | None = None,
    module_prefix: str = "host.tools",
) -> tuple[Tool, ...]:
    """The bundled tools are discovered from the packages under host/tools —
    like the app platform's manifest scan — so adding a tool is adding its
    package (with a module-level ``BUNDLED_TOOL`` instance), not editing a
    hand-kept registry here."""
    if root is None:
        root = Path(host.tools.__path__[0])
    discovered: list[Tool] = []
    for package_name in _tool_package_names(root):
        module = importlib.import_module(f"{module_prefix}.{package_name}")
        tool = getattr(module, "BUNDLED_TOOL", None)
        if tool is None:
            raise RuntimeError(
                f"host/tools/{package_name} is not a tool package: it must expose a "
                "module-level BUNDLED_TOOL instance (or be listed in _NON_TOOL_PACKAGES)."
            )
        manifest = getattr(tool, "manifest", None)
        if not isinstance(manifest, ToolManifest):
            raise RuntimeError(
                f"host/tools/{package_name} BUNDLED_TOOL must expose a ToolManifest."
            )
        if manifest.tool_id != package_name:
            raise RuntimeError(
                f"host/tools/{package_name} declares tool_id {manifest.tool_id!r}; "
                "tool_id must match the package directory name."
            )
        discovered.append(tool)
    return tuple(discovered)


def _bundled_tool_map(tools: tuple[Tool, ...]) -> dict[str, Tool]:
    bundled: dict[str, Tool] = {}
    for tool in tools:
        tool_id = tool.manifest.tool_id
        if tool_id in bundled:
            raise RuntimeError(f"Duplicate bundled tool_id: {tool_id}")
        for spec in tool.manifest.actions:
            for label, schema in (("input_schema", spec.input_schema), ("output_schema", spec.output_schema)):
                error = unsupported_schema_error(schema, allow_empty=label == "output_schema")
                if error:
                    raise RuntimeError(f"{tool_id}.{spec.id}.{label}: {error}")
        # The manifest's connection kind and the Tool.credentials flow are two
        # declarations of one fact; pin them consistent at registration so no
        # consumer has to guess which to trust (tool-contract.md).
        if (tool.manifest.connection == "oauth") != (tool.credentials is not None):
            raise RuntimeError(
                f"{tool_id}: connection={tool.manifest.connection!r} disagrees with"
                f" credentials={'set' if tool.credentials is not None else 'None'}"
            )
        bundled[tool_id] = tool
    return bundled




class ToolCallError(Exception):
    """A tool call the host refuses to make (unknown tool or action, disabled
    tool, missing config, invalid input). The message is safe to return."""


class ApprovalBackpressureError(Exception):
    """The host refuses another pending approval until the operator catches up."""


class ToolConfigKeyUnsetError(RuntimeError):
    """A tool read a config key the operator has not set. The message is
    operator-actionable and safe to return even when the tool package lets it
    escape uncaught."""


def _ensure_json_object(value: Any, *, what: str, max_bytes: int) -> str:
    """Validate a JSON object per the host API's value rules and return its
    compact serialization (used for the byte-size limit). json.dumps with
    allow_nan=False is the whole check: it rejects NaN/inf and any non-JSON
    value; values come from in-repo tool packages only."""
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object.")
    try:
        serialized = json.dumps(value, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be a JSON object with JSON-serializable values.") from exc
    if len(serialized.encode("utf-8")) > max_bytes:
        raise ValueError(f"{what} exceeds {max_bytes} bytes.")
    return serialized


class HostCredentials:
    """``tools.Credentials`` backed by the tool_credentials table: one stored
    OAuth credential per tool."""

    def __init__(self, tool_id: str) -> None:
        self._tool_id = tool_id

    def load(self) -> StoredCredential | None:
        record = state.tool_credential(self._tool_id)
        return cast(StoredCredential, record) if record is not None else None

    def save(self, credential: StoredCredential) -> None:
        record = dict(credential)
        _ensure_json_object(record, what="Credential", max_bytes=CREDENTIAL_VALUE_MAX_BYTES)
        state.put_tool_credential(self._tool_id, record)

    def clear(self) -> None:
        state.delete_tool_credential(self._tool_id)


class HostApprovals:
    """``tools.Approvals`` backed by the tool_approvals table, scoped to one
    tool's partition. Decisions and terminal outcomes are applied by the
    module-level lifecycle functions, not through this tool-facing surface."""

    def __init__(self, manifest: ToolManifest) -> None:
        self._manifest = manifest

    def request(self, *, action_id: str, summary: str, payload: JSONObject) -> ApprovalRecord:
        spec = self._manifest.action(action_id)
        if spec is None:
            raise ValueError("Approval action is not in the tool manifest.")
        if not isinstance(summary, str) or not 1 <= len(summary.encode("utf-8")) <= SUMMARY_MAX_BYTES:
            raise ValueError(f"Approval summary must be 1-{SUMMARY_MAX_BYTES} UTF-8 bytes.")
        _ensure_json_object(payload, what="Approval payload", max_bytes=PAYLOAD_MAX_BYTES)
        try:
            record = state.insert_tool_approval(
                self._manifest.tool_id, action_id, summary, payload, int(time.time()),
                pending_limit=PENDING_APPROVAL_LIMIT,
            )
        except state.PendingToolApprovalLimitReached as exc:
            raise ApprovalBackpressureError(
                f"Too many pending tool approvals. Decide or deny existing approvals before queuing more."
            ) from exc
        return _approval_record(record)


def _approval_record(record: dict[str, Any]) -> ApprovalRecord:
    status: ApprovalStatus = record["status"]
    return ApprovalRecord(
        approval_id=record["approval_id"],
        action_id=record["action_id"],
        status=status,
        payload=record["payload"],
        summary=record["summary"],
        created_at=record["created_at"],
        decided_at=record["decided_at"],
    )


@dataclass(frozen=True)
class HostToolAPI:
    """The ``tools.HostAPI`` bundle for one tool call."""

    credentials: HostCredentials
    config: Mapping[str, str]
    approvals: HostApprovals


class _ToolConfigView(dict[str, str]):
    """The config mapping handed to tool code. Enablement is not gated on
    config, so a tool can run before its keys are set; reading an unset key
    must fail with an operator-actionable message (which tool packages surface
    verbatim), not a bare KeyError whose str() is just the quoted key name."""

    def __missing__(self, key: str) -> str:
        raise ToolConfigKeyUnsetError(
            f"Tool config {key} is not set. The operator must set it in the admin UI's Tools tab."
        )


def host_api_for(tool: Tool) -> HostToolAPI:
    manifest = tool.manifest
    config = state.tool_config_values(manifest.tool_id, [entry.key for entry in manifest.config])
    return HostToolAPI(
        credentials=HostCredentials(manifest.tool_id),
        config=_ToolConfigView(config),
        approvals=HostApprovals(manifest),
    )


def bundled_tool(tool_id: str) -> Tool:
    tool = BUNDLED_TOOLS.get(tool_id) if isinstance(tool_id, str) else None
    if tool is None:
        raise ToolCallError(f"Unknown tool: {tool_id}.")
    return tool


def enabled_tool(tool_id: str) -> Tool:
    """The tool, ready to call: it only has to be enabled. Config is not gated --
    a tool may be enabled with partial or no config, so an action that needs a
    key that is not set fails when the tool reads it, and actions that do not need
    that key still work."""
    tool = bundled_tool(tool_id)
    if tool_id not in state.enabled_tool_ids():
        raise ToolCallError(f"Tool {tool_id} is not enabled.")
    return tool


# -- action execution ----------------------------------------------------------


def execute_action(tool_id: str, action: str, tool_input: Any) -> dict[str, Any]:
    """Run one agent-initiated action call end to end: resolve the enabled
    tool, schema-validate the input, invoke the package, audit, and return
    the JSON shape of the result."""
    tool = enabled_tool(tool_id)
    spec = tool.manifest.action(action) if isinstance(action, str) else None
    if spec is None:
        raise ToolCallError(f"Tool {tool_id} has no action {action}.")
    if tool_input is None:
        tool_input = {}
    if not isinstance(tool_input, dict):
        raise ToolCallError("Tool input must be a JSON object.")
    schema_error = validate_against_schema(tool_input, spec.input_schema)
    if schema_error:
        raise ToolCallError(f"Invalid input for {tool_id}.{action}: {schema_error}")
    try:
        result = tool.execute(action, tool_input, host_api_for(tool))
    except (ApprovalBackpressureError, ToolConfigKeyUnsetError) as exc:
        result = ActionFailed(str(exc))
    except Exception:
        result = ActionFailed("Tool call failed.")
    result_json = _result_json(result)
    result_json = _validate_output(tool_id, action, spec, result_json)
    _audit(tool_id, action, result_json)
    return result_json


def _execute_approved(record: dict[str, Any]) -> dict[str, Any]:
    """Run one approved action and audit the outcome. Never raises: any
    failure (tool disabled since queueing, config unset, tool exception)
    becomes a failed result so the approval always reaches a terminal state.
    Approved results carry a user-visible message, no output schema."""
    tool_id, action, approval_id = record["tool_id"], record["action_id"], record["approval_id"]
    try:
        tool = enabled_tool(tool_id)
        result: Any = tool.execute_approved(_approval_record(record), host_api_for(tool))
    except (ToolCallError, ToolConfigKeyUnsetError) as exc:
        result = ActionFailed(str(exc))
    except Exception:
        result = ActionFailed("Tool call failed.")
    if isinstance(result, ActionPendingApproval):
        # The contract forbids this; treat it as a failed execution.
        result = ActionFailed("Tool returned a pending result for an approved action.")
    result_json = _result_json(result)
    _audit(tool_id, action, result_json, detail=_approval_audit_detail(approval_id, result_json))
    return result_json


def _result_json(result: Any) -> dict[str, Any]:
    if isinstance(result, ActionExecuted):
        return {"status": "executed", "result": result.result}
    if isinstance(result, ApprovalExecuted):
        # An approved action's outcome is a single user-visible message.
        return {"status": "executed", "message": result.message}
    if isinstance(result, ActionPendingApproval):
        # The approval id already carries the poll token (state._approval_id).
        return {
            "status": "pending_approval",
            "approval_id": result.approval_id,
            "summary": result.summary,
        }
    return {
        "status": "failed",
        "error": result.error,
        "reconnect_required": result.reconnect_required,
    }


def _validate_output(tool_id: str, action: str, spec: Any, result_json: dict[str, Any]) -> dict[str, Any]:
    # Only direct actions carry a JSON "result" to validate; approved actions
    # return a user-visible "message" with no output schema.
    if result_json.get("status") != "executed" or "result" not in result_json or spec is None:
        return result_json
    error = validate_against_schema(result_json["result"], spec.output_schema, path="result")
    if not error:
        return result_json
    return {
        "status": "failed",
        "error": f"Invalid output for {tool_id}.{action}: {error}",
        "reconnect_required": False,
    }


def _approval_audit_detail(approval_id: str, result_json: dict[str, Any]) -> str:
    error = result_json.get("error")
    return f"{approval_id}: {error}" if error else approval_id


def _audit(tool_id: str, action: str, result_json: dict[str, Any], *, detail: str | None = None) -> None:
    outcome = result_json["status"]
    if detail is None:
        detail = result_json.get("error") or result_json.get("approval_id") or ""
    if not isinstance(detail, str):
        detail = ""
    state.record_tool_event(tool_id, action, outcome, detail)


# -- approval lifecycle --------------------------------------------------------


def decide_approval(approval_id: str, decision: str) -> dict[str, Any]:
    """Apply an operator decision. Approving runs ``execute_approved`` at most
    once and records the terminal outcome; denying is terminal immediately.
    Raises ToolCallError when the record is absent or already decided."""
    record = state.tool_approval(approval_id)
    if record is None:
        raise ToolCallError(f"Unknown approval: {approval_id}.")
    now = int(time.time())
    if decision == "deny":
        if not state.transition_tool_approval(approval_id, "pending", "denied", now):
            raise ToolCallError(f"Approval {approval_id} is not pending.")
        # Deny has no execute_approved call to audit, so record it here.
        state.record_tool_event(record["tool_id"], record["action_id"], "denied", approval_id)
        return {"approval": state.tool_approval(approval_id)}
    if not state.transition_tool_approval(approval_id, "pending", "approved", now):
        raise ToolCallError(f"Approval {approval_id} is not pending.")
    # The transition above is the at-most-once gate; the record handed to the
    # tool carries the state the transition just wrote.
    result_json = _execute_approved(record | {"status": "approved", "decided_at": now})
    outcome = "executed" if result_json["status"] == "executed" else "failed"
    # The stored result is the outcome's single text per the contract: the
    # user-visible message when the approved action executed, the error when
    # it failed (the status column says which one it is).
    result_text = str(result_json.get("message") or result_json.get("error") or "")
    state.transition_tool_approval(approval_id, "approved", outcome, int(time.time()), result=result_text)
    return {"approval": state.tool_approval(approval_id), "result": result_json}


def recover_interrupted_approvals() -> None:
    """Mark approvals stuck in ``approved`` as failed at service start: the
    one execute_approved call cannot survive a restart, and a single-use
    approval whose outcome is unknown is spent."""
    state.fail_approved_tool_approvals(int(time.time()))


def maintain_approvals() -> None:
    state.expire_tool_approvals(int(time.time()) - APPROVAL_PENDING_TTL_SECONDS)
    state.prune_tool_approvals(APPROVAL_HISTORY_LIMIT)


# -- schema validation ----------------------------------------------------------


# Exactly the keywords validate_against_schema enforces, so a manifest cannot
# declare a constraint the validator would silently skip. Checked at tool
# registration (unsupported_schema_error). CI registers every package before
# merge, and runtime registration keeps the same fail-closed backstop.
_SCHEMA_COMMON_KEYS = frozenset({"type", "enum", "description"})
_SCHEMA_KEYS_BY_TYPE: dict[str, frozenset[str]] = {
    "object": frozenset({"properties", "required", "additionalProperties"}),
    "array": frozenset({"items", "minItems", "maxItems"}),
    "string": frozenset(),
    "boolean": frozenset(),
}


def unsupported_schema_error(
    schema: Any,
    path: str = "schema",
    *,
    allow_empty: bool = False,
) -> str:
    """Reject a declared manifest schema that steps outside the subset
    validate_against_schema enforces. Returns an error string, or "" when the
    whole schema is within the subset. The declaration check is the strict
    mirror of the validator: any keyword, type, or shape the validator would
    ignore is an error here, so declaration and enforcement cannot drift."""
    if not isinstance(schema, dict):
        return f"{path} must be a JSON object."
    if "description" in schema and not isinstance(schema["description"], str):
        return f"{path}.description must be a string."
    if "oneOf" in schema:
        extra = set(schema) - {"oneOf", "description"}
        if extra:
            return f"{path} mixes oneOf with unsupported keys: {', '.join(sorted(extra))}."
        options = schema["oneOf"]
        if not isinstance(options, list) or not options:
            return f"{path}.oneOf must be a non-empty array of schemas."
        for index, option in enumerate(options):
            error = unsupported_schema_error(option, f"{path}.oneOf[{index}]")
            if error:
                return error
        return ""
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum or not all(isinstance(item, (str, int, bool)) for item in enum):
            return f"{path}.enum must be a non-empty array of scalars."
    schema_type = schema.get("type")
    if not schema and not allow_empty:
        return f"{path} is an empty schema, which validates nothing."
    if not schema:
        return ""
    if schema_type is None and "enum" in schema:
        return "" if not set(schema) - {"enum", "description"} else (
            f"{path} mixes enum with unsupported keys: {', '.join(sorted(set(schema) - {'enum', 'description'}))}."
        )
    if schema_type not in _SCHEMA_KEYS_BY_TYPE:
        return f"{path}.type {schema_type!r} is outside the supported subset."
    allowed = _SCHEMA_COMMON_KEYS | _SCHEMA_KEYS_BY_TYPE[schema_type]
    extra = set(schema) - allowed
    if extra:
        return f"{path} has unsupported keys: {', '.join(sorted(extra))}."
    if schema_type == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return f"{path}.properties must be an object."
        if not all(isinstance(name, str) for name in properties):
            return f"{path}.properties names must be strings."
        if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
            return f"{path}.additionalProperties must be a boolean."
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
            return f"{path}.required must be an array of strings."
        if len(required) != len(set(required)):
            return f"{path}.required must not contain duplicate names."
        missing = [name for name in required if name not in properties]
        if missing:
            return f"{path}.required names undeclared properties: {', '.join(sorted(missing))}."
        for name, item in properties.items():
            error = unsupported_schema_error(item, f"{path}.{name}")
            if error:
                return error
    if schema_type == "array":
        if "items" not in schema:
            return f"{path} is an array without items, so elements would go unvalidated."
        for key in ("minItems", "maxItems"):
            if key in schema and (
                isinstance(schema[key], bool)
                or not isinstance(schema[key], int)
                or schema[key] < 0
            ):
                return f"{path}.{key} must be a non-negative integer."
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and isinstance(max_items, int) and min_items > max_items:
            return f"{path}.minItems must not exceed maxItems."
        return unsupported_schema_error(schema["items"], f"{path}.items")
    return ""


def validate_against_schema(value: Any, schema: Any, path: str = "input") -> str:
    """Validate ``value`` against the JSON Schema subset tool manifests use
    (object/string/array/boolean types, properties, required,
    additionalProperties: false, enum, items, minItems/maxItems, oneOf).
    Returns an error string, or "" when the value validates. Tools still own
    semantic validation beyond the schema. The declared schemas are checked
    against exactly this subset at registration (unsupported_schema_error)."""
    if not isinstance(schema, dict):
        return ""
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        errors = []
        for option in one_of:
            error = validate_against_schema(value, option, path)
            if not error:
                return ""
            errors.append(error)
        return f"{path} matches none of the allowed shapes."
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return f"{path} must be one of: {', '.join(str(item) for item in enum)}."
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(value, dict):
            return f"{path} must be an object."
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        for name in schema.get("required") or []:
            if name not in value:
                return f"{path}.{name} is required."
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                return f"{path} has unsupported fields: {', '.join(sorted(extra))}."
        for name, item in value.items():
            if name in properties:
                error = validate_against_schema(item, properties[name], f"{path}.{name}")
                if error:
                    return error
        return ""
    if schema_type == "array":
        if not isinstance(value, list):
            return f"{path} must be an array."
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            return f"{path} must have at least {min_items} items."
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            return f"{path} must have at most {max_items} items."
        items = schema.get("items")
        for index, item in enumerate(value):
            error = validate_against_schema(item, items, f"{path}[{index}]")
            if error:
                return error
        return ""
    if schema_type == "string" and not isinstance(value, str):
        return f"{path} must be a string."
    if schema_type == "boolean" and not isinstance(value, bool):
        return f"{path} must be a boolean."
    return ""


# Discovery runs at import, after the schema declaration checker above is
# defined: an out-of-subset manifest fails the service at startup.
BUNDLED_TOOLS: dict[str, Tool] = _bundled_tool_map(_discover_bundled_tools())
