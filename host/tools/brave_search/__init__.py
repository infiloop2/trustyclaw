"""Brave Search tool package."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ActionSpec, ConfigRequirement, ToolManifest
from host.tools.results import ActionExecuted, ActionFailed, ActionResult, ApprovalResult
from host.tools.host_api import ApprovalRecord, HostAPI

BRAVE_LLM_CONTEXT_ENDPOINT = "https://api.search.brave.com/res/v1/llm/context"
DEFAULT_COUNT = 8
MAX_COUNT = 10
DEFAULT_MAX_TOKENS = 4_096
MAX_SEARCH_QUERY_WORDS = 50
MAX_SEARCH_QUERY_CHARS = 400


MANIFEST = ToolManifest(
    tool_id="brave_search",
    display_name="Brave Search",
    description="Read-only web search grounding via the Brave Search API.",
    connection="enable_only",
    actions=(
        ActionSpec(id="search_web",
            description="Search the web and return grounding results (title, url, snippets).",
            data_policy=(
                "Search queries supplied by the user or agent are sent to Brave Search "
                "to return grounding results. This action runs directly with no approval."
            ),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "required": ["status"],
                "properties": {
                    "status": {"type": "string"},
                    "message": {"type": "string"},
                    "query": {"type": "string"},
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "url": {"type": "string"},
                                "snippets": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                },
            },
        ),
    ),
    config=(ConfigRequirement(key="BRAVE_SEARCH_API_KEY", description="Brave Search API subscription key for the hosting deployment."),),
    setup_guide=(
        "Create a Brave Search API account and subscribe to a plan (the free tier works "
        "for grounding) at https://api-dashboard.search.brave.com/, generate an API "
        "subscription key, then set BRAVE_SEARCH_API_KEY here and enable the tool. "
        "There is no OAuth connect step."
    ),
)


def _string_value(value: JSONValue | None) -> str:
    return value.strip() if isinstance(value, str) else ""


def _extract_search_query(tool_input: JSONObject) -> str:
    query = _string_value(tool_input.get("query"))
    if not query:
        raise ValueError("Brave Search query is required.")
    words = query.split()
    if len(words) > MAX_SEARCH_QUERY_WORDS:
        query = " ".join(words[:MAX_SEARCH_QUERY_WORDS])
    return query[:MAX_SEARCH_QUERY_CHARS]


def _request_payload(tool_input: JSONObject) -> JSONObject:
    return {
        "q": _extract_search_query(tool_input),
        "count": DEFAULT_COUNT,
        "maximum_number_of_urls": DEFAULT_COUNT,
        "maximum_number_of_tokens": DEFAULT_MAX_TOKENS,
        "context_threshold_mode": "balanced",
    }


def _post_brave_context(api_key: str, payload: JSONObject) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        BRAVE_LLM_CONTEXT_ENDPOINT,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Subscription-Token": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        message = f"Brave Search API returned HTTP {status_code}."
        if status_code in {401, 403}:
            message = "Brave Search API rejected the configured API key."
        elif status_code == 429:
            message = "Brave Search API rate limit was reached."
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Brave Search API request failed.") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("Brave Search API returned an invalid response.")
    return decoded


def _string_list(value: Any, *, max_items: int = 4) -> list[JSONValue]:
    if not isinstance(value, list):
        return []
    output: list[JSONValue] = []
    for item in value[:max_items]:
        if isinstance(item, str) and item.strip():
            output.append(item.strip()[:1_200])
    return output


def _grounding_results(raw_response: dict[str, Any]) -> list[JSONObject]:
    grounding = raw_response.get("grounding")
    if not isinstance(grounding, dict):
        return []
    generic = grounding.get("generic")
    if not isinstance(generic, list):
        return []
    results: list[JSONObject] = []
    for item in generic[:MAX_COUNT]:
        if not isinstance(item, dict):
            continue
        url = _string_value(cast(JSONValue | None, item.get("url")))
        title = _string_value(cast(JSONValue | None, item.get("title")))
        if not url and not title:
            continue
        results.append({"title": title, "url": url, "snippets": _string_list(item.get("snippets"))})
    return results


class BraveSearchTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        if action != "search_web":
            return ActionFailed("Unsupported Brave Search action.")
        try:
            api_key = api.config["BRAVE_SEARCH_API_KEY"]
            request_payload = _request_payload(tool_input)
            raw_response = _post_brave_context(api_key, request_payload)
            results = _grounding_results(raw_response)
            result: JSONObject = {
                "status": "success_executed",
                "message": f"Brave Search returned {len(results)} grounding result(s).",
                "query": request_payload["q"],
                "results": cast(list[JSONValue], results),
            }
            return ActionExecuted(result)
        except Exception as exc:
            return ActionFailed(str(exc) or "Brave Search tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        del approval, api
        return ActionFailed("Brave Search has no approval-gated actions.")


# The instance the host discovers (see host.runtime.tools_host).
BUNDLED_TOOL = BraveSearchTool()
