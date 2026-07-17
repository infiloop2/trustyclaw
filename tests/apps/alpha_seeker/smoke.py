"""Alpha Seeker smoke-test mock backend and UI checks.

Alpha Seeker is a workspace_kit app, so its operator surface is the generic workspace
API; this mock serves that API with Alpha Seeker's four domain artifacts (positions,
watchlist, prediction_markets, research) so the bespoke dashboard renders each
named artifact's declarative view, alongside the shared conversation feed.
"""

from __future__ import annotations

from http import HTTPStatus
import json
import re
from typing import Any, Callable

from host.session_options import public_session_options, session_config_error


ApiErrorFactory = Callable[[HTTPStatus, str], Exception]
HostApi = Callable[[str, str, dict[str, list[str]], Any], dict[str, Any]]

# Fixed literal timestamps spanning ~3 weeks of genuine use; the most recent
# entries land within the last day (today is 2026-07-16). No wall-clock reads.
_BASE = "2026-06-08T00:0{}:00Z"

WORKSPACE: dict[str, Any] = {
    "agent_runtime": "claude_code",
    "model": "opus",
    "effort": "high",
    "thread_seq": 1,
    "goal": "Find asymmetric ideas in US large-cap tech and event-driven prediction markets",
    "measurement": "A tracked idea list with entry theses, targets, and risk notes; research quality, not P&L",
    "created_at": "2026-06-25T13:28:00Z",
}
MESSAGES: list[dict[str, Any]] = [
    {"id": 1, "role": "user", "content": "Set up my research desk and refresh positions every morning.", "meta": None, "created_at": "2026-06-25T13:30:00Z"},
    {"id": 2, "role": "agent", "content": "Done. I rebuilt the **positions** artifact from a live IBKR read and scheduled a pre-market brief. Read-only mandate: I propose ideas, I never place orders.", "meta": None, "created_at": "2026-06-25T13:32:00Z"},
    {"id": 3, "role": "event", "content": 'Updated artifact "Portfolio"', "meta": {"action": "update_artifact", "artifact_id": "positions"}, "created_at": "2026-06-25T13:32:00Z"},
    {"id": 4, "role": "event", "content": 'Scheduled "Pre-market brief" every day', "meta": {"action": "create_schedule", "schedule_id": "pre_market_brief"}, "created_at": "2026-06-25T13:33:00Z"},
    {"id": 5, "role": "error", "content": 'Action rejected: artifact "old_notes" does not exist', "meta": {"action": "update_artifact"}, "created_at": "2026-06-25T13:35:00Z"},
    {"id": 6, "role": "user", "content": "Add AMD and TSM to the watchlist with entry theses and price targets.", "meta": None, "created_at": "2026-06-27T09:12:00Z"},
    {"id": 7, "role": "agent", "content": "Added **AMD** and **TSM** to the watchlist. Both sit below their trigger levels, so I am watching, not buying. Entry theses and targets are in the artifact.", "meta": None, "created_at": "2026-06-27T09:15:00Z"},
    {"id": 8, "role": "event", "content": 'Updated artifact "Watchlist"', "meta": {"action": "update_artifact", "artifact_id": "watchlist"}, "created_at": "2026-06-27T09:15:00Z"},
    {"id": 9, "role": "agent", "content": "Pre-market brief: portfolio +0.6% overnight, led by **NVDA** and **AVGO**. Every single-name weight is inside the 15% cap. No action required.", "meta": None, "created_at": "2026-07-02T12:04:00Z"},
    {"id": 10, "role": "event", "content": 'Updated artifact "Portfolio"', "meta": {"action": "update_artifact", "artifact_id": "positions"}, "created_at": "2026-07-02T12:04:00Z"},
    {"id": 11, "role": "user", "content": "Prioritise the datacenter names over consumer tech from here on.", "meta": None, "created_at": "2026-07-06T15:40:00Z"},
    {"id": 12, "role": "agent", "content": "Understood, tilting research toward datacenter compute. I also refreshed the Polymarket read: **Fed cuts in September** Yes is 0.64, up from 0.55 three weeks ago.", "meta": None, "created_at": "2026-07-06T15:42:00Z"},
    {"id": 13, "role": "event", "content": 'Updated artifact "Prediction markets"', "meta": {"action": "update_artifact", "artifact_id": "prediction_markets"}, "created_at": "2026-07-06T15:43:00Z"},
    {"id": 14, "role": "error", "content": "Action rejected: order placement is not available in this workspace (read-only mandate)", "meta": {"action": "place_order"}, "created_at": "2026-07-09T12:01:00Z"},
    {"id": 15, "role": "user", "content": "Deep-dive AMD datacenter TAM and file a research note.", "meta": None, "created_at": "2026-07-13T08:50:00Z"},
    {"id": 16, "role": "agent", "content": "Filed a note on **AMD** datacenter TAM: MI-class accelerators plus a full-rack story put the 2027 SAM near $110B; AMD needs share, not TAM, to work. Entry stays below $150.", "meta": None, "created_at": "2026-07-13T08:56:00Z"},
    {"id": 17, "role": "event", "content": 'Updated artifact "Research"', "meta": {"action": "update_artifact", "artifact_id": "research"}, "created_at": "2026-07-13T08:56:00Z"},
    {"id": 18, "role": "agent", "content": "Pre-market brief for today: net liquidation $356,025, +1.2% overnight on **NVDA**. No trades placed, proposal only.", "meta": None, "created_at": "2026-07-16T12:00:00Z"},
    {"id": 19, "role": "event", "content": 'Updated artifact "Portfolio"', "meta": {"action": "update_artifact", "artifact_id": "positions"}, "created_at": "2026-07-16T12:00:00Z"},
]
SCHEDULES: list[dict[str, Any]] = [
    {
        "schedule_id": "pre_market_brief",
        "title": "Pre-market brief",
        "every_minutes": 1440,
        "next_run_at": "2026-07-17T12:00:00Z",
        "enabled": True,
        "created_at": "2026-06-25T13:33:00Z",
        "updated_at": "2026-06-25T13:33:00Z",
        "last_run_status": "completed",
        "last_run_at": "2026-07-16T12:00:00Z",
    },
    {
        "schedule_id": "weekly_deep_dive",
        "title": "Weekly thesis review",
        "every_minutes": 10080,
        "next_run_at": "2026-07-20T14:00:00Z",
        "enabled": True,
        "created_at": "2026-06-29T14:00:00Z",
        "updated_at": "2026-06-29T14:00:00Z",
        "last_run_status": "failed",
        "last_run_at": "2026-07-13T14:00:00Z",
    },
]
ARTIFACTS: dict[str, dict[str, Any]] = {
    "positions": {
        "artifact_id": "positions",
        "title": "Portfolio",
        "data": {"as_of": "2026-07-16T11:59:00Z", "source": "ibkr_get_positions"},
        "view": [
            {"type": "callout", "title": "As of", "text": "Live read via `ibkr_get_positions` at 2026-07-16 11:59 UTC.", "tone": "info"},
            {"type": "metrics", "items": [
                {"label": "Net liquidation", "value": "$356,025", "delta": "+1.2%"},
                {"label": "Cash", "value": "$54,300"},
                {"label": "Unrealized PnL", "value": "+$28,410", "delta": "+8.7%"},
            ]},
            {"type": "table", "columns": ["Symbol", "Sector", "Qty", "Mark", "Value", "Unrealized PnL"], "rows": [
                ["AAPL", "Consumer tech", "300", "$234.10", "$70,230", "+$6,410"],
                ["MSFT", "Software", "90", "$498.90", "$44,901", "+$5,120"],
                ["AMZN", "Consumer / cloud", "140", "$238.50", "$33,390", "+$2,980"],
                ["GOOGL", "Software", "160", "$196.30", "$31,408", "+$3,240"],
                ["JPM", "Financials", "100", "$286.70", "$28,670", "+$1,910"],
                ["META", "Software", "40", "$712.40", "$28,496", "+$4,120"],
                ["XOM", "Energy", "200", "$118.40", "$23,680", "-$640"],
                ["NVDA", "Datacenter", "120", "$172.40", "$20,688", "+$3,180"],
                ["AVGO", "Datacenter", "110", "$184.20", "$20,262", "+$2,090"],
            ]},
            {"type": "chart", "kind": "bar", "label": "Position value ($)", "points": [
                {"label": "AAPL", "value": 70230},
                {"label": "MSFT", "value": 44901},
                {"label": "AMZN", "value": 33390},
                {"label": "GOOGL", "value": 31408},
                {"label": "JPM", "value": 28670},
                {"label": "META", "value": 28496},
                {"label": "XOM", "value": 23680},
                {"label": "NVDA", "value": 20688},
                {"label": "AVGO", "value": 20262},
            ]},
        ],
        "created_at": "2026-06-25T13:32:00Z",
        "updated_at": "2026-07-16T12:00:00Z",
    },
    "watchlist": {
        "artifact_id": "watchlist",
        "title": "Watchlist",
        "data": {"ideas": 5},
        "view": [
            {"type": "cards", "items": [
                {"title": "AMD", "text": "Datacenter share gains against a dominant incumbent; MI-class ramp is the catalyst. Entry below **$150**, target **$195**, stop on a broken $135 base.", "badge": "New", "tone": "info"},
                {"title": "TSM", "text": "Pricing power on leading nodes as N2 fills up. Entry below **$185**, target **$240**. Key risk is Taiwan headline volatility.", "badge": "Watching", "tone": "neutral"},
                {"title": "ASML", "text": "EUV monopoly; orders troughed and are turning. Entry below **$820**, target **$1,050**. Risk is another China export-control leg.", "badge": "Watching", "tone": "neutral"},
                {"title": "UBER", "text": "FCF inflection with buybacks starting; AV headlines are the swing factor. Entry below **$82**, target **$110**.", "badge": "Watching", "tone": "neutral"},
                {"title": "CRM", "text": "Trimmed thesis: agent products help, but seat growth is soft. Waiting below **$240** before re-engaging.", "badge": "On hold", "tone": "warning"},
            ]},
        ],
        "created_at": "2026-06-25T13:32:00Z",
        "updated_at": "2026-07-13T08:56:00Z",
    },
    "prediction_markets": {
        "artifact_id": "prediction_markets",
        "title": "Prediction markets",
        "data": {"source": "polymarket"},
        "view": [
            {"type": "table", "columns": ["Market", "Outcome", "Price", "Note"], "rows": [
                ["Fed cuts in September", "Yes", "0.64", "Tracking a data-dependent tilt"],
                ["CPI above 3.0% in July", "No", "0.68", "Cooler shelter component"],
                ["Nvidia FY revenue > $210B", "Yes", "0.57", "Datacenter demand still tight"],
                ["US 10Y above 4.5% at year-end", "No", "0.61", "Curve pricing in cuts"],
                ["OpenAI raises at >$500B valuation", "Yes", "0.44", "Thin liquidity, wide spread"],
            ]},
            {"type": "chart", "kind": "line", "label": "Fed-cut Yes price (weekly)", "points": [
                {"label": "Jun 25", "value": 0.55},
                {"label": "Jul 2", "value": 0.58},
                {"label": "Jul 6", "value": 0.62},
                {"label": "Jul 10", "value": 0.63},
                {"label": "Jul 16", "value": 0.64},
            ]},
        ],
        "created_at": "2026-06-25T13:32:00Z",
        "updated_at": "2026-07-16T12:00:00Z",
    },
    "research": {
        "artifact_id": "research",
        "title": "Research",
        "data": {"notes": 4},
        "view": [
            {"type": "heading", "text": "2026-07-16 pre-market", "level": 3},
            {"type": "text", "text": "Portfolio +1.2% overnight on **NVDA** strength. Datacenter tilt intact; no trades placed, proposal only."},
            {"type": "heading", "text": "2026-07-13 AMD datacenter TAM", "level": 3},
            {"type": "text", "text": "MI-class accelerators plus rack-scale systems put the 2027 SAM near **$110B**. The idea works on *share*, not TAM; entry stays below $150 with a hard stop under the $135 base."},
            {"type": "heading", "text": "2026-07-06 rotation note", "level": 3},
            {"type": "text", "text": "Rotating research emphasis to datacenter compute over consumer tech at the operator's request. Kept AAPL and AMZN core weights; will not add to consumer names near highs."},
            {"type": "heading", "text": "2026-06-27 watchlist build", "level": 3},
            {"type": "text", "text": "Seeded AMD, TSM, ASML, UBER, and CRM with entry levels and targets. All below trigger; watching only."},
            {"type": "field", "control_id": "research_prompt", "label": "Ask for a follow-up note", "value": "", "placeholder": "e.g. deep-dive AMD datacenter TAM"},
        ],
        "created_at": "2026-06-25T13:32:00Z",
        "updated_at": "2026-07-16T12:00:00Z",
    },
    "raw_scan": {
        "artifact_id": "raw_scan",
        "title": "Raw scan output",
        "data": {"note": "data-only artifact"},
        "view": None,
        "created_at": "2026-06-25T13:32:00Z",
        "updated_at": "2026-07-06T15:43:00Z",
    },
}
MEMORIES: list[dict[str, Any]] = [
    {
        "memory_id": "risk_limit",
        "content": "Single-name position cap is 15% of net liquidation; flag before any weight breaches it.",
        "updated_at": "2026-06-25T13:33:00Z",
    },
    {
        "memory_id": "mandate_horizon",
        "content": "Ideas are held on a 3 to 12 month horizon; this desk is research-only and never places orders.",
        "updated_at": "2026-06-25T13:33:00Z",
    },
    {
        "memory_id": "focus_tilt",
        "content": "As of 2026-07-06 the operator wants datacenter compute prioritised over consumer tech.",
        "updated_at": "2026-07-06T15:43:00Z",
    },
]
TOOLS: list[dict[str, Any]] = [
    {
        "tool_id": "ibkr",
        "title": "Interactive Brokers",
        "priority": "must_have",
        "status": "implemented",
        "note": "Read-only live portfolio; no order placement exists.",
        "updated_at": "2026-07-16T12:00:00Z",
    },
    {
        "tool_id": "polymarket",
        "title": "Polymarket",
        "priority": "must_have",
        "status": "implemented",
        "note": "Read-only public prediction-market data.",
        "updated_at": "2026-07-06T15:43:00Z",
    },
    {
        "tool_id": "brave_search",
        "title": "Brave Search",
        "priority": "must_have",
        "status": "enabled",
        "note": "Public-web research for theses and filings.",
        "updated_at": "2026-07-13T08:56:00Z",
    },
    {
        "tool_id": "sec_edgar",
        "title": "SEC EDGAR",
        "priority": "good_to_have",
        "status": "not_implemented",
        "note": "Pull 10-Q/10-K segment data directly instead of via web search.",
        "updated_at": "2026-07-13T08:56:00Z",
    },
]
_NEXT_MESSAGE_ID = [len(MESSAGES) + 1]
_NEXT_UPDATE_MINUTE = [10]


def _full_snapshot() -> dict[str, Any]:
    return {
        "workspace": dict(WORKSPACE),
        "messages": [dict(message) for message in MESSAGES],
        "busy": [],
        "schedules": [dict(schedule) for schedule in SCHEDULES],
        "artifacts": [
            {
                "artifact_id": artifact["artifact_id"],
                "title": artifact["title"],
                "has_view": artifact["view"] is not None,
                "data_chars": len(str(artifact["data"])),
                "updated_at": artifact["updated_at"],
            }
            for artifact in ARTIFACTS.values()
        ],
        "memories": [dict(memory) for memory in MEMORIES],
        "tools": [dict(tool) for tool in TOOLS],
    }


def _append_message(role: str, content: str, meta: dict[str, Any] | None = None) -> None:
    MESSAGES.append(
        {"id": _NEXT_MESSAGE_ID[0], "role": role, "content": content, "meta": meta, "created_at": "2026-07-16T14:30:00Z"}
    )
    _NEXT_MESSAGE_ID[0] += 1


# The mock boots unactivated like a real first open: the activation gate
# renders until the operator activates the workspace, then the canned
# workspace story becomes visible.
ACTIVATED = False
EVER_ACTIVATED = False


def _snapshot() -> dict[str, Any]:
    snapshot = _full_snapshot()
    if ACTIVATED:
        return snapshot
    workspace = dict(snapshot["workspace"])
    workspace.update({"agent_runtime": None, "model": None, "effort": None, "goal": "", "measurement": ""})
    if EVER_ACTIVATED:
        snapshot["workspace"] = workspace
        return snapshot
    empty = {key: ([] if isinstance(value, list) else value) for key, value in snapshot.items()}
    empty["workspace"] = workspace
    return empty


def route_app_api(
    method: str,
    relative: str,
    _query: dict[str, list[str]],
    body: Any,
    api_error: ApiErrorFactory,
    host_api: HostApi,
) -> dict[str, Any]:
    if method == "GET" and relative == "health":
        return {"status": "ok", "app": "alpha_seeker", "mock_backend": True}
    if method == "GET" and relative == "connections":
        return {
            "status": "ready",
            "providers": [
                {"provider": "openai", "agent_runtime": "codex", "enabled": True},
                {"provider": "claude", "agent_runtime": "claude_code", "enabled": True},
            ],
            "tools": [
                {"tool_id": "ibkr", "title": "Interactive Brokers", "priority": "must_have", "state": "ready", "detail": ""},
                {"tool_id": "polymarket", "title": "Polymarket", "priority": "must_have", "state": "ready", "detail": ""},
                {"tool_id": "brave_search", "title": "Brave Search", "priority": "good_to_have", "state": "ready", "detail": ""},
            ],
        }
    if method == "GET" and relative == "workspace":
        return _snapshot()
    if method == "GET" and relative == "session-options":
        return {
            "session_options": public_session_options(),
            "active_runtimes": ["codex", "claude_code"],
        }
    if method == "POST" and relative == "activate":
        global ACTIVATED, EVER_ACTIVATED
        if not isinstance(body, dict) or not all(
            isinstance(body.get(key), str) and body[key] for key in ("agent_runtime", "model", "effort")
        ):
            raise api_error(HTTPStatus.BAD_REQUEST, "activate requires agent_runtime, model, and effort")
        error = session_config_error(body["agent_runtime"], body["model"], body["effort"])
        if error is not None:
            raise api_error(HTTPStatus.BAD_REQUEST, error)
        if not ACTIVATED:
            for key in ("agent_runtime", "model", "effort"):
                WORKSPACE[key] = body[key]
            ACTIVATED = True
            EVER_ACTIVATED = True
        return {"activated": True}
    if method == "POST" and relative == "deactivate":
        ACTIVATED = False
        for schedule in SCHEDULES:
            schedule["enabled"] = False
        return {"activated": False, "stopping_tasks": 0}
    if method == "POST" and relative == "messages":
        if not isinstance(body, dict) or not isinstance(body.get("content"), str) or not body["content"].strip():
            raise api_error(HTTPStatus.BAD_REQUEST, "content must be a non-empty string")
        content = body["content"].strip()
        if not ACTIVATED:
            settings = ("agent_runtime", "model", "effort")
            if not all(isinstance(body.get(key), str) and body[key] for key in settings):
                raise api_error(
                    HTTPStatus.BAD_REQUEST,
                    "agent_runtime, model, and effort are required for the first message",
                )
            for key in settings:
                WORKSPACE[key] = body[key]
            # The operator's brief becomes the first message of the story.
            if MESSAGES and MESSAGES[0].get("role") == "user":
                MESSAGES[0] = {**MESSAGES[0], "content": content}
            ACTIVATED = True
            EVER_ACTIVATED = True
            return {"message_id": 1, "steered": False}
        _append_message("user", content)
        _append_message("agent", f"Mock reply: noted — {content[:60]}")
        return {"message_id": _NEXT_MESSAGE_ID[0] - 2}
    if method == "POST" and relative == "settings":
        if not isinstance(body, dict) or set(body) != {"agent_runtime", "model", "effort"}:
            raise api_error(HTTPStatus.BAD_REQUEST, "settings require agent_runtime, model, and effort")
        error = session_config_error(body["agent_runtime"], body["model"], body["effort"])
        if error is not None:
            raise api_error(HTTPStatus.BAD_REQUEST, error)
        if all(WORKSPACE[field] == body[field] for field in ("agent_runtime", "model", "effort")):
            return {**body, "thread_seq": WORKSPACE["thread_seq"], "changed": False}
        WORKSPACE.update(body)
        WORKSPACE["thread_seq"] += 1
        runtime_label = "Claude Code" if body["agent_runtime"] == "claude_code" else "Codex"
        _append_message(
            "event",
            f'Switched to {runtime_label} · {body["model"]} · {str(body["effort"]).title()}',
            {"action": "update_agent_settings"},
        )
        return {**body, "thread_seq": WORKSPACE["thread_seq"], "changed": True}
    if method == "POST" and relative == "interactions":
        if not isinstance(body, dict) or set(body) != {"artifact_id", "control_id", "value"}:
            raise api_error(HTTPStatus.BAD_REQUEST, "interaction requires artifact_id, control_id, and value")
        artifact = ARTIFACTS.get(body["artifact_id"])
        if artifact is None:
            raise api_error(HTTPStatus.NOT_FOUND, "artifact not found")
        control = next(
            (block for block in artifact.get("view") or [] if block.get("control_id") == body["control_id"]),
            None,
        )
        if control is None:
            raise api_error(HTTPStatus.CONFLICT, "control is not present in the current artifact view")
        if control["type"] == "field" and not isinstance(body["value"], str):
            raise api_error(HTTPStatus.CONFLICT, "field value must be a string")
        event = {
            "type": "artifact_interaction",
            "artifact_id": artifact["artifact_id"],
            "control_id": control["control_id"],
            "control_type": control["type"],
            "value": body["value"],
        }
        _append_message(
            "user",
            json.dumps(event, sort_keys=True, separators=(",", ":")),
            {
                "action": "artifact_interaction",
                "artifact_id": artifact["artifact_id"],
                "artifact_title": artifact["title"],
                "control_id": control["control_id"],
                "control_label": control["label"],
                "control_type": control["type"],
                "value": body["value"],
            },
        )
        _append_message("agent", f'Mock agent received "{control["label"]}".')
        return {"message_id": _NEXT_MESSAGE_ID[0] - 2, "steered": False}
    match = re.fullmatch(r"artifacts/([^/]+)", relative)
    if match:
        artifact = ARTIFACTS.get(match.group(1))
        if artifact is None:
            raise api_error(HTTPStatus.NOT_FOUND, "artifact not found")
        if method == "GET":
            return {"artifact": dict(artifact)}
        if method == "DELETE":
            del ARTIFACTS[match.group(1)]
            _append_message("event", f'Removed artifact "{artifact["title"]}"', {"action": "delete_artifact"})
            return {"deleted": match.group(1)}
    match = re.fullmatch(r"schedules/([^/]+)/(enable|disable)", relative)
    if method == "POST" and match:
        schedule_id, action = match.groups()
        for schedule in SCHEDULES:
            if schedule["schedule_id"] == schedule_id:
                schedule["enabled"] = action == "enable"
                return {"schedule_id": schedule_id, "enabled": schedule["enabled"]}
        raise api_error(HTTPStatus.NOT_FOUND, "schedule not found")
    match = re.fullmatch(r"schedules/([^/]+)", relative)
    if method == "DELETE" and match:
        schedule_id = match.group(1)
        remaining = [schedule for schedule in SCHEDULES if schedule["schedule_id"] != schedule_id]
        if len(remaining) == len(SCHEDULES):
            raise api_error(HTTPStatus.NOT_FOUND, "schedule not found")
        SCHEDULES[:] = remaining
        return {"deleted": schedule_id}
    match = re.fullmatch(r"memories/([^/]+)", relative)
    if match:
        memory_id = match.group(1)
        memory = next((entry for entry in MEMORIES if entry["memory_id"] == memory_id), None)
        if memory is None:
            raise api_error(HTTPStatus.NOT_FOUND, "memory not found")
        if method == "POST":
            if not isinstance(body, dict) or set(body) != {"content"}:
                raise api_error(HTTPStatus.BAD_REQUEST, "memory update requires content")
            content = body["content"]
            if not isinstance(content, str) or not content.strip():
                raise api_error(HTTPStatus.BAD_REQUEST, "content must be a non-empty string")
            memory["content"] = content.strip()
            memory["updated_at"] = "2026-07-16T14:30:00Z"
            _append_message("event", f'Updated memory "{memory_id}"', {"action": "update_memory"})
            return {"memory_id": memory_id}
        if method == "DELETE":
            MEMORIES.remove(memory)
            _append_message("event", f'Removed memory "{memory_id}"', {"action": "delete_memory"})
            return {"deleted": memory_id}
    match = re.fullmatch(r"tools/([^/]+)", relative)
    if method == "DELETE" and match:
        tool_id = match.group(1)
        tool = next((entry for entry in TOOLS if entry["tool_id"] == tool_id), None)
        if tool is None:
            raise api_error(HTTPStatus.NOT_FOUND, "tool not found")
        TOOLS.remove(tool)
        _append_message("event", f'Removed tool "{tool["title"]}"', {"action": "delete_tool"})
        return {"deleted": tool_id}
    match = re.fullmatch(r"tasks/([^/]+)/stop", relative)
    if method == "POST" and match:
        return {"task_id": match.group(1), "was": "running"}
    raise api_error(HTTPStatus.NOT_FOUND, "mock app route not found")


def desktop_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    expect(page.locator("#sidebar-apps")).to_contain_text("Apps")
    expect(page.locator("#sidebar-apps .sidebar-section-title > span").first).to_have_text("Apps (Beta)")
    expect(page.locator(".sidebar-section-note")).to_have_count(0)
    expect(page.locator("#beta-info-popover")).to_be_hidden()
    page.get_by_role("button", name="About beta apps").focus()
    expect(page.locator("#beta-info-popover")).to_be_visible()
    expect(page.locator("#beta-info-popover")).to_have_text(
        "These apps are under development and may not function properly."
    )
    page.locator("#app-tabs").get_by_role("button", name="Alpha Seeker", exact=True).click()
    expect(page.locator("#panel-app-alpha_seeker")).to_be_visible()
    frame = page.frame_locator('iframe[title="Alpha Seeker"]')

    # A fresh open lands on the activation gate: the app explains itself and
    # nothing exists until the operator activates the workspace.
    expect(frame.locator("#hero")).to_be_visible()
    expect(frame.locator(".hero-about-block")).to_have_count(3)
    frame.locator("#hero-send").click()
    expect(frame.locator("#workspace")).to_be_visible()

    expect(frame.locator(".app-frame-title")).to_have_text("Alpha Seeker")
    expect(frame.locator("#goal-banner")).to_contain_text("US large-cap tech")
    expect(frame.locator("#goal-banner")).to_contain_text("Mandate")
    expect(frame.locator("#agent-settings-toggle")).to_contain_text("Codex · gpt-5.6-terra · High")

    # Feed renders every role: user, agent (with inline markup), event, error.
    expect(frame.locator("#feed")).to_contain_text("refresh positions every morning")
    expect(frame.locator("#feed .msg.agent strong").first).to_have_text("positions")
    expect(frame.locator("#feed .event-chip").first).to_contain_text('Updated artifact "Portfolio"')
    expect(frame.locator("#feed .msg.error").first).to_contain_text("Action rejected")

    # The bespoke dashboard renders each named artifact's declarative view.
    positions = frame.locator('[data-panel-id="positions"]')
    expect(positions.locator(".dash-head h2")).to_have_text("Portfolio")
    expect(positions.locator(".metric-tile").first).to_contain_text("Net liquidation")
    expect(positions.locator(".b-table-wrap")).to_contain_text("NVDA")
    expect(positions.locator(".b-chart svg")).to_be_visible()
    expect(positions.locator(".dash-updated")).to_contain_text("updated")

    watchlist = frame.locator('[data-panel-id="watchlist"]')
    expect(watchlist.locator(".artifact-card").first).to_contain_text("AMD")

    markets = frame.locator('[data-panel-id="prediction_markets"]')
    expect(markets.locator(".b-table-wrap")).to_contain_text("Fed cuts in September")
    expect(markets.locator(".b-chart svg")).to_be_visible()

    research = frame.locator('[data-panel-id="research"]')
    expect(research.locator(".b-heading").first).to_contain_text("2026-07-16 pre-market")
    expect(research.locator(".b-field-control input")).to_be_visible()

    # No agent-authored HTML escapes the native renderer.
    expect(frame.locator("#dashboard-panels img")).to_have_count(0)

    # Read-only data sources are surfaced with their enablement status.
    expect(frame.locator("#tools")).to_contain_text("Interactive Brokers")
    expect(frame.locator("#tools")).to_contain_text("needs enabling")
    expect(frame.locator("#tools")).to_contain_text("Polymarket")
    expect(frame.locator("#memories")).to_contain_text("risk_limit")

    # The seeded pre-market brief is an ordinary schedule with immediate controls.
    # A second weekly review is seeded too (with a failed last run), so the
    # pause/resume interaction is scoped to the pre-market row.
    expect(frame.locator("#schedules")).to_contain_text("Pre-market brief")
    expect(frame.locator("#schedules")).to_contain_text("every day")
    expect(frame.locator("#schedules")).to_contain_text("last failed")
    brief_row = frame.locator(".rail-item.schedule", has_text="Pre-market brief")
    brief_row.get_by_role("button", name="Pause").click()
    expect(brief_row).to_contain_text("paused")
    brief_row.get_by_role("button", name="Resume").click()
    expect(brief_row).not_to_contain_text("paused")

    # A native research control sends constrained structured human input.
    research.locator(".b-field-control input").fill("deep-dive AMD datacenter TAM")
    research.locator(".b-field-control button").click()
    expect(frame.locator("#feed")).to_contain_text("deep-dive AMD datacenter TAM")

    # Data-only artifacts fall back to pretty-printed JSON in the overlay.
    frame.locator("#artifacts .rail-item", has_text="Raw scan output").click()
    overlay = frame.locator("#artifact-overlay")
    expect(overlay).to_be_visible()
    expect(overlay.locator(".artifact-json")).to_contain_text("data-only artifact")
    overlay.get_by_role("button", name="Close", exact=True).click()
    expect(overlay).to_be_hidden()

    # Sending a message round-trips through the app backend.
    frame.locator("#chat-input").fill("alpha seeker smoke message")
    frame.get_by_role("button", name="Send", exact=True).click()
    expect(frame.locator("#feed")).to_contain_text("alpha seeker smoke message")
    expect(frame.locator("#feed")).to_contain_text("Mock reply: noted")

    # The product explanation anchors to its trigger and dismisses on click-away.
    info_trigger = frame.get_by_role("button", name="How it works")
    info_trigger.click()
    info_popover = frame.locator("#info-popover")
    expect(info_popover).to_be_visible()
    expect(info_popover).to_contain_text("Read-only by design")
    frame.locator("#workspace").click(position={"x": 4, "y": 4})
    expect(info_popover).to_be_hidden()

    expect(frame.locator("#connections-pill")).to_have_text("Connections healthy")
    # Agent-authored research URLs remain inert text; the app exposes no link sink.
    expect(frame.locator("a")).to_have_count(0)
    _assert_single_scroll_desktop(page, frame, "Alpha Seeker app (desktop)")


def mobile_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    expect(page.locator("#sidebar-apps")).to_contain_text("Apps")
    page.locator("#mobile-nav-toggle").click()
    expect(page.locator("#nav-backdrop")).to_be_visible()
    page.locator("#app-tabs").get_by_role("button", name="Alpha Seeker", exact=True).click()
    expect(page.locator("#nav-backdrop")).to_be_hidden()
    expect(page.locator("#panel-app-alpha_seeker")).to_be_visible()
    frame = page.frame_locator('iframe[title="Alpha Seeker"]')
    expect(frame.locator("#feed")).to_contain_text("refresh positions every morning")
    expect(frame.locator('[data-panel-id="positions"] .b-chart svg')).to_be_visible()
    _assert_frame_no_horizontal_overflow(frame, "Alpha Seeker app")
    _assert_outer_page_locked(page, "Alpha Seeker app (mobile)")
    page.once("dialog", lambda dialog: dialog.accept())
    frame.locator("#deactivate-app").click()
    expect(frame.locator("#hero")).to_be_visible()
    expect(frame.locator("#workspace")).to_be_hidden()
    expect(frame.locator("#hero-send")).to_contain_text("Reactivate")
    expect(frame.locator("#hero-hint")).to_contain_text("schedules stay paused")


def _assert_frame_no_horizontal_overflow(frame: Any, label: str) -> None:
    overflow = frame.locator("html").evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    if overflow > 1:
        raise AssertionError(f"{label} frame overflows horizontally by {overflow}px")


def _assert_single_scroll_desktop(page: Any, frame: Any, label: str) -> None:
    """With the app tab open on a wide viewport, neither the outer page nor
    the iframe document may scroll vertically -- only the app's internal
    panes do. Guards the nested page + iframe double-scrollbar regression."""
    outer = page.evaluate(
        "() => document.documentElement.scrollHeight - document.documentElement.clientHeight"
    )
    if outer > 1:
        raise AssertionError(f"{label}: outer page scrolls vertically by {outer}px with an app tab open")
    inner = frame.locator("html").evaluate(
        "() => document.documentElement.scrollHeight - document.documentElement.clientHeight"
    )
    if inner > 1:
        raise AssertionError(f"{label}: app iframe document scrolls vertically by {inner}px")


def _assert_outer_page_locked(page: Any, label: str) -> None:
    """On a phone the app document may scroll, but the host page must stay
    locked so there is only one scrollbar."""
    outer = page.evaluate(
        "() => document.documentElement.scrollHeight - document.documentElement.clientHeight"
    )
    if outer > 1:
        raise AssertionError(f"{label}: outer page scrolls vertically by {outer}px with an app tab open")
