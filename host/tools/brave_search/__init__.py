"""Brave Search tool package."""

from __future__ import annotations

import urllib.parse
from typing import Any, cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import (
    ActionSpec,
    ConfigRequirement,
    DataSummary,
    DataSummaryCard,
    DataSummaryLink,
    SetupStep,
    ToolManifest,
)
from host.tools.results import ActionExecuted, ActionFailed, ActionResult, ApprovalResult
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.shared.web import WebRequestError, json_request

BRAVE_LLM_CONTEXT_ENDPOINT = "https://api.search.brave.com/res/v1/llm/context"
DEFAULT_COUNT = 8
MAX_COUNT = 10
DEFAULT_MAX_TOKENS = 4_096
MAX_SEARCH_QUERY_WORDS = 50
MAX_SEARCH_QUERY_CHARS = 400


MANIFEST = ToolManifest(
    tool_id="brave_search",
    display_name="Brave Search",
    description="Lets your agent search the public web with Brave Search.",
    connection="enable_only",
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                description=(
                    "Only the search query (shortened to a maximum length), fixed result-size options, and the API key that "
                    "authenticates the request. Nothing else on this host is sent."
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                description=(
                    "Queries go only to Brave's Search API service, and results come back from Brave's own search index. "
                    "The query text is received and logged like any other request, so what the agent searches for is itself "
                    "data sent to Brave, which does not expose it anywhere else."
                ),
            ),
            DataSummaryCard(
                title="What Brave can do with it",
                description=(
                    "Brave says it keeps API query records only to bill the account, troubleshoot the service, and prevent abuse, and for no other purpose "
                    "such as advertising or profiling. It also says it collects no identifiers that can link a query to you or your device."
                ),
                links=(
                    DataSummaryLink(label="Brave Search API privacy notice", url="https://api-dashboard.search.brave.com/documentation/resources/privacy-notice"),
                    DataSummaryLink(label="Brave Search API terms", url="https://api-dashboard.search.brave.com/documentation/resources/terms-of-service"),
                ),
            ),
            DataSummaryCard(
                title="How long Brave retains it",
                description="Brave retains API query records for at most 90 days, subject to its legal obligations.",
                links=(
                    DataSummaryLink(label="Brave Search API privacy notice", url="https://api-dashboard.search.brave.com/documentation/resources/privacy-notice"),
                ),
            ),
        ),
    ),
    actions=(
        ActionSpec(id="search_web",
            description="Search the web and return grounding results (title, url, snippets).",
            data_policy=(
                "Searches the public web through Brave and returns result titles, URLs, and snippets. "
                "Only the query text and fixed result options leave the host. Runs directly with no approval."
            ),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Public-web search text; supports Brave operators such as site:, quotes, and exclusions."}},
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
    protections=(
        "Only the search query and the API key that authenticates the request are sent to Brave. The API key stays in write-only host config and is never returned to the agent.",
        "The tool is read-only, and its requests do not require operator approval.",
    ),
    setup_steps=(
        SetupStep(
            title="Create a Brave Search API account",
            description="Open the Brave Search API dashboard, create an account, and subscribe to the Search plan. The included monthly credits are enough to try the integration.",
            link_url="https://api-dashboard.search.brave.com/",
            link_label="Open the Brave Search API dashboard",
        ),
        SetupStep(
            title="Generate an API key",
            description="In API Keys, choose Add API Key, give it a recognizable name, and copy the subscription token. Treat it like a password.",
            link_url="https://api-dashboard.search.brave.com/documentation/guides/authentication",
            link_label="View Brave authentication instructions",
        ),
        SetupStep(
            title="Configure and enable Brave Search",
            description="Expand Brave Search in Internet Access and Tools, save the token under the configuration key below, then enable the tool. There is no separate OAuth connection step. Run one web-grounded request and confirm a successful search_web call in the Tool audit log.",
            show_config=True,
        ),
    ),
)


def _string_value(value: JSONValue | None) -> str:
    return value.strip() if isinstance(value, str) else ""


def _result_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 2_048:
        return ""
    url = value.strip()
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None and not 1 <= port <= 65_535
    ):
        return ""
    return url


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
    try:
        return json_request(
            "POST",
            BRAVE_LLM_CONTEXT_ENDPOINT,
            body=payload,
            headers={"x-subscription-token": api_key},
            failure_message="Brave Search API request failed.",
            invalid_response_message="Brave Search API returned an invalid response.",
        )
    except WebRequestError as exc:
        message = f"Brave Search API returned HTTP {exc.status}." if exc.status else str(exc)
        if exc.status in {401, 403}:
            message = "Brave Search API rejected the configured API key."
        elif exc.status == 429:
            message = "Brave Search API rate limit was reached."
        raise RuntimeError(message) from exc


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
        url = _result_url(item.get("url"))
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
