"""Public LinkedIn-post discovery through SerpApi's Google Search API."""

from __future__ import annotations

import re
import urllib.parse
from typing import cast

from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink, DataSummaryPoint, SetupStep, ToolManifest
from host.tools.results import ActionExecuted, ActionFailed, ActionResult, ApprovalResult
from host.tools.shared.web import WebRequestError, json_request

SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"
MAX_QUERY_CHARS = 300
MAX_RESULTS = 10
MAX_PAGE = 10

RESULT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status", "message", "query", "results"],
    "properties": {
        "status": {"type": "string"},
        "message": {"type": "string"},
        "query": {"type": "string"},
        "page": {"type": "string"},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "url", "snippet"],
                "properties": {
                    "position": {"type": "string"},
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "snippet": {"type": "string"},
                    "date": {"type": "string"},
                    "source": {"type": "string"},
                },
            },
        },
    },
}

MANIFEST = ToolManifest(
    tool_id="linkedin_discovery",
    display_name="LinkedIn Discovery",
    description=(
        "Lets your agent search Google-indexed public LinkedIn posts without connecting "
        "a LinkedIn account."
    ),
    connection="enable_only",
    actions=(
        ActionSpec(
            id="search_posts",
            description=(
                "Search Google-indexed public LinkedIn post pages for a topic and return bounded titles, "
                "LinkedIn URLs, dates, sources, and search snippets. This is discovery and snippet reading, "
                "not a LinkedIn feed or full-post reader: unindexed, private, deleted, login-gated, or text "
                "omitted from Google's snippet is unavailable. The tool adds site:linkedin.com/posts itself, "
                "runs immediately, and spends one SerpApi search credit unless SerpApi serves a free cached result."
            ),
            data_policy=(
                "Sends the topic inside a site:linkedin.com/posts Google query, page offset, language, country, "
                "safe-search setting, and the deployment API key to SerpApi. Returns only bounded organic-result "
                "metadata for LinkedIn post URLs; the result enters active model context. LinkedIn is not contacted "
                "by this host. This read-only action runs directly with no approval."
            ),
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Topic, phrase, company, or person to find in public LinkedIn posts, up to 300 characters; do not include site: because the tool scopes it automatically.",
                    },
                    "limit": {
                        "type": "string",
                        "description": "Maximum matching LinkedIn results returned, 1-10 (default 10). One API request is charged regardless of this value.",
                    },
                    "page": {
                        "type": "string",
                        "description": "Google results page, 1-10 (default 1). Use a later page only after the prior page; each page is a separate SerpApi request.",
                    },
                },
                "additionalProperties": False,
            },
            output_schema=RESULT_SCHEMA,
        ),
    ),
    config=(
        ConfigRequirement(
            key="SERPAPI_API_KEY",
            description="SerpApi account API key used only for this LinkedIn public-post search tool.",
        ),
    ),
    protections=(
        "The host never logs in to or sends a request to LinkedIn. The only provider request is a bounded site-restricted Google search through SerpApi.",
        "Only public LinkedIn post URLs are returned. The API key stays in write-only tool config, queries are capped at 300 characters, and results are capped at 10.",
    ),
    setup_steps=(
        SetupStep(
            title="Create a SerpApi account",
            description="Create a SerpApi account and choose a plan with enough Google Search credits. The current free plan is suitable for a setup test; the Starter plan currently includes 1,000 searches per month. One uncached search_posts page consumes one search even when Google returns no matching LinkedIn post.",
            link_url="https://serpapi.com/pricing",
            link_label="View SerpApi plans",
        ),
        SetupStep(
            title="Copy the API key",
            description="Open the SerpApi dashboard and copy the private API key. TrustyClaw uses the synchronous Google Search JSON endpoint and allows SerpApi's one-hour cache.",
            link_url="https://serpapi.com/manage-api-key",
            link_label="Open the SerpApi API key page",
        ),
        SetupStep(
            title="Configure and enable LinkedIn Discovery",
            show_config=True,
            description="Expand LinkedIn Discovery in Internet Access and Tools, save the key as SERPAPI_API_KEY, then enable the tool. There is no LinkedIn app, login, OAuth consent, cookie, or session to configure; never paste LinkedIn credentials into this field. SerpApi offers Zero Data Retention through ZeroTrace on Enterprise plans, but TrustyClaw does not currently enable it.",
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                description=(
                    "Only the topic text, sent as a site:linkedin.com/posts Google query with fixed English, United States, "
                    "safe-search, and paging options. This tool holds no LinkedIn credential; besides the SerpApi key that "
                    "authenticates each request, nothing else on this host is sent. The topic text is received and logged like "
                    "any other search, so what the agent searches for is itself data sent to SerpApi and Google."
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(label="SerpApi", text="The query goes to SerpApi, which runs the search from its own infrastructure and may serve identical queries from its one-hour cache."),
                    DataSummaryPoint(label="Google", text="SerpApi submits the query to Google Search, so Google also sees the topic text along with SerpApi's request metadata rather than this host's."),
                ),
            ),
            DataSummaryCard(
                title="What SerpApi can do with it",
                description=(
                    "SerpApi keeps search and account data to operate, secure, debug, and support the service under its legal "
                    "policy. Zero Data Retention is available through ZeroTrace on Enterprise plans, but TrustyClaw does not "
                    "currently enable it, so standard-search handling applies. Google processes the search under its own "
                    "Privacy Policy."
                ),
                links=(
                    DataSummaryLink(label="SerpApi ZeroTrace", url="https://serpapi.com/zero-trace-mode"),
                    DataSummaryLink(label="SerpApi legal and privacy policy", url="https://serpapi.com/legal"),
                    DataSummaryLink(label="Google Privacy Policy", url="https://policies.google.com/privacy"),
                ),
            ),
            DataSummaryCard(
                title="How long SerpApi retains it",
                description="SerpApi states no fixed retention period for standard searches. Its Enterprise ZeroTrace mode offers Zero Data Retention, but that mode is not available in TrustyClaw. Google's retention follows its own policy.",
                links=(
                    DataSummaryLink(label="SerpApi ZeroTrace", url="https://serpapi.com/zero-trace-mode"),
                    DataSummaryLink(label="SerpApi legal and privacy policy", url="https://serpapi.com/legal"),
                ),
            ),
        ),
    ),
)


def _text(value: object, *, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _bounded_int(value: JSONValue | None, *, name: str, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    if isinstance(value, str):
        digits = value.strip()
        if not digits.isascii() or not digits.isdecimal() or len(digits) > 3:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
        number = int(digits)
    else:
        number = value
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    return number


def _query(tool_input: JSONObject) -> str:
    value = tool_input.get("query")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("LinkedIn discovery query is required.")
    query = value.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(
            f"LinkedIn discovery query must be at most {MAX_QUERY_CHARS} characters."
        )
    if "site:" in query.lower():
        raise ValueError("Do not include a site: operator; LinkedIn Discovery adds its fixed scope.")
    if re.search(r"(?:^|\s)OR(?:\s|$)", query, flags=re.IGNORECASE):
        raise ValueError("Do not include an OR operator; LinkedIn Discovery keeps every result scoped to LinkedIn posts.")
    return query


def _linkedin_post_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 2_048:
        return ""
    url = value.strip()
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url):
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    segments = [urllib.parse.unquote(segment) for segment in parsed.path.split("/") if segment]
    # Accept regional LinkedIn hosts (uk.linkedin.com, de.linkedin.com, ...) that
    # Google commonly returns, not just the bare/www host; normalize to www.
    if (
        parsed.scheme != "https"
        or (host != "linkedin.com" and not host.endswith(".linkedin.com"))
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or len(segments) < 2
        or segments[0] != "posts"
        or any(segment in {".", ".."} for segment in segments)
    ):
        return ""
    return urllib.parse.urlunsplit(("https", "www.linkedin.com", parsed.path, "", ""))


def _organic_results(response: JSONObject, *, limit: int) -> list[JSONObject]:
    raw_results = response.get("organic_results")
    if not isinstance(raw_results, list):
        return []
    results: list[JSONObject] = []
    seen: set[str] = set()
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        url = _linkedin_post_url(raw.get("link"))
        if not url or url in seen:
            continue
        seen.add(url)
        position = raw.get("position")
        item: JSONObject = {
            "position": str(position if isinstance(position, int) and not isinstance(position, bool) else len(results) + 1),
            "title": _text(raw.get("title"), limit=500),
            "url": url,
            "snippet": _text(raw.get("snippet"), limit=1_500),
            "date": _text(raw.get("date"), limit=100),
            "source": _text(raw.get("source"), limit=200),
        }
        results.append(item)
        if len(results) >= limit:
            break
    return results


def _search(api_key: str, query: str, page: int) -> JSONObject:
    params = {
        "engine": "google",
        "q": f"site:linkedin.com/posts {query}",
        "hl": "en",
        "gl": "us",
        "safe": "active",
        "num": str(MAX_RESULTS),
        "start": str((page - 1) * MAX_RESULTS),
        "api_key": api_key,
    }
    return json_request(
        "GET",
        f"{SERPAPI_SEARCH_URL}?{urllib.parse.urlencode(params)}",
        failure_message="SerpApi LinkedIn search failed.",
        invalid_response_message="SerpApi returned an invalid LinkedIn search response.",
    )


class LinkedInDiscoveryTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        if action != "search_posts":
            return ActionFailed("Unsupported LinkedIn Discovery action.")
        try:
            query = _query(tool_input)
            limit = _bounded_int(tool_input.get("limit"), name="limit", default=MAX_RESULTS, minimum=1, maximum=MAX_RESULTS)
            page = _bounded_int(tool_input.get("page"), name="page", default=1, minimum=1, maximum=MAX_PAGE)
            response = _search(api.config["SERPAPI_API_KEY"], query, page)
            error = response.get("error")
            if isinstance(error, str) and error:
                # SerpApi returns HTTP 200 with this error text when a search
                # simply matched nothing; a site:linkedin.com/posts scope hits
                # it often. Report a valid empty result rather than a failure
                # (which would make the agent retry and burn search credits).
                if "hasn't returned any results" in error.lower() or "no results" in error.lower():
                    results: list[JSONObject] = []
                else:
                    raise RuntimeError("SerpApi rejected the LinkedIn search request.")
            else:
                results = _organic_results(response, limit=limit)
            return ActionExecuted(
                {
                    "status": "success_executed",
                    "message": f"Found {len(results)} indexed public LinkedIn post result(s).",
                    "query": query,
                    "page": str(page),
                    "results": cast(list[JSONValue], results),
                }
            )
        except WebRequestError as exc:
            if exc.status in {401, 403}:
                return ActionFailed("SerpApi rejected the configured API key.")
            if exc.status == 429:
                return ActionFailed("SerpApi search capacity or account credits were exhausted.")
            return ActionFailed(str(exc))
        except (ValueError, RuntimeError) as exc:
            # Input validation and config-unset carry curated messages.
            return ActionFailed(str(exc) or "LinkedIn Discovery request failed.")
        except Exception:
            return ActionFailed("LinkedIn Discovery request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        del approval, api
        return ActionFailed("LinkedIn Discovery has no approval-gated actions.")


BUNDLED_TOOL = LinkedInDiscoveryTool()
