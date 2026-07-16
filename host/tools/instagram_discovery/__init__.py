"""Public Instagram discovery through the unofficial ScrapeCreators API."""

from __future__ import annotations

import datetime
import math
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any, cast

from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink, DataSummaryPoint, SetupStep, ToolManifest
from host.tools.results import ActionExecuted, ActionFailed, ActionResult, ApprovalResult
from host.tools.shared.web import WebRequestError, json_request

API_ORIGIN = "https://api.scrapecreators.com"
SEARCH_REELS_PATH = "/v2/instagram/reels/search"
TRENDING_REELS_PATH = "/v1/instagram/reels/trending"
HASHTAG_PATH = "/v1/instagram/search/hashtag"
AUDIO_REELS_PATH = "/v1/instagram/audio/reels"
REEL_DETAILS_PATH = "/v1/instagram/post"
MAX_RESULTS = 25
MAX_QUERY_CHARS = 200
MAX_CAPTION_CHARS = 2_200
DATE_WINDOWS = ("last-hour", "last-day", "last-week", "last-month", "last-year")
AUDIO_ID_RE = re.compile(r"^[0-9]{1,30}$")

REEL_PROPERTIES: JSONObject = {
    "id": {"type": "string"},
    "shortcode": {"type": "string"},
    "url": {"type": "string"},
    "caption": {"type": "string"},
    "username": {"type": "string"},
    "taken_at": {"type": "string"},
    "like_count": {"type": "string"},
    "comment_count": {"type": "string"},
    "play_count": {"type": "string"},
    "view_count": {"type": "string"},
    "duration_seconds": {"type": "string"},
    "video_url": {"type": "string"},
    "image_url": {"type": "string"},
    "audio_id": {"type": "string"},
    "audio_name": {"type": "string"},
}
LIST_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status", "message", "reels"],
    "properties": {
        "status": {"type": "string"},
        "message": {"type": "string"},
        "reels": {"type": "array", "items": {"type": "object", "properties": REEL_PROPERTIES}},
        "next_cursor": {"type": "string"},
        "has_more": {"type": "boolean"},
    },
}
DETAIL_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status", "message", "reel"],
    "properties": {
        "status": {"type": "string"},
        "message": {"type": "string"},
        "reel": {"type": "object", "properties": REEL_PROPERTIES},
    },
}

MANIFEST = ToolManifest(
    tool_id="instagram_discovery",
    display_name="Instagram Discovery",
    description=(
        "Lets your agent discover public Instagram Reels by keyword, feed, hashtag, or audio "
        "without connecting an Instagram account."
    ),
    connection="enable_only",
    actions=(
        ActionSpec(
            id="search_reels",
            description=(
                "Search Google-indexed public Instagram Reels by keyword through ScrapeCreators and return "
                "normalized captions, creator names, dates, engagement metrics, media URLs, and audio ids. "
                "This is not Instagram's logged-in search and can miss unindexed, private, new, or deleted Reels. "
                "Each page runs immediately and spends one ScrapeCreators credit."
            ),
            data_policy=(
                "Sends the keyword, optional relative date window and page number with the deployment API key to "
                "ScrapeCreators. ScrapeCreators searches Google-indexed public Instagram pages and returns public "
                "Reel metadata; the bounded normalized result enters active model context. No Instagram account or "
                "cookie is supplied. This read-only action runs directly with no approval."
            ),
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Keyword or phrase to find in public Reels, up to 200 characters."},
                    "limit": {"type": "string", "description": "Maximum unique Reels returned from this response, 1-25 (default 10); changing it does not change the one-credit request cost."},
                    "date_posted": {"type": "string", "enum": list(DATE_WINDOWS), "description": "Optional Google-indexed relative window: last-hour, last-day, last-week, last-month, or last-year."},
                    "page": {"type": "string", "description": "Provider results page, 1-100 (default 1); each page is a separate one-credit request."},
                },
                "additionalProperties": False,
            },
            output_schema=LIST_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="get_trending_reels",
            description=(
                "Return unique public Reels from Instagram's public instagram.com/reels discovery page through "
                "ScrapeCreators, with captions, creators, dates, engagement metrics, media URLs, and audio ids. "
                "This feed can contain duplicates and changes between calls; it is not an objective global ranking "
                "or a personalized logged-in feed. Runs immediately and spends one ScrapeCreators credit."
            ),
            data_policy=(
                "Sends only the deployment API key to ScrapeCreators's trending-Reels endpoint. ScrapeCreators reads "
                "Instagram's public Reels page and returns public Reel metadata; TrustyClaw deduplicates and bounds "
                "the normalized result before it enters active model context. This read-only action runs directly "
                "with no approval."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "string", "description": "Maximum unique Reels returned from this response, 1-25 (default 10); changing it does not change the one-credit request cost."},
                },
                "additionalProperties": False,
            },
            output_schema=LIST_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="search_hashtag",
            description=(
                "Search Google-indexed public Instagram posts for one hashtag through ScrapeCreators. By default "
                "returns Reels only; set reels_only=false to include public image and carousel posts. Returns "
                "normalized public metadata and a next_cursor when another provider page exists. Each page runs "
                "immediately and spends one ScrapeCreators credit."
            ),
            data_policy=(
                "Sends the hashtag, optional relative date window, media-type filter and pagination cursor with the "
                "deployment API key to ScrapeCreators. ScrapeCreators uses Google-indexed public pages and returns "
                "public Instagram metadata; the bounded normalized result enters active model context. No Instagram "
                "account or cookie is supplied. This read-only action runs directly with no approval."
            ),
            input_schema={
                "type": "object",
                "required": ["hashtag"],
                "properties": {
                    "hashtag": {"type": "string", "description": "One hashtag with or without #, up to 100 characters; spaces are not allowed."},
                    "reels_only": {"type": "boolean", "description": "Return only Reels (default true); false also permits public images and carousels."},
                    "limit": {"type": "string", "description": "Maximum unique results returned, 1-25 (default 10); changing it does not change the one-credit request cost."},
                    "date_posted": {"type": "string", "enum": list(DATE_WINDOWS), "description": "Optional Google-indexed relative window: last-hour, last-day, last-week, last-month, or last-year."},
                    "cursor": {"type": "string", "description": "Opaque next_cursor from the prior search_hashtag result; omit for the first page. Each cursor request spends one credit."},
                },
                "additionalProperties": False,
            },
            output_schema=LIST_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="get_reels_by_audio",
            description=(
                "List public Instagram Reels associated with one numeric audio id, returning normalized captions, "
                "creators, dates, engagement metrics, media URLs, and audio metadata. Get an audio_id from another "
                "discovery result or an Instagram audio-page URL. Use next_cursor for another provider page; each "
                "page runs immediately and spends one ScrapeCreators credit."
            ),
            data_policy=(
                "Sends the numeric Instagram audio id and optional pagination cursor with the deployment API key to "
                "ScrapeCreators. ScrapeCreators returns public Reel metadata; the bounded normalized result enters "
                "active model context. No Instagram account or cookie is supplied. This read-only action runs "
                "directly with no approval."
            ),
            input_schema={
                "type": "object",
                "required": ["audio_id"],
                "properties": {
                    "audio_id": {"type": "string", "description": "Numeric Instagram audio id from an audio-page URL or another Instagram Discovery result."},
                    "limit": {"type": "string", "description": "Maximum unique Reels returned, 1-25 (default 10); changing it does not change the one-credit request cost."},
                    "cursor": {"type": "string", "description": "Opaque next_cursor from the prior get_reels_by_audio result; omit for the first page. Each cursor request spends one credit."},
                },
                "additionalProperties": False,
            },
            output_schema=LIST_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="get_reel_details",
            description=(
                "Read current public metadata for one known instagram.com Reel URL through ScrapeCreators, including "
                "caption, creator, timestamp, engagement counts, media URLs, duration, and audio id when available. "
                "Use this after a discovery action when its summary lacks details. The source must still be public; "
                "the action runs immediately and spends one ScrapeCreators credit."
            ),
            data_policy=(
                "Sends the public Instagram Reel URL, trim=true, download_media=false, and the deployment API key to "
                "ScrapeCreators. ScrapeCreators retrieves the public page and returns metadata; TrustyClaw requests "
                "no permanent media copy and returns a bounded normalized record to active model context. No "
                "Instagram account or cookie is supplied. This read-only action runs directly with no approval."
            ),
            input_schema={
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "description": "Public https://www.instagram.com/reel/... URL from discovery or supplied by the operator."},
                },
                "additionalProperties": False,
            },
            output_schema=DETAIL_OUTPUT_SCHEMA,
        ),
    ),
    config=(
        ConfigRequirement(
            key="SCRAPECREATORS_API_KEY",
            description="ScrapeCreators API key used only for this public Instagram discovery tool.",
        ),
    ),
    protections=(
        "The tool has no Instagram login, cookie, or session and exposes only five fixed read-only endpoints for public content. It cannot follow, like, comment, message, or publish.",
        "Results include only the public Reel details your agent needs, with at most 25 unique items per request.",
    ),
    technical_details=(
        "TrustyClaw accepts only valid hashtags, numeric audio ids, and instagram.com Reel URLs. It asks ScrapeCreators not to download media, removes duplicate Reels, and maps vendor responses to fixed fields before returning them.",
    ),
    setup_steps=(
        SetupStep(
            title="Create a ScrapeCreators account",
            description="Create an account in the ScrapeCreators dashboard and confirm the account has usable credits. The provider currently offers trial credits and prepaid credits; each discovery request consumes provider credits even when the result is empty. No Instagram account is needed for this tool.",
            link_url="https://app.scrapecreators.com/",
            link_label="Open the ScrapeCreators dashboard",
        ),
        SetupStep(
            title="Review the unofficial-data boundary",
            description="ScrapeCreators is not affiliated with Instagram. It retrieves public third-party data and states that endpoints can change or stop when external sites change. Confirm that the intended use complies with applicable law, Instagram terms, and ScrapeCreators acceptable-use rules.",
            link_url="https://scrapecreators.com/terms",
            link_label="Read ScrapeCreators terms",
        ),
        SetupStep(
            title="Configure and enable Instagram Discovery",
            show_config=True,
            description="Copy the API key from the ScrapeCreators dashboard. Expand Instagram Discovery in Internet Access and Tools, save it as SCRAPECREATORS_API_KEY, then enable the tool. There is no Instagram OAuth, cookie, password, or account connection step; never paste an Instagram session into this field.",
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                description=(
                    "All data in a discovery request that passes TrustyClaw's validation is sent to ScrapeCreators: keyword text, "
                    "a hashtag, date, page, or cursor values, an audio id, or a validated public Instagram Reel URL, plus the "
                    "ScrapeCreators API key. Requests rejected by TrustyClaw do not leave the host. Requests rejected by "
                    "ScrapeCreators have already left the host and may be represented in its retained request metadata and error "
                    "logs. No Instagram account or credential is sent."
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(label="ScrapeCreators", text="Requests go to ScrapeCreators, an unofficial scraping vendor with no Meta or Instagram service commitment."),
                    DataSummaryPoint(label="Instagram public pages", text="ScrapeCreators makes its own upstream requests to public Instagram pages and Google-indexed lookups. TrustyClaw cannot see or constrain those upstream requests."),
                ),
            ),
            DataSummaryCard(
                title="What ScrapeCreators can do with it",
                description=(
                    "ScrapeCreators says it may keep API responses, request metadata, usage and error logs, and IP addresses to "
                    "operate, debug, secure, and improve the service, uses providers including Stripe, Google Analytics, and "
                    "PostHog, and says it does not sell personal information. Its terms place lawful-use responsibility on the "
                    "customer."
                ),
                links=(
                    DataSummaryLink(label="ScrapeCreators privacy policy", url="https://scrapecreators.com/privacy"),
                    DataSummaryLink(label="ScrapeCreators terms of service", url="https://scrapecreators.com/terms"),
                ),
            ),
            DataSummaryCard(
                title="How long ScrapeCreators retains it",
                description="ScrapeCreators says usage logs may be retained indefinitely and states no fixed period for other request data.",
                links=(
                    DataSummaryLink(label="ScrapeCreators privacy policy", url="https://scrapecreators.com/privacy"),
                ),
            ),
        ),
    ),
)


def _text(value: object, *, limit: int = 2_048) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _integer(value: object) -> str:
    if isinstance(value, bool):
        return "0"
    if isinstance(value, int):
        return str(max(0, value))
    if isinstance(value, float):
        # A non-finite float (inf/nan, e.g. from a JSON 1e999) raises
        # OverflowError/ValueError on int(); treat it as unknown rather than
        # failing the whole action on one absurd provider value.
        try:
            return str(max(0, int(value))) if value >= 0 else "0"
        except (ValueError, OverflowError):
            return "0"
    if isinstance(value, str):
        try:
            return str(max(0, int(float(value.replace(",", "")))))
        except (ValueError, OverflowError):
            return "0"
    return "0"


def _number(value: object) -> str:
    if isinstance(value, bool):
        return "0"
    if isinstance(value, (int, float)) and value >= 0:
        try:
            number = float(value)
        except OverflowError:
            return "0"
        return str(number) if math.isfinite(number) else "0"
    return "0"


def _bounded_int(value: JSONValue | None, *, name: str, default: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be an integer from 1 to {maximum}.")
    if isinstance(value, str):
        digits = value.strip()
        if not digits.isascii() or not digits.isdecimal() or len(digits) > 3:
            raise ValueError(f"{name} must be an integer from 1 to {maximum}.")
        number = int(digits)
    else:
        number = value
    if not 1 <= number <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}.")
    return number


def _date_window(value: JSONValue | None) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or value not in DATE_WINDOWS:
        raise ValueError(f"date_posted must be one of: {', '.join(DATE_WINDOWS)}.")
    return value


def _cursor(value: JSONValue | None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("cursor must be a non-empty string when supplied.")
    cursor = value.strip()
    if len(cursor) > 1_000:
        raise ValueError("cursor must be at most 1000 characters.")
    return cursor


def _query(value: JSONValue | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Instagram Reel search query is required.")
    query = value.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(
            f"Instagram Reel search query must be at most {MAX_QUERY_CHARS} characters."
        )
    return query


def _hashtag(value: JSONValue | None) -> str:
    if not isinstance(value, str):
        raise ValueError("hashtag must be one non-empty hashtag up to 100 characters, with no spaces.")
    hashtag = value.strip().lstrip("#")
    if not hashtag or len(hashtag) > 100 or any(char.isspace() for char in hashtag):
        raise ValueError("hashtag must be one non-empty hashtag up to 100 characters, with no spaces.")
    return hashtag


def _audio_id(value: JSONValue | None) -> str:
    audio_id = _text(value, limit=31)
    if not AUDIO_ID_RE.fullmatch(audio_id):
        raise ValueError("audio_id must contain 1-30 digits.")
    return audio_id


def _reel_url(value: JSONValue | None) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 2_048:
        raise ValueError("url must be a public https://www.instagram.com/reel/... URL.")
    url = value.strip()
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("url must be a public https://www.instagram.com/reel/... URL.") from exc
    segments = [segment for segment in parsed.path.split("/") if segment]
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in {"instagram.com", "www.instagram.com"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or len(segments) != 2
        or segments[0] != "reel"
        or not _SHORTCODE_RE.fullmatch(segments[1])
    ):
        raise ValueError("url must be a public https://www.instagram.com/reel/... URL.")
    return f"https://www.instagram.com/reel/{segments[1]}/"


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, dict) else {}


def _nested(value: object, *keys: str) -> object:
    current = value
    for key in keys:
        current = _mapping(current).get(key)
    return current


def _first(*values: object) -> object:
    for value in values:
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return None


def _caption(raw: Mapping[str, Any]) -> str:
    caption = raw.get("caption")
    if isinstance(caption, dict):
        caption = caption.get("text")
    edges = _nested(raw, "edge_media_to_caption", "edges")
    edge_text: object = None
    if isinstance(edges, list) and edges:
        edge_text = _nested(edges[0], "node", "text")
    return _text(_first(caption, raw.get("description"), edge_text), limit=MAX_CAPTION_CHARS)


def _timestamp(value: object) -> str:
    if isinstance(value, str):
        return value.strip()[:100]
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        try:
            return datetime.datetime.fromtimestamp(value, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


# The response comes from an unofficial scraper, so URLs it hands back are
# untrusted. Only accept media/permalink URLs on Instagram or Meta's CDN hosts
# so an attacker-controlled response can't plant an arbitrary link the agent
# then relays or fetches.
_ALLOWED_URL_HOSTS = ("instagram.com", "cdninstagram.com", "fbcdn.net")
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _instagram_url(value: object, *, limit: int = 4_096) -> str:
    if not isinstance(value, str) or len(value.strip()) > limit:
        return ""
    text = value.strip()
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in text):
        return ""
    if not text.startswith("https://"):
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and any(host == allowed or host.endswith("." + allowed) for allowed in _ALLOWED_URL_HOSTS)
    ):
        return text
    return ""


def _instagram_permalink(value: object) -> str:
    if not isinstance(value, str) or len(value.strip()) > 2_048:
        return ""
    try:
        return _reel_url(value.strip())
    except (ValueError, TypeError):
        return ""


def _first_url(raw: Mapping[str, Any], direct_keys: tuple[str, ...], versions_key: str) -> str:
    for key in direct_keys:
        value = _instagram_url(raw.get(key))
        if value:
            return value
    versions = raw.get(versions_key)
    if isinstance(versions, list):
        for version in versions:
            value = _instagram_url(_mapping(version).get("url"))
            if value:
                return value
    return ""


def _image_url(raw: Mapping[str, Any]) -> str:
    direct = _first_url(raw, ("image_url", "display_url", "thumbnail_src"), "image_versions")
    if direct:
        return direct
    candidates = _nested(raw, "image_versions2", "candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            value = _instagram_url(_mapping(candidate).get("url"))
            if value:
                return value
    return ""


def _normalize_reel(value: object) -> JSONObject:
    raw = _mapping(value)
    shortcode = _text(raw.get("shortcode"), limit=100)
    # Prefer a permalink derived from the validated shortcode; only fall back to
    # the provider's url if it is a real Instagram URL (never an arbitrary host).
    if shortcode and _SHORTCODE_RE.fullmatch(shortcode):
        url = f"https://www.instagram.com/reel/{shortcode}/"
    else:
        shortcode = ""
        url = _instagram_permalink(raw.get("url"))
    user = _mapping(_first(raw.get("user"), raw.get("owner")))
    music = _mapping(_first(raw.get("clips_music_attribution_info"), _nested(raw, "clips_metadata", "music_info", "music_asset_info"), _nested(raw, "clips_metadata", "original_sound_info")))
    play_count = _integer(_first(raw.get("play_count"), raw.get("video_play_count"), raw.get("ig_play_count")))
    return {
        "id": _text(_first(raw.get("id"), raw.get("pk")), limit=100),
        "shortcode": shortcode,
        "url": url,
        "caption": _caption(raw),
        "username": _text(_first(raw.get("username"), user.get("username")), limit=100),
        "taken_at": _timestamp(_first(raw.get("taken_at"), raw.get("created_at"), raw.get("taken_at_timestamp"))),
        "like_count": _integer(_first(raw.get("like_count"), _nested(raw, "edge_media_preview_like", "count"))),
        "comment_count": _integer(_first(raw.get("comment_count"), _nested(raw, "edge_media_to_parent_comment", "count"), _nested(raw, "edge_media_to_comment", "count"))),
        "play_count": play_count,
        "view_count": _integer(_first(raw.get("view_count"), raw.get("video_view_count"), play_count)),
        "duration_seconds": _number(_first(raw.get("video_duration"), raw.get("duration"))),
        "video_url": _first_url(raw, ("video_url",), "video_versions"),
        "image_url": _image_url(raw),
        "audio_id": _text(_first(music.get("audio_id"), music.get("id")), limit=100),
        "audio_name": _text(_first(music.get("song_name"), music.get("title"), music.get("display_artist"), music.get("artist_name")), limit=300),
    }


def _reel_values(response: JSONObject) -> list[object]:
    candidates = (
        response.get("reels"),
        response.get("posts"),
        _nested(response, "data", "reels"),
        _nested(response, "data", "items"),
        response.get("items"),
    )
    for candidate in candidates:
        if isinstance(candidate, list):
            return cast(list[object], candidate)
    return []


def _normalized_reels(response: JSONObject, *, limit: int) -> list[JSONObject]:
    output: list[JSONObject] = []
    seen: set[str] = set()
    for raw in _reel_values(response):
        item = _normalize_reel(raw)
        key = str(item.get("shortcode") or item.get("url") or item.get("id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(item)
        if len(output) >= limit:
            break
    return output


def _details_media(response: JSONObject) -> object:
    candidates = (
        _nested(response, "data", "xdt_shortcode_media"),
        response.get("reel"),
        response.get("post"),
        response.get("data"),
    )
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


def _provider_request(api_key: str, path: str, params: Mapping[str, str]) -> JSONObject:
    query = urllib.parse.urlencode(params)
    url = f"{API_ORIGIN}{path}{'?' + query if query else ''}"
    response = json_request(
        "GET",
        url,
        headers={"x-api-key": api_key},
        failure_message="ScrapeCreators Instagram request failed.",
        invalid_response_message="ScrapeCreators returned an invalid Instagram response.",
    )
    if response.get("success") is False:
        raise RuntimeError("ScrapeCreators rejected the Instagram request.")
    return response


def _list_result(response: JSONObject, *, limit: int, label: str) -> ActionExecuted:
    reels = _normalized_reels(response, limit=limit)
    cursor = _text(_first(response.get("cursor"), response.get("next_cursor"), _nested(response, "data", "cursor")), limit=1_000)
    has_more_raw = _first(response.get("has_more"), _nested(response, "data", "has_more"))
    result: JSONObject = {
        "status": "success_executed",
        "message": f"{label} returned {len(reels)} unique public result(s).",
        "reels": cast(list[JSONValue], reels),
        "next_cursor": cursor,
        "has_more": bool(has_more_raw) if isinstance(has_more_raw, bool) else bool(cursor),
    }
    return ActionExecuted(result)


class InstagramDiscoveryTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            api_key = api.config["SCRAPECREATORS_API_KEY"]
            limit = _bounded_int(tool_input.get("limit"), name="limit", default=10, maximum=MAX_RESULTS)
            if action == "search_reels":
                params = {"query": _query(tool_input.get("query")), "page": str(_bounded_int(tool_input.get("page"), name="page", default=1, maximum=100))}
                date_posted = _date_window(tool_input.get("date_posted"))
                if date_posted:
                    params["date_posted"] = date_posted
                return _list_result(_provider_request(api_key, SEARCH_REELS_PATH, params), limit=limit, label="Keyword search")
            if action == "get_trending_reels":
                return _list_result(_provider_request(api_key, TRENDING_REELS_PATH, {}), limit=limit, label="Trending Reels")
            if action == "search_hashtag":
                reels_only = tool_input.get("reels_only", True)
                if not isinstance(reels_only, bool):
                    raise ValueError("reels_only must be true or false.")
                params = {"hashtag": _hashtag(tool_input.get("hashtag")), "media_type": "reels" if reels_only else "all"}
                date_posted = _date_window(tool_input.get("date_posted"))
                cursor = _cursor(tool_input.get("cursor"))
                if date_posted:
                    params["date_posted"] = date_posted
                if cursor:
                    params["cursor"] = cursor
                return _list_result(_provider_request(api_key, HASHTAG_PATH, params), limit=limit, label="Hashtag search")
            if action == "get_reels_by_audio":
                params = {"audio_id": _audio_id(tool_input.get("audio_id"))}
                cursor = _cursor(tool_input.get("cursor"))
                if cursor:
                    params["cursor"] = cursor
                return _list_result(_provider_request(api_key, AUDIO_REELS_PATH, params), limit=limit, label="Audio lookup")
            if action == "get_reel_details":
                response = _provider_request(
                    api_key,
                    REEL_DETAILS_PATH,
                    {"url": _reel_url(tool_input.get("url")), "trim": "true", "download_media": "false"},
                )
                reel = _normalize_reel(_details_media(response))
                if not reel.get("id") and not reel.get("shortcode") and not reel.get("url"):
                    raise RuntimeError("ScrapeCreators returned no public Reel details.")
                return ActionExecuted({"status": "success_executed", "message": "Retrieved public Reel details.", "reel": reel})
            return ActionFailed("Unsupported Instagram Discovery action.")
        except WebRequestError as exc:
            if exc.status in {401, 403}:
                return ActionFailed("ScrapeCreators rejected the configured API key or account access.")
            if exc.status == 429:
                return ActionFailed("ScrapeCreators request capacity or account credits were exhausted.")
            if exc.status == 404:
                return ActionFailed("ScrapeCreators could not find public Instagram content for that request.")
            return ActionFailed(str(exc))
        except (ValueError, RuntimeError) as exc:
            # Input validation and config-unset carry curated messages; an
            # unexpected exception must not leak its raw text to the agent.
            return ActionFailed(str(exc) or "Instagram Discovery request failed.")
        except Exception:
            return ActionFailed("Instagram Discovery request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        del approval, api
        return ActionFailed("Instagram Discovery has no approval-gated actions.")


BUNDLED_TOOL = InstagramDiscoveryTool()
