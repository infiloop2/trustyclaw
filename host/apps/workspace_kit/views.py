"""Declarative artifact-view validation, the single source of truth for the
view blocks a workspace agent may author.

A view is a bounded list of typed blocks. Every block shape, field, and length
is validated here before storage; the matching ``ui/view_blocks.js`` renderer
escapes each field before it reaches the DOM. This module is self-contained and
context-free so it is unit-testable and safe to import from anywhere in the
kit.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any


SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False

VIEW_BLOCK_TYPES = (
    "heading",
    "text",
    "callout",
    "metrics",
    "cards",
    "details",
    "list",
    "table",
    "checklist",
    "progress",
    "timeline",
    "kanban",
    "chart",
    "code",
    "button",
    "toggle",
    "field",
    "divider",
)
INTERACTIVE_VIEW_BLOCK_TYPES = frozenset({"button", "toggle", "field"})

MAX_VIEW_BLOCKS = 64
VIEW_LIMIT = 16_000
CONTROL_LABEL_LIMIT = 120
FIELD_VALUE_LIMIT = 1_000
FIELD_PLACEHOLDER_LIMIT = 200


def validate_view(view: Any) -> str | None:
    if not isinstance(view, list) or not view:
        return "view must be a non-empty list of blocks"
    if len(view) > MAX_VIEW_BLOCKS:
        return f"view must have at most {MAX_VIEW_BLOCKS} blocks"
    control_ids: set[str] = set()
    for index, block in enumerate(view):
        error = _validate_block(block)
        if error:
            return f"view block {index + 1}: {error}"
        if block["type"] in INTERACTIVE_VIEW_BLOCK_TYPES:
            control_id = block["control_id"]
            if control_id in control_ids:
                return f"view block {index + 1}: duplicate control_id {control_id!r}"
            control_ids.add(control_id)
    if len(json.dumps(view, sort_keys=True)) > VIEW_LIMIT:
        return f"view must be at most {VIEW_LIMIT} characters when serialized"
    return None


def _validate_block(block: Any) -> str | None:
    if not isinstance(block, dict):
        return "block must be an object"
    kind = block.get("type")
    if kind == "heading":
        error = _block_fields(block, {"text"}, {"level"})
        if error:
            return error
        if "level" in block and block["level"] not in (1, 2, 3):
            return "heading level must be 1, 2, or 3"
        return _block_text(block, "text", 200)
    if kind == "text":
        return _block_fields(block, {"text"}, set()) or _block_text(block, "text", 4000)
    if kind == "callout":
        error = _block_fields(block, {"text"}, {"title", "tone"})
        if error:
            return error
        if block.get("tone", "info") not in ("info", "success", "warning", "danger"):
            return "callout tone must be info, success, warning, or danger"
        if "title" in block:
            error = _block_text(block, "title", 120)
            if error:
                return error
        return _block_text(block, "text", 2000)
    if kind == "metrics":
        error = _block_fields(block, {"items"}, set())
        if error:
            return error
        items = block["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 8:
            return "metrics items must be a list of 1 to 8 entries"
        for item in items:
            if not isinstance(item, dict):
                return "metrics items must be objects"
            extra = sorted(set(item) - {"label", "value", "delta"})
            if extra or "label" not in item or "value" not in item:
                return "metrics items need label and value (delta optional)"
            item_error = _block_text(item, "label", 80) or _block_text(item, "value", 40)
            if item_error:
                return item_error
            if "delta" in item:
                item_error = _block_text(item, "delta", 40)
                if item_error:
                    return item_error
        return None
    if kind == "cards":
        error = _block_fields(block, {"items"}, set())
        if error:
            return error
        items = block["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 12:
            return "cards items must be a list of 1 to 12 entries"
        for item in items:
            if not isinstance(item, dict):
                return "cards items must be objects"
            if set(item) - {"title", "text", "badge", "tone"} or "title" not in item:
                return "cards items need title (text, badge, and tone optional)"
            if item.get("tone", "neutral") not in ("neutral", "info", "success", "warning", "danger"):
                return "card tone must be neutral, info, success, warning, or danger"
            item_error = _block_text(item, "title", 120)
            if item_error:
                return item_error
            for field, limit in (("text", 1000), ("badge", 40)):
                if field in item:
                    item_error = _block_text(item, field, limit)
                    if item_error:
                        return item_error
        return None
    if kind == "details":
        error = _block_fields(block, {"items"}, set())
        if error:
            return error
        items = block["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 30:
            return "details items must be a list of 1 to 30 entries"
        for item in items:
            if not isinstance(item, dict) or set(item) != {"label", "value"}:
                return "details items need exactly label and value"
            item_error = _block_text(item, "label", 80) or _block_text(item, "value", 500)
            if item_error:
                return item_error
        return None
    if kind == "list":
        error = _block_fields(block, {"items"}, {"style"})
        if error:
            return error
        if block.get("style", "bullet") not in ("bullet", "number"):
            return "list style must be bullet or number"
        items = block["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            return "list items must be a list of 1 to 50 strings"
        if any(not isinstance(item, str) or not item or len(item) > 500 for item in items):
            return "list items must be non-empty strings of at most 500 characters"
        return None
    if kind == "table":
        error = _block_fields(block, {"columns", "rows"}, set())
        if error:
            return error
        columns = block["columns"]
        rows = block["rows"]
        if not isinstance(columns, list) or not 1 <= len(columns) <= 8:
            return "table columns must be a list of 1 to 8 names"
        if any(not isinstance(column, str) or len(column) > 80 for column in columns):
            return "table columns must be strings of at most 80 characters"
        if not isinstance(rows, list) or len(rows) > 50:
            return "table rows must be a list of at most 50 rows"
        for row in rows:
            if not isinstance(row, list) or len(row) != len(columns):
                return "each table row must match the number of columns"
            if any(not isinstance(cell, str) or len(cell) > 200 for cell in row):
                return "table cells must be strings of at most 200 characters"
        return None
    if kind == "checklist":
        error = _block_fields(block, {"items"}, set())
        if error:
            return error
        items = block["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            return "checklist items must be a list of 1 to 50 entries"
        for item in items:
            if not isinstance(item, dict) or set(item) != {"text", "done"}:
                return "checklist items need exactly text and done"
            if not isinstance(item["done"], bool):
                return "checklist done must be true or false"
            item_error = _block_text(item, "text", 200)
            if item_error:
                return item_error
        return None
    if kind == "progress":
        error = _block_fields(block, {"value"}, {"label"})
        if error:
            return error
        if "label" in block:
            error = _block_text(block, "label", 80)
            if error:
                return error
        value = block["value"]
        if not _is_finite_number(value) or not 0 <= value <= 100:
            return "progress value must be a number between 0 and 100"
        return None
    if kind == "timeline":
        error = _block_fields(block, {"items"}, set())
        if error:
            return error
        items = block["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 30:
            return "timeline items must be a list of 1 to 30 entries"
        for item in items:
            if not isinstance(item, dict):
                return "timeline items must be objects"
            if set(item) - {"title", "status", "text", "time"} or not {"title", "status"} <= set(item):
                return "timeline items need title and status (text and time optional)"
            if item["status"] not in ("done", "current", "upcoming"):
                return "timeline status must be done, current, or upcoming"
            item_error = _block_text(item, "title", 120)
            if item_error:
                return item_error
            for field, limit in (("text", 500), ("time", 80)):
                if field in item:
                    item_error = _block_text(item, field, limit)
                    if item_error:
                        return item_error
        return None
    if kind == "kanban":
        error = _block_fields(block, {"columns"}, set())
        if error:
            return error
        columns = block["columns"]
        if not isinstance(columns, list) or not 1 <= len(columns) <= 6:
            return "kanban columns must be a list of 1 to 6 entries"
        for column in columns:
            if not isinstance(column, dict) or set(column) != {"title", "items"}:
                return "kanban columns need exactly title and items"
            column_error = _block_text(column, "title", 80)
            if column_error:
                return column_error
            items = column["items"]
            if not isinstance(items, list) or len(items) > 20:
                return "kanban column items must be a list of at most 20 strings"
            if any(not isinstance(item, str) or not item or len(item) > 200 for item in items):
                return "kanban items must be non-empty strings of at most 200 characters"
        return None
    if kind == "chart":
        error = _block_fields(block, {"kind", "points"}, {"label"})
        if error:
            return error
        if block["kind"] not in ("bar", "line"):
            return "chart kind must be bar or line"
        if "label" in block:
            error = _block_text(block, "label", 80)
            if error:
                return error
        points = block["points"]
        if not isinstance(points, list) or not 2 <= len(points) <= 60:
            return "chart points must be a list of 2 to 60 points"
        for point in points:
            if not isinstance(point, dict) or set(point) != {"label", "value"}:
                return "chart points need exactly label and value"
            value = point["value"]
            if not _is_finite_number(value):
                return "chart point values must be finite numbers"
            point_error = _block_text(point, "label", 40)
            if point_error:
                return point_error
        return None
    if kind == "code":
        error = _block_fields(block, {"text"}, {"language"})
        if error:
            return error
        if "language" in block:
            error = _block_text(block, "language", 20)
            if error:
                return error
        return _block_text(block, "text", 8000)
    if kind == "button":
        error = _block_fields(block, {"control_id", "label"}, {"tone"})
        if error:
            return error
        if block.get("tone", "primary") not in ("primary", "neutral", "danger"):
            return "button tone must be primary, neutral, or danger"
        return _validate_control_identity(block)
    if kind == "toggle":
        error = _block_fields(block, {"control_id", "label", "value"}, set())
        if error:
            return error
        if not isinstance(block["value"], bool):
            return "toggle value must be true or false"
        return _validate_control_identity(block)
    if kind == "field":
        error = _block_fields(block, {"control_id", "label", "value"}, {"placeholder"})
        if error:
            return error
        identity_error = _validate_control_identity(block)
        if identity_error:
            return identity_error
        value = block["value"]
        if not isinstance(value, str) or len(value) > FIELD_VALUE_LIMIT:
            return f"field value must be a string of at most {FIELD_VALUE_LIMIT} characters"
        if "placeholder" in block:
            placeholder = block["placeholder"]
            if not isinstance(placeholder, str) or len(placeholder) > FIELD_PLACEHOLDER_LIMIT:
                return f"field placeholder must be a string of at most {FIELD_PLACEHOLDER_LIMIT} characters"
        return None
    if kind == "divider":
        return _block_fields(block, set(), set())
    return f"unknown block type {kind!r}; allowed types: {', '.join(VIEW_BLOCK_TYPES)}"


def _block_fields(block: dict[str, Any], required: set[str], optional: set[str]) -> str | None:
    missing = sorted(required - set(block))
    if missing:
        return f"missing field {missing[0]}"
    extra = sorted(set(block) - required - optional - {"type"})
    if extra:
        return f"unsupported field {extra[0]}"
    return None


def _block_text(block: dict[str, Any], key: str, limit: int) -> str | None:
    value = block.get(key)
    if not isinstance(value, str) or not value:
        return f"{key} must be a non-empty string"
    if len(value) > limit:
        return f"{key} must be at most {limit} characters"
    return None


def _validate_control_identity(block: dict[str, Any]) -> str | None:
    value = block.get("control_id")
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        return f"control_id must match {SLUG_RE.pattern}"
    return _block_text(block, "label", CONTROL_LABEL_LIMIT)
