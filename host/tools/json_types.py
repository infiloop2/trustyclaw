"""Minimal JSON type aliases.

This package is standalone and must not depend on any host's utility libraries,
so it defines its own JSON value types. All data crossing
the tool/host boundary — action inputs, results, stored state, approval
payloads — is plain JSON.
"""

from __future__ import annotations

JSONValue = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)
JSONObject = dict[str, JSONValue]
