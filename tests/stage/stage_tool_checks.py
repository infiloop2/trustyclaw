"""Credential preflight and deterministic live scenarios for bundled tools."""

from __future__ import annotations

import json
import os
import shlex
import time
from typing import TYPE_CHECKING

from host.runtime.tools.tools_host import BUNDLED_TOOLS
from tests.stage.stage_support import (
    CHEAP_EFFORT,
    CHEAP_MODELS,
    RUNTIME_LABELS,
    CredentialUnavailable,
    agent_catalog_tool_ids,
    diagnostic_ref,
)


_SENSITIVE_ARGUMENT_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "bearer_token",
        "credential",
        "password",
        "private_key",
        "secret",
    }
)


def _safe_arguments(value: object, key: str = "") -> object:
    """Bound diagnostic arguments and redact anything credential-shaped."""
    normalized_key = key.lower()
    if normalized_key == "approval_id":
        return f"<ref:{diagnostic_ref(value)}>"
    if normalized_key in _SENSITIVE_ARGUMENT_KEYS or normalized_key.endswith(
        ("_api_key", "_access_token", "_bearer_token", "_password", "_secret")
    ):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(item_key): _safe_arguments(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_safe_arguments(item, key) for item in value[:20]]
    if isinstance(value, str) and len(value.encode("utf-8")) > 160:
        return value.encode("utf-8")[:160].decode("utf-8", errors="ignore") + "…"
    return value


def _result_shape(value: dict) -> str:
    """Describe an MCP result without printing provider-returned values."""
    fields: list[str] = []
    for key, item in sorted(value.items()):
        if isinstance(item, list):
            fields.append(f"{key}[{len(item)}]")
        elif isinstance(item, dict):
            fields.append(f"{key}{{{len(item)}}}")
        else:
            fields.append(str(key))
    return ",".join(fields)


class StageToolChecks:
    """Mixin supplying tool checks to the persistent stage harness."""

    if TYPE_CHECKING:
        def _api(self, method: str, path: str, body: dict | None = None) -> dict: ...
        def _ssh_code(self, command: str) -> str: ...
        def _step(self, label: str) -> None: ...
        def _ok(self, detail: str) -> None: ...
        def _wait_for_task(self, task_id: str, timeout: int = 0) -> dict: ...
        def _task_failure_detail(self, task_id: str) -> str: ...
        def task_body(
            self,
            input_message: str,
            thread_id: str,
            *,
            runtime: str | None = None,
            model: str | None = None,
            effort: str | None = None,
        ) -> dict: ...

    def _tool_credential_failures(self, tool_id: str) -> list[str]:
        """Return every missing prerequisite for one bundled tool."""
        tools = {
            entry["tool_id"]: entry
            for entry in self._api("GET", "/v1/tools")["tools"]
        }
        entry = tools.get(tool_id) or {}
        manifest = BUNDLED_TOOLS[tool_id].manifest
        missing_config = [
            item["key"] for item in entry.get("config", []) if not item.get("set")
        ]
        connected = (entry.get("connection_status") or {}).get("connected") is True
        if entry.get("enabled") and not missing_config and (
            manifest.connection != "oauth" or connected
        ):
            state = "connected" if manifest.connection == "oauth" else "configured"
            print(f"  [credential ok] {tool_id}: enabled and {state}", flush=True)
            return []
        problems: list[str] = []
        if missing_config:
            if manifest.connection == "oauth":
                problems.append(
                    "set its OAuth app configuration once in the stage admin UI "
                    f"(missing: {', '.join(missing_config)})"
                )
            else:
                env_names = [f"TRUSTYCLAW_STAGE_{key}" for key in missing_config]
                problems.append(f"set config via {', '.join(env_names)} or the admin UI")
        if manifest.connection == "oauth" and not connected:
            problems.append("connect its stage account once in the admin UI")
        if not entry.get("enabled"):
            problems.append("enable the tool")
        return [f"{tool_id}: {'; '.join(problems)}"]

    def autoconfigure_tools(self, tool_ids: tuple[str, ...]) -> None:
        """Apply enable-only provider config from Actions secrets when present."""
        for tool_id in tool_ids:
            manifest = BUNDLED_TOOLS[tool_id].manifest
            if manifest.connection == "oauth":
                continue
            for requirement in manifest.config:
                value = os.environ.get(f"TRUSTYCLAW_STAGE_{requirement.key}", "")
                if value:
                    self._api(
                        "PUT",
                        f"/v1/tools/{tool_id}/config",
                        {"key": requirement.key, "value": value},
                    )
        listing = {
            entry["tool_id"]: entry
            for entry in self._api("GET", "/v1/tools")["tools"]
        }
        for tool_id in tool_ids:
            if BUNDLED_TOOLS[tool_id].manifest.connection == "oauth":
                continue
            entry = listing[tool_id]
            if all(item.get("set") for item in entry.get("config", [])):
                self._api("POST", f"/v1/tools/{tool_id}/enable", {})

    def _shim_call(self, request: dict) -> dict:
        """Run one JSON-RPC request through the real agent-side MCP shim."""
        line = json.dumps(request)
        output = self._ssh_code(
            f"printf '%s\\n' {shlex.quote(line)} | "
            "sudo -u trustyclaw-agent env PYTHONPATH=/opt/trustyclaw-host "
            "python3 -m host.runtime.agent_shim.mcp_shim"
        )
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"tools shim returned non-JSON: {output!r}") from exc

    def _shim_tool_response(self, name: str, arguments: dict) -> tuple[dict, str]:
        """Call one MCP action and print bounded, secret-free diagnostics."""
        safe = json.dumps(_safe_arguments(arguments), sort_keys=True, separators=(",", ":"))
        started = time.monotonic()
        print(f"    [mcp call] {name} arguments={safe}", flush=True)
        try:
            response = self._shim_call(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
            result = response.get("result") or {}
            text = str((result.get("content") or [{}])[0].get("text", ""))
        except Exception as exc:
            elapsed = time.monotonic() - started
            print(
                f"    [mcp transport failed] {name} after {elapsed:.1f}s: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            raise
        elapsed = time.monotonic() - started
        rpc_outcome = "error" if result.get("isError") else "success"
        print(f"    [mcp {rpc_outcome}] {name} in {elapsed:.1f}s", flush=True)
        return result, text

    def _shim_tool_result(self, name: str, arguments: dict) -> dict:
        result, text = self._shim_tool_response(name, arguments)
        if result.get("isError"):
            lower = text.lower()
            if any(
                marker in lower
                for marker in (
                    "reconnect",
                    "not connected",
                    "rejected the configured api key",
                    "rejected the configured bearer token",
                    "rejected the request as unauthorized",
                )
            ):
                raise CredentialUnavailable(f"{name}: {text}")
            raise AssertionError(f"{name} failed: {text}")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{name} returned non-JSON result text: {text!r}") from exc
        if not isinstance(parsed, dict):
            raise AssertionError(f"{name} returned a non-object result: {type(parsed).__name__}")
        print(
            f"    [mcp result] {name} status={parsed.get('status')!r} "
            f"payload_shape={_result_shape(parsed)}",
            flush=True,
        )
        return parsed

    def check_agent_mcp_catalog(self, runtime: str) -> None:
        """Prove one provider session can invoke the host's MCP catalog."""
        self._step(f"{RUNTIME_LABELS[runtime]} agent MCP catalog")
        prompt = (
            "Call the MCP tool list_bundled_tools exactly once. From its result, reply with exactly "
            'one JSON object shaped {"tools":["tool_id",...]}. Include every bundled tool_id, '
            "including disabled tools, exactly once in sorted order. Do not include display names, "
            "action names, Markdown, or commentary."
        )
        task = self._api(
            "POST",
            "/v1/tasks",
            self.task_body(prompt, f"mcp-catalog-{runtime}-{time.time_ns()}", runtime=runtime),
        )
        expected_session = (CHEAP_MODELS[runtime], CHEAP_EFFORT)
        if (task.get("model"), task.get("effort")) != expected_session:
            raise AssertionError(f"agent MCP catalog task did not use {expected_session}: {task}")
        print(
            f"    [agent task] runtime={runtime} task_id={task['task_id']} "
            f"model={task.get('model')} effort={task.get('effort')}",
            flush=True,
        )
        done = self._wait_for_task(task["task_id"], timeout=240)
        if done.get("status") != "completed":
            raise AssertionError(
                f"agent MCP catalog task failed: {self._task_failure_detail(task['task_id'])}"
            )
        actual = agent_catalog_tool_ids(done.get("output_message") or "")
        expected = tuple(sorted(BUNDLED_TOOLS))
        if actual != expected:
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            raise AssertionError(
                f"agent MCP catalog mismatch: missing={missing}, extra={extra}, actual={actual}"
            )
        self._ok(f"agent returned all {len(expected)} bundled tool ids through MCP")

    def check_tool_live(self, tool_id: str) -> None:
        """Exercise one tool deeply through the real agent-side MCP shim."""
        self._step(f"{tool_id}: deterministic live MCP path")
        before = self._latest_tool_event_seq()
        try:
            detail = self._check_tool_provider(tool_id)
        except CredentialUnavailable:
            self._ok("live probe found an expired or rejected credential; integration unavailable")
            raise
        events = self._api("GET", "/v1/tools/events?limit=100")["events"]
        tool_events = [
            event
            for event in events
            if event.get("tool_id") == tool_id and event.get("seq", 0) > before
        ]
        if not tool_events:
            raise AssertionError(f"{tool_id} live checks produced no tool audit events")
        seqs = [event["seq"] for event in events]
        if seqs != sorted(seqs, reverse=True):
            raise AssertionError(f"tool events are not newest-first: {seqs}")
        audited = [
            f"{event.get('action_id')}:{event.get('outcome')}" for event in tool_events
        ]
        print(f"    [audit events] {tool_id}: {', '.join(audited)}", flush=True)
        self._ok(f"{detail}; {len(tool_events)} audited action/decision events")

    def _check_tool_provider(self, tool_id: str) -> str:
        specialized = {
            "brave_search": self._check_brave_live,
            "gmail": self._check_gmail_live,
            "google_calendar": self._check_calendar_live,
            "instagram_discovery": self._check_instagram_discovery_live,
            "polymarket": self._check_polymarket_live,
            "twitter": self._check_twitter_live,
            "runway": self._check_runway_live,
        }.get(tool_id)
        if specialized is not None:
            return specialized()

        if tool_id == "ibkr":
            # IBKR reads require an explicit account_id; discover it live first.
            accounts_result = self._successful_tool_call("ibkr_get_accounts", {})
            rows = accounts_result.get("accounts")
            first = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
            account_id = first.get("account_id")
            if not isinstance(account_id, str) or not account_id:
                raise AssertionError(f"ibkr get_accounts returned no account id: {accounts_result}")
            calls = (
                ("get_positions", {"account_id": account_id}),
                ("get_account_summary", {"account_id": account_id}),
                ("get_trades", {"account_id": account_id, "days": "1"}),
            )
        else:
            calls = {
                "instagram": (
                    ("get_profile", {}),
                    ("get_recent_media", {"limit": "1"}),
                    ("get_publishing_limit", {}),
                ),
                "linkedin": (("get_profile", {}),),
                "linkedin_discovery": (
                    ("search_posts", {"query": "TrustyClaw", "limit": "1"}),
                ),
            }[tool_id]
        for action_id, arguments in calls:
            self._successful_tool_call(f"{tool_id}_{action_id}", arguments)
        if tool_id == "linkedin":
            self._queue_and_deny(
                "linkedin",
                "linkedin_create_post",
                {"text": f"TrustyClaw stage proposal {os.urandom(3).hex()}"},
            )
        suffix = "; publish proposal denied" if tool_id == "linkedin" else ""
        read_count = len(calls) + (1 if tool_id == "ibkr" else 0)
        return f"{read_count} live read(s) completed{suffix}"

    def _check_brave_live(self) -> str:
        name = "brave_search_search_web"
        arguments = {"query": "TrustyClaw agent host"}
        for attempt in range(2):
            try:
                self._successful_tool_call(name, arguments)
                return "live search completed"
            except AssertionError as exc:
                if attempt or "Brave Search API returned HTTP 5" not in str(exc):
                    raise
                print("    [provider retry] brave_search HTTP 5xx; retrying once", flush=True)
        raise AssertionError("unreachable")

    def _check_gmail_live(self) -> str:
        unique_title = f"TrustyClaw stage check {os.urandom(4).hex()}"
        messages = self._successful_tool_call("gmail_search_messages", {"query": "in:anywhere"})
        labels = self._successful_tool_call("gmail_list_labels", {})
        self._successful_tool_call("gmail_list_drafts", {})
        rows = messages.get("messages") if isinstance(messages.get("messages"), list) else []
        if rows and isinstance(rows[0], dict):
            message_id = rows[0].get("id")
            thread_id = rows[0].get("threadId")
            if isinstance(message_id, str) and message_id:
                self._successful_tool_call("gmail_read_message", {"message_id": message_id})
                self._queue_and_deny(
                    "gmail",
                    "gmail_message_action",
                    {"action": "mark_read", "message_ids": [message_id]},
                )
            if isinstance(thread_id, str) and thread_id:
                self._successful_tool_call("gmail_read_thread", {"thread_id": thread_id})
        self._queue_and_deny(
            "gmail",
            "gmail_send_email",
            {
                "to": "stage@example.com",
                "subject": unique_title,
                "blocks": [{"type": "paragraph", "text": "Stage proposal; never sent."}],
            },
        )
        self._queue_and_deny(
            "gmail",
            "gmail_label_action",
            {"action": "create", "name": f"trustyclaw-stage-{os.urandom(3).hex()}"},
        )
        pending = self._shim_tool_result(
            "gmail_draft_action",
            {
                "action": "create",
                "to": "stage@example.com",
                "subject": unique_title,
                "blocks": [
                    {"type": "paragraph", "text": "Stage draft round trip; safe to ignore."}
                ],
            },
        )
        approval_id = pending.get("approval_id")
        if not isinstance(approval_id, str):
            raise AssertionError(f"gmail draft create did not queue approval: {pending}")
        created = self._approval_decision("gmail", approval_id, "approve")
        if created["approval"]["status"] != "executed":
            raise AssertionError(f"approved gmail draft create did not execute: {created}")
        draft_id = self._created_id_from_message(created.get("result"))
        if not draft_id:
            raise AssertionError(f"approved gmail draft create did not report a draft id: {created}")
        cleanup = self._shim_tool_result(
            "gmail_draft_action", {"action": "delete", "draft_id": draft_id}
        )
        cleanup_id = cleanup.get("approval_id")
        if not isinstance(cleanup_id, str):
            raise AssertionError(f"gmail draft delete did not queue approval: {cleanup}")
        decision = self._approval_decision("gmail", cleanup_id, "approve")
        if decision["approval"]["status"] != "executed":
            raise AssertionError(f"approved gmail draft delete did not execute: {decision}")
        return (
            f"mail search/read surfaces, {len(labels.get('labels', []))} labels, "
            "proposals denied, draft created and deleted"
        )

    def _check_calendar_live(self) -> str:
        self._successful_tool_call("google_calendar_read_events", {})
        unique_title = f"TrustyClaw stage check {os.urandom(4).hex()}"
        start = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 86400))
        end = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 90000))
        pending = self._shim_tool_result(
            "google_calendar_event_change",
            {"operation": "create", "summary": unique_title, "start_time": start, "end_time": end},
        )
        approval_id = pending.get("approval_id")
        if not isinstance(approval_id, str):
            raise AssertionError(f"calendar create did not queue approval: {pending}")
        decision = self._approval_decision("google_calendar", approval_id, "approve")
        if decision["approval"]["status"] != "executed":
            raise AssertionError(f"approved calendar create did not execute: {decision}")
        event_id = self._created_id_from_message(decision.get("result"))
        if not event_id:
            raise AssertionError(f"approved calendar create did not report an event id: {decision}")
        checked = self._successful_tool_call("check_tool_approval", {"approval_id": approval_id})
        if checked.get("approval_status") != "executed":
            raise AssertionError(f"check_tool_approval disagreed: {checked}")
        cleanup = self._shim_tool_result(
            "google_calendar_event_change", {"operation": "delete", "event_id": event_id}
        )
        cleanup_id = cleanup.get("approval_id")
        if not isinstance(cleanup_id, str):
            raise AssertionError(f"calendar delete did not queue approval: {cleanup}")
        cleanup_decision = self._approval_decision("google_calendar", cleanup_id, "approve")
        if cleanup_decision["approval"]["status"] != "executed":
            raise AssertionError(f"approved calendar delete did not execute: {cleanup_decision}")
        return "event read plus approval round trip created and deleted one event"

    def _check_instagram_discovery_live(self) -> str:
        # Prefer the current trending feed for dependent reads. Keyword search
        # can legitimately return an old Reel that has since become private or
        # disappeared even though the search record still exists.
        trending = self._successful_tool_call(
            "instagram_discovery_get_trending_reels", {"limit": "1"}
        )
        results = [
            trending,
            self._successful_tool_call(
                "instagram_discovery_search_reels", {"query": "TrustyClaw", "limit": "1"}
            ),
            self._successful_tool_call(
                "instagram_discovery_search_hashtag", {"hashtag": "ai", "limit": "1"}
            ),
        ]
        reels: list[dict] = []
        for result in results:
            raw_reels = result.get("reels")
            if isinstance(raw_reels, list):
                reels.extend(reel for reel in raw_reels if isinstance(reel, dict))
        audio = next(
            (reel.get("audio_id") for reel in reels if str(reel.get("audio_id") or "").isdigit()),
            None,
        )
        url = next(
            (
                reel.get("url")
                for reel in reels
                if isinstance(reel.get("url"), str) and reel.get("url")
            ),
            None,
        )
        derived = 0
        if isinstance(audio, str):
            self._successful_tool_call(
                "instagram_discovery_get_reels_by_audio", {"audio_id": audio, "limit": "1"}
            )
            derived += 1
        if isinstance(url, str):
            self._successful_tool_call("instagram_discovery_get_reel_details", {"url": url})
            derived += 1
        print(f"    [derived coverage] instagram_discovery actions={derived}/2", flush=True)
        return f"three one-credit discovery reads plus {derived} provider-result-derived read(s)"

    def _check_polymarket_live(self) -> str:
        listing = self._successful_tool_call("polymarket_list_markets", {"limit": "10"})
        self._successful_tool_call("polymarket_list_events", {"limit": "1"})
        self._successful_tool_call(
            "polymarket_search", {"query": "bitcoin", "limit_per_type": "1"}
        )
        raw_markets = listing.get("markets")
        markets = raw_markets if isinstance(raw_markets, list) else []
        market = next(
            (
                item
                for item in markets
                if isinstance(item, dict) and item.get("id") and item.get("clob_token_ids")
            ),
            None,
        )
        if not isinstance(market, dict):
            raise AssertionError(
                f"Polymarket returned no active market usable by dependent reads; "
                f"market_count={len(markets)}"
            )
        try:
            token_ids = json.loads(str(market["clob_token_ids"]))
        except json.JSONDecodeError as exc:
            raise AssertionError("Polymarket returned invalid token ids") from exc
        token_id = next(
            (value for value in token_ids if isinstance(value, str) and value.isdigit()),
            None,
        )
        if not isinstance(token_id, str):
            raise AssertionError("Polymarket returned no decimal outcome token id")
        self._successful_tool_call("polymarket_get_market", {"market_id": market["id"]})
        self._successful_tool_call("polymarket_get_order_book", {"token_id": token_id})
        self._successful_tool_call(
            "polymarket_price_history", {"token_id": token_id, "interval": "1d"}
        )
        return "all six public market, event, search, book, and price actions completed"

    def _check_twitter_live(self) -> str:
        search = self._successful_tool_call(
            "twitter_search_tweets", {"query": "TrustyClaw", "max_results": "10"}
        )
        self._successful_tool_call("twitter_get_trends", {"max_trends": "1"})
        self._successful_tool_call("twitter_get_personalized_trends", {})
        raw_tweets = search.get("tweets")
        tweets = raw_tweets if isinstance(raw_tweets, list) else []
        first = next((tweet for tweet in tweets if isinstance(tweet, dict)), None)
        derived = 0
        if isinstance(first, dict) and isinstance(first.get("id"), str):
            self._successful_tool_call("twitter_read_tweet", {"tweet_id": first["id"]})
            derived += 1
            author_id = first.get("author_id")
            if isinstance(author_id, str) and author_id:
                self._successful_tool_call(
                    "twitter_user_tweets", {"user_id": author_id, "max_results": "5"}
                )
                derived += 1
        print(f"    [derived coverage] twitter actions={derived}/2", flush=True)
        self._queue_and_deny(
            "twitter",
            "twitter_post_tweet",
            {"text": f"TrustyClaw stage proposal {os.urandom(3).hex()}"},
        )
        return (
            f"search, global/personal trends, {derived} result-derived read(s), "
            "publish proposal denied"
        )

    def _check_runway_live(self) -> str:
        name = "runway_get_task"
        result, text = self._shim_tool_response(
            name, {"task_id": "00000000-0000-4000-8000-000000000000"}
        )
        if "rejected the configured api key" in text.lower():
            raise CredentialUnavailable(f"{name}: {text}")
        if not result.get("isError") or "not found" not in text.lower():
            raise AssertionError(
                f"Runway authenticated task probe was unexpected: "
                f"isError={result.get('isError')}, message={text}"
            )
        return "authenticated missing-task probe completed without generation spend"

    def _successful_tool_call(self, name: str, arguments: dict) -> dict:
        result = self._shim_tool_result(name, arguments)
        if result.get("status") != "success_executed" and name != "check_tool_approval":
            raise AssertionError(
                f"{name} returned invalid status {result.get('status')!r}; "
                f"keys={sorted(result)}"
            )
        return result

    def _approval_decision(self, tool_id: str, approval_id: str, decision: str) -> dict:
        started = time.monotonic()
        print(
            f"    [approval {decision}] tool={tool_id} "
            f"approval_ref={diagnostic_ref(approval_id)}",
            flush=True,
        )
        response = self._api(
            "POST", f"/v1/tools/{tool_id}/approvals/{approval_id}/{decision}", {}
        )
        elapsed = time.monotonic() - started
        status = (response.get("approval") or {}).get("status")
        print(
            f"    [approval result] tool={tool_id} decision={decision} "
            f"status={status!r} elapsed={elapsed:.1f}s",
            flush=True,
        )
        return response

    def _queue_and_deny(self, tool_id: str, name: str, arguments: dict) -> None:
        pending = self._shim_tool_result(name, arguments)
        approval_id = pending.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            raise AssertionError(f"{name} did not queue an approval: {pending}")
        decision = self._approval_decision(tool_id, approval_id, "deny")
        if decision["approval"]["status"] != "denied":
            raise AssertionError(f"{name} approval was not denied: {decision}")

    def _latest_tool_event_seq(self) -> int:
        events = self._api("GET", "/v1/tools/events?limit=1")["events"]
        return int(events[0]["seq"]) if events else 0

    @staticmethod
    def _created_id_from_message(result: object) -> str:
        message = result.get("message") if isinstance(result, dict) else None
        if not isinstance(message, str) or not message.strip():
            return ""
        return message.strip().rstrip(".").split()[-1]
