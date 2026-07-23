#!/usr/bin/env python3
"""Serve the admin UI against a deterministic local mock backend.

This is for browser/UI development only. It does not import the real admin API
handler because the real handler reads host state and invokes privileged helper
paths. The mock keeps just enough in-memory state to exercise the single-page
admin UI at ``host/runtime/admin_api/admin_ui.html``, and ships with seeded history plus
time-based task progression so the UI looks and behaves like a live host.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import app_mocks
from host.config import ConfigError, parse_network_controls
from host.constants import LOOPBACK
from host.session_options import session_config_error
from host.runtime.core import app_platform
from host.runtime.tools.tools_host import BUNDLED_TOOLS

RUNTIME_DIR = REPO_ROOT / "host/runtime/admin_api"
TOOLS_DIR = REPO_ROOT / "host/tools"
VERSION = (REPO_ROOT / "VERSION").read_text().strip()
UI_ASSETS = {
    "/": (RUNTIME_DIR / "admin_ui.html", "text/html; charset=utf-8"),
    "/oauth/callback": (RUNTIME_DIR / "admin_ui.html", "text/html; charset=utf-8"),
    "/admin_ui.css": (RUNTIME_DIR / "admin_ui.css", "text/css; charset=utf-8"),
    "/favicon.ico": (RUNTIME_DIR / "admin_favicon.svg", "image/svg+xml"),
    "/favicon.svg": (RUNTIME_DIR / "admin_favicon.svg", "image/svg+xml"),
    "/workspace-kit/view_blocks.css": (
        app_platform.APP_ROOT / "workspace_kit" / "ui" / "view_blocks.css",
        "text/css; charset=utf-8",
    ),
    "/workspace-kit/view_blocks.js": (
        app_platform.APP_ROOT / "workspace_kit" / "ui" / "view_blocks.js",
        "application/javascript; charset=utf-8",
    ),
}
UI_ASSETS.update({
    f"/admin_ui/{module.name}": (module, "application/javascript; charset=utf-8")
    for module in sorted((RUNTIME_DIR / "admin_ui").glob("*.js"))
})
for asset in sorted(TOOLS_DIR.glob("**/guide_assets/**/*.png")):
    route = f"/guide-assets/{asset.name}"
    if route in UI_ASSETS:
        raise RuntimeError(f"duplicate tool guide asset filename: {asset.name}")
    UI_ASSETS[route] = (asset, "image/png")
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "base-uri 'none'; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data:; "
        "media-src blob:; "
        "object-src 'none'; "
        "script-src 'self'; "
        "style-src 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
PASSWORD = "dev"
FAILED_UPLOADS_ONCE: set[str] = set()
TASK_RE = re.compile(r"^/v1/tasks/([^/]+)(?:/(steer|cancel|kill|events))?$")
THREAD_TASKS_RE = re.compile(r"^/v1/threads/([^/]+)/tasks$")
THREAD_EVENTS_RE = re.compile(r"^/v1/threads/([^/]+)/events$")
TOOL_ACTION_RE = re.compile(r"^/v1/tools/([a-z0-9_]+)/(enable|disable|oauth_connect/start|oauth_connect/complete|oauth_connect/disconnect)$")
GITHUB_PENDING_PUSH_RE = re.compile(r"^/v1/network-tools/github-pending-pushes/([a-z0-9]+)/(approve|reject)$")
TOOL_APPROVALS_LIST_RE = re.compile(r"^/v1/tools/([a-z0-9_]+)/approvals$")
TOOL_APPROVAL_RE = re.compile(r"^/v1/tools/([a-z0-9_]+)/approvals/([^/]+)/(approve|deny)$")
TOOL_APPROVAL_GET_RE = re.compile(r"^/v1/tools/([a-z0-9_]+)/approvals/([^/]+)$")
TOOL_CONFIG_RE = re.compile(r"^/v1/tools/([a-z0-9_]+)/config$")
TOOL_EVENT_RE = re.compile(r"^/v1/tools/events/([1-9][0-9]*)$")
MOCK_OAUTH_CODE = "mock-auth-code"
RUNTIMES = ("codex", "claude_code", "hermes")
PROVIDER_BY_RUNTIME = {"codex": "openai", "claude_code": "claude", "hermes": "bedrock"}
MAX_RUNNING_PER_RUNTIME = 3
MAX_RUNNING_TOTAL = 6

# Timed progression script for running tasks: (fraction of duration, message).
PROGRESS_SCRIPT = [
    (0.2, "Reading the workspace and planning the change."),
    (0.55, "Applying edits and running the relevant checks."),
    (0.85, "Checks passed; writing up the result."),
]
# Provider traffic emitted alongside progress milestones, keyed by runtime.
PROVIDER_TRAFFIC = {
    "codex": ("api.openai.com", "/v1/responses"),
    "claude_code": ("api.anthropic.com", "/v1/messages"),
    "hermes": ("bedrock-runtime.us-east-1.amazonaws.com", "/model/qwen.qwen3-coder-next/converse-stream"),
}


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class MockState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_task_number: int = 1
    next_agent_event_seq: int = 1
    next_network_event_seq: int = 1
    next_tool_event_seq: int = 1
    agent_events: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    task_events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    network_events: list[dict[str, Any]] = field(default_factory=list)
    policy: dict[str, Any] = field(
        default_factory=lambda: {"network_integrations": {}}
    )
    logged_in: dict[str, bool] = field(default_factory=lambda: {"codex": False, "claude_code": False})
    github_credential: dict[str, Any] | None = None
    codex_oauth: dict[str, str] = field(default_factory=dict)
    claude_oauth: dict[str, str] = field(default_factory=dict)
    bedrock_access_key_id: str | None = None
    bedrock_region: str | None = None
    reboot_requested: bool = False
    upgrade_available: bool = True
    usage_refreshes: int = 0
    github_pending_pushes: list[dict[str, Any]] = field(default_factory=list)
    tool_enabled: set[str] = field(default_factory=set)
    # Config is scoped per tool: tool_id -> set of configured keys.
    tool_config: dict[str, set[str]] = field(default_factory=dict)
    tool_connections: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_approvals: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    next_approval_number: int = 1

    def add_tool_approval(
        self, tool_id: str, action: str, summary: str, payload: dict[str, Any], status: str = "pending",
        created_at: int | None = None, result: str = "",
    ) -> dict[str, Any]:
        approval = {
            "approval_id": f"approval_{self.next_approval_number}",
            "tool_id": tool_id,
            "action_id": action,
            "status": status,
            "summary": summary,
            "payload": payload,
            # Terminal outcome text (the executed action's message or the
            # failure error); empty until executed/failed, like the real API.
            "result": result,
            "created_at": created_at if created_at is not None else int(time.time()),
            "decided_at": 0 if status == "pending" else int(time.time()),
        }
        self.next_approval_number += 1
        self.tool_approvals.append(approval)
        return approval

    def now(self) -> str:
        return iso(datetime.now(timezone.utc))

    def add_agent_event(
        self, event_type: str, task_id: str | None, payload: dict[str, Any], timestamp: str | None = None
    ) -> None:
        event = {
            "seq": self.next_agent_event_seq,
            "timestamp": timestamp or self.now(),
            "event_type": event_type,
            "task_id": task_id,
            "payload": payload,
        }
        self.next_agent_event_seq += 1
        self.agent_events.append(event)
        if task_id:
            self.task_events.setdefault(task_id, []).append(event)

    def add_network_event(self, method: str, host: str, path: str, decision: str, timestamp: str | None = None) -> None:
        parsed = urlparse(path)
        event = {
            "seq": self.next_network_event_seq,
            "timestamp": timestamp or self.now(),
            "method": method,
            "protocol": "https",
            "host": host,
            "port": 443,
            "path": parsed.path or "/",
            "query": parsed.query,
            "decision": decision,
        }
        if decision == "denied":
            event["reason_code"] = "host_not_allowed"
        self.network_events.append(event)
        self.next_network_event_seq += 1

    def add_tool_event(
        self,
        tool_id: str,
        action: str,
        outcome: str,
        detail: str = "",
        timestamp: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        self.tool_events.append({
            "seq": self.next_tool_event_seq,
            "timestamp": timestamp or self.now(),
            "tool_id": tool_id,
            "action_id": action,
            "outcome": outcome,
            "detail": detail,
            "arguments": arguments,
        })
        self.next_tool_event_seq += 1

    def public_task(self, task: dict[str, Any], queue_position: int | None = None) -> dict[str, Any]:
        result = {key: value for key, value in task.items() if not key.startswith("_")}
        if queue_position is not None:
            result["queue_position"] = queue_position
        return result

    def runtime_status(self, runtime: str) -> str:
        provider = PROVIDER_BY_RUNTIME[runtime]
        managed = self.policy.get("network_integrations", {})
        integration = managed.get(provider) if isinstance(managed, dict) else None
        if not isinstance(integration, dict) or integration.get("enabled") is not True:
            return "deactivated"
        if runtime == "hermes":
            return "active" if self.bedrock_access_key_id else "awaiting_login"
        if self.logged_in.get(runtime):
            return "active"
        return "awaiting_login"


STATE = MockState()


def iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ago(minutes: float) -> str:
    return iso(datetime.now(timezone.utc) - timedelta(minutes=minutes))


def seed_state() -> None:
    """Populate history that resembles a host that has been in use for a while.

    The seeded story: the operator ran a few threads earlier today, one deploy
    task failed against the network policy, and provider access was later
    switched off, which is why every runtime starts out deactivated.
    """
    seed_tasks = [
        {
            "task_id": "task_1",
            "thread_id": "main",
            "agent_runtime": "codex",
            "status": "completed",
            "input_message": "Review the failing deploy workflow in acme/infra and summarize the root cause.",
            "output_message": (
                "Root cause: the deploy job pins actions/setup-node to a yanked version, so the runner "
                "falls back to Node 16 and the build script fails on `Array.prototype.toSorted`.\n"
                "Recommend pinning setup-node to the current v4 commit SHA and adding a node --version guard."
            ),
            "created_min": 205,
            "started_min": 204,
            "completed_min": 197,
        },
        {
            "task_id": "task_2",
            "thread_id": "main",
            "agent_runtime": "codex",
            "status": "completed",
            "input_message": "Apply that fix on a branch and open a PR with the workflow change.",
            "output_message": (
                "Opened acme/infra#128 pinning actions/setup-node to the v4 commit SHA with a version guard "
                "step. CI is green on the branch; requested review from @platform-team."
            ),
            "created_min": 191,
            "started_min": 190,
            "completed_min": 178,
        },
        {
            "task_id": "task_3",
            "thread_id": "website-redesign",
            "agent_runtime": "claude_code",
            "status": "completed",
            "input_message": "Audit the marketing site for mobile layout issues and list concrete fixes.",
            "output_message": (
                "Found 6 issues: hero overflows at <390px, nav does not collapse, pricing table needs "
                "horizontal scroll, two tap targets under 40px, CLS from unsized images, and a fixed "
                "footer covering CTAs. Wrote fixes to workspace/acme-web/notes in priority order."
            ),
            "created_min": 96,
            "started_min": 95,
            "completed_min": 84,
        },
        {
            "task_id": "task_4",
            "thread_id": "website-redesign",
            "agent_runtime": "claude_code",
            "status": "failed",
            "input_message": "Push the responsive fixes to staging and verify the deploy.",
            "error_message": "network access to deploy.acme.dev denied by policy (no custom domain rule)",
            "created_min": 81,
            "started_min": 80,
            "completed_min": 78,
        },
        {
            "task_id": "task_5",
            "thread_id": "dependency-audit",
            "agent_runtime": "codex",
            "status": "cancelled",
            "input_message": "Upgrade all npm dependencies in acme-web and note any breaking changes.",
            "created_min": 1510,
            "started_min": None,
            "completed_min": 1490,
        },
        {
            "task_id": "task_6",
            "thread_id": "incident-response",
            "agent_runtime": "codex",
            "status": "running",
            "input_message": "Investigate the production alert and draft the mitigation plan.",
            "created_min": 18,
            "started_min": 17,
            "completed_min": 16,
        },
        {
            "task_id": "task_7",
            "thread_id": "docs-cleanup",
            "agent_runtime": "claude_code",
            "status": "queued",
            "input_message": "Rewrite the onboarding notes to match the current deploy flow.",
            "created_min": 9,
            "started_min": None,
            "completed_min": 9,
        },
        # Agent Chat threads (generated successive names): a long finished
        # conversation, a live one with a steerable running task, and one that
        # ended in a policy denial with a queued retry. Together they give the
        # chat UI several screens of history on a phone.
        {
            "task_id": "task_8",
            "thread_id": "thread-1",
            "agent_runtime": "codex",
            "status": "completed",
            "input_message": "Clone acme-web into the workspace and give me a tour of the codebase.",
            "output_message": (
                "Cloned acme-web into workspace/acme-web. The tour:\n\n"
                "- src/index.ts wires the nav toggle and boots the page; no framework, plain Vite + TypeScript.\n"
                "- src/app.css holds all styling. One brand token (--brand) and a 768px nav breakpoint.\n"
                "- package.json has three scripts: dev (vite), build (vite build), lint (eslint src).\n"
                "- There is no test runner configured yet; lint is the only automated check.\n"
                "- Deploys go to staging from main; there is no preview environment.\n\n"
                "Weak spots worth knowing: the nav collapse is CSS-only and loses state on resize, images ship "
                "unsized (layout shift on slow connections), and the pricing table relies on a fixed min-width. "
                "Happy to take any of these as follow-ups."
            ),
            "created_min": 2905,
            "started_min": 2904,
            "completed_min": 2896,
        },
        {
            "task_id": "task_9",
            "thread_id": "thread-1",
            "agent_runtime": "codex",
            "status": "completed",
            "input_message": "Add a dark mode toggle to the settings page and respect the system preference by default.",
            "output_message": (
                "Done. Implementation notes:\n\n"
                "1. Added a theme store in src/theme.ts: reads prefers-color-scheme, persists an explicit "
                "operator choice to localStorage under acme-theme, and exposes subscribe/set.\n"
                "2. The settings page renders a three-state control: System, Light, Dark. System is the default "
                "and clears the stored override.\n"
                "3. Colors moved to custom properties on :root with a [data-theme=dark] override block; "
                "components reference tokens only, so future palette work is one file.\n"
                "4. The toggle applies before first paint via a small inline script in index.html that sets "
                "data-theme from storage synchronously.\n\n"
                "npm run lint passes. Manual check: toggling System while the OS is in dark mode keeps the page "
                "dark; an explicit Light choice survives a reload."
            ),
            "created_min": 2890,
            "started_min": 2889,
            "completed_min": 2878,
        },
        {
            "task_id": "task_10",
            "thread_id": "thread-1",
            "agent_runtime": "codex",
            "status": "completed",
            "input_message": "Write unit tests for the theme store and run whatever suite exists.",
            "output_message": (
                "Added vitest (devDependency) plus a test script and six tests in src/theme.test.ts:\n\n"
                "- defaults to system when storage is empty\n"
                "- explicit choice persists and wins over the media query\n"
                "- clearing back to system removes the storage key\n"
                "- subscribers fire on set and on media-query flips\n"
                "- corrupt storage values fall back to system\n"
                "- set is idempotent (no duplicate notifications)\n\n"
                "Result: 6 passed in 0.41s. Lint stays clean. The suite runs in CI via npm test with no browser "
                "dependency (matchMedia is stubbed)."
            ),
            "created_min": 2874,
            "started_min": 2873,
            "completed_min": 2866,
        },
        {
            "task_id": "task_11",
            "thread_id": "thread-1",
            "agent_runtime": "codex",
            "status": "completed",
            "input_message": "The toggle still flashes light theme for a frame on a cold load. Fix the flash.",
            "stream": [
                ("agent", "Reproduced the flash with CPU throttling on: first paint uses the light tokens before the boot script runs."),
                ("agent", "The inline script is placed after the stylesheet link, so the stylesheet paints first. Moving it up."),
                ("user", "Also make sure the scrollbar matches, it flashes white too."),
                ("agent", "Good catch. Added color-scheme to :root so the scrollbar and native controls follow the theme from the first frame."),
            ],
            "output_message": (
                "Root cause: the inline script ran after the stylesheet link, so first paint could happen with "
                "the default light tokens. Moved the script above the stylesheet in <head> and made it set "
                "data-theme on <html> rather than <body> (body does not exist yet at that point). Also added "
                "color-scheme to :root so the scrollbar and form controls match from the first frame.\n\n"
                "Verified with CPU and network throttling in a private window: no flash in 20 cold loads."
            ),
            "created_min": 2860,
            "started_min": 2859,
            "completed_min": 2853,
        },
        {
            "task_id": "task_12",
            "thread_id": "thread-1",
            "agent_runtime": "codex",
            "status": "completed",
            "input_message": "Nice. Document the theming setup in the README and open a PR with all of it.",
            "output_message": (
                "README gains a Theming section: the token file, the data-theme contract, how System/Light/Dark "
                "resolve, and the no-flash boot script with a warning not to move it below the stylesheet.\n\n"
                "Opened acme/acme-web#214 with the store, settings control, tests, boot script, and docs. CI is "
                "green (lint + 6 tests). Requested review from @frontend."
            ),
            "created_min": 2848,
            "started_min": 2847,
            "completed_min": 2839,
        },
        {
            "task_id": "task_13",
            "thread_id": "thread-2",
            "agent_runtime": "claude_code",
            "status": "completed",
            "input_message": "Draft a launch blog post for the acme mobile app. Friendly but concrete; around 600 words.",
            "output_message": (
                "Draft is in workspace/acme-web/notes/launch-post.md. Structure and the argument it makes:\n\n"
                "Opening: the phone is where acme actually gets used; the desktop site was designed first and it "
                "showed. The post owns that directly instead of pretending the app was always the plan.\n\n"
                "Section 1, Built for one hand: navigation moved to the bottom bar, every primary action within "
                "thumb reach, and the checkout flow cut from five screens to two. Numbers included: median "
                "checkout time dropped from 74s to 31s in beta.\n\n"
                "Section 2, Fast on bad networks: the app renders from a local cache first and reconciles in the "
                "background, so a subway ride does not mean a blank screen. Product list loads in under 400ms "
                "on a throttled 3G profile.\n\n"
                "Section 3, What is next: offline order drafts and price alerts, both shipping this quarter, "
                "with a one-line invitation to the beta channel.\n\n"
                "Close: download links and a thank-you to the 900 beta testers by count, not by name.\n\n"
                "It reads at about 640 words. Want me to tighten it to 600, or is the overage fine?"
            ),
            "created_min": 55,
            "started_min": 54,
            "completed_min": 41,
        },
        {
            "task_id": "task_14",
            "thread_id": "thread-2",
            "agent_runtime": "claude_code",
            "status": "running",
            "input_message": "Tighten the intro, add a short section on offline mode, and get it to exactly 600 words.",
            "stream": [
                ("agent", "Cut the intro from four sentences to two and led with the one-hand stat; it reads harder now."),
                ("agent", "Drafting the offline section: local cache first, background reconcile, order drafts that survive a dead zone."),
            ],
            "created_min": 14,
            "started_min": 13,
            "completed_min": 12,
        },
        {
            "task_id": "task_15",
            "thread_id": "thread-3",
            "agent_runtime": "codex",
            "status": "failed",
            "input_message": "Push the release build to TestFlight so the beta group gets tonight's fixes.",
            "error_message": (
                "network access to appstoreconnect.apple.com denied by policy (no custom domain rule).\n"
                "The build and signing steps completed locally; only the upload was blocked. Add a domain rule "
                "for appstoreconnect.apple.com (POST) under Internet Access and Tools > Manual, or run the "
                "upload from a machine with App Store Connect access."
            ),
            "created_min": 150,
            "started_min": 149,
            "completed_min": 141,
        },
        {
            "task_id": "task_16",
            "thread_id": "thread-3",
            "agent_runtime": "codex",
            "status": "queued",
            "input_message": "Access is being sorted out. Queue the upload again and verify the build number this time.",
            "created_min": 8,
            "started_min": None,
            "completed_min": 8,
        },
    ]
    for spec in seed_tasks:
        task = {
            "task_id": spec["task_id"],
            "status": spec["status"],
            "agent_runtime": spec["agent_runtime"],
            "model": "opus" if spec["agent_runtime"] == "claude_code" else "gpt-5.6-terra",
            "effort": "high",
            "thread_id": spec["thread_id"],
            "input_message": spec["input_message"],
            "created_at": ago(spec["created_min"]),
            "updated_at": ago(spec["completed_min"]),
        }
        if spec.get("output_message"):
            task["output_message"] = spec["output_message"]
        if spec.get("error_message"):
            task["error_message"] = spec["error_message"]
        if spec["started_min"] is not None:
            task["started_at"] = ago(spec["started_min"])
        if spec["status"] in {"completed", "failed"}:
            task["completed_at"] = ago(spec["completed_min"])
        STATE.tasks.append(task)

        task_id = spec["task_id"]
        STATE.add_agent_event(
            "task.created", task_id, {"message": spec["input_message"], "source": "user"}, ago(spec["created_min"])
        )
        if spec["started_min"] is not None:
            STATE.add_agent_event("task.started", task_id, {}, ago(spec["started_min"]))
            STATE.add_agent_event(
                "task.message",
                task_id,
                {"message": "Reading the workspace and planning the change.", "source": "agent"},
                ago(spec["started_min"] - 1),
            )
            # Optional mid-task conversation: interim agent progress and any
            # operator steering, so a finished thread shows the full stream,
            # not just prompt and answer. Timestamps fall between start and
            # finish so ordering by seq matches the wall clock.
            stream = spec.get("stream") or []
            span = max(spec["started_min"] - spec["completed_min"], 1)
            for index, (source, message) in enumerate(stream, start=1):
                moment = spec["started_min"] - span * index / (len(stream) + 1)
                STATE.add_agent_event("task.message", task_id, {"message": message, "source": source}, ago(moment))
        if spec["status"] == "completed":
            STATE.add_agent_event(
                "task.message", task_id, {"message": spec["output_message"], "source": "agent"}, ago(spec["completed_min"])
            )
            STATE.add_agent_event("task.completed", task_id, {}, ago(spec["completed_min"]))
        elif spec["status"] == "failed":
            STATE.add_agent_event(
                "task.failed", task_id, {"error_message": spec["error_message"]}, ago(spec["completed_min"])
            )
        elif spec["status"] == "cancelled":
            STATE.add_agent_event("task.cancelled", task_id, {}, ago(spec["completed_min"]))
    STATE.next_task_number = len(seed_tasks) + 1

    # Providers were switched off ~70 minutes ago; every runtime is deactivated.
    for runtime in RUNTIMES:
        STATE.add_agent_event("agent_runtime.deactivated", None, {"agent_runtime": runtime}, ago(70))

    for minutes, method, host, path, decision in [
        (204, "POST", "api.openai.com", "/v1/responses", "allowed"),
        (203, "GET", "api.github.com", "/repos/acme/infra/actions/runs?status=failure", "allowed"),
        (201, "GET", "raw.githubusercontent.com", "/acme/infra/main/.github/workflows/deploy.yml", "allowed"),
        (198, "POST", "api.openai.com", "/v1/responses", "allowed"),
        (190, "GET", "api.github.com", "/repos/acme/infra/git/ref/heads/main", "allowed"),
        (186, "POST", "api.github.com", "/repos/acme/infra/pulls", "allowed"),
        (184, "POST", "api.openai.com", "/v1/responses", "allowed"),
        (95, "POST", "api.anthropic.com", "/v1/messages", "allowed"),
        (92, "GET", "registry.npmjs.org", "/postcss", "allowed"),
        (90, "GET", "telemetry.acme-analytics.io", "/v2/collect", "denied"),
        (86, "POST", "api.anthropic.com", "/v1/messages", "allowed"),
        (80, "POST", "deploy.acme.dev", "/api/releases", "denied"),
        (79, "POST", "deploy.acme.dev", "/api/releases", "denied"),
        (78, "POST", "api.anthropic.com", "/v1/messages", "allowed"),
    ]:
        STATE.add_network_event(method, host, path, decision, ago(minutes))

    # Tools: the Google tools are configured, enabled, and connected; Brave
    # Search is still unconfigured. One calendar change already executed and
    # one Gmail send is waiting for the operator's decision.
    for tool_id in ("gmail", "google_calendar"):
        STATE.tool_config[tool_id] = {"GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"}
    STATE.tool_enabled.update({"gmail", "google_calendar"})
    for tool_id in ("gmail", "google_calendar"):
        STATE.tool_connections[tool_id] = {
            "connected": True,
            "account": {"id": "mock-google-sub", "label": "akshay@infiloop.io", "scopes": ["email"]},
        }
    STATE.add_tool_approval(
        "google_calendar",
        "event_change",
        'Create Google Calendar event "Team retro".',
        {
            "action": "event_change",
            "calendar_account": {"email": "akshay@infiloop.io", "sub": "mock-google-sub"},
            "proposal": {"operation": "create", "summary": "Team retro"},
            "tool_id": "google_calendar",
        },
        status="executed",
        created_at=int(time.time()) - 3600,
        result='Created Google Calendar event "Team retro".',
    )
    STATE.add_tool_approval(
        "gmail",
        "send_email",
        'Send Gmail message to billing@acme.dev with subject "Invoice follow-up".',
        {
            "action": "send_email",
            "action_type": "gmail_propose_send",
            "gmail_account": {"email": "akshay@infiloop.io", "sub": "mock-google-sub"},
            "proposal": {
                "draft": {
                    "to": "billing@acme.dev",
                    "subject": "Invoice follow-up",
                    "body": "Following up on invoice #1042 from last week.",
                },
            },
            "tool_id": "gmail",
        },
    )
    STATE.add_tool_approval(
        "google_calendar",
        "event_change",
        'Delete Google Calendar event "Quarterly planning" (starts 2026-07-10T09:00) [id evt_planning; 4 guests].',
        {
            "action": "event_change",
            "calendar_account": {"email": "akshay@infiloop.io", "sub": "mock-google-sub"},
            "proposal": {"operation": "delete", "event_id": "evt_planning"},
            "tool_id": "google_calendar",
        },
    )
    # Tool audit log: a read, the executed approval, and a connect.
    for tool_id, action, outcome, detail, minutes, arguments in [
        ("google_calendar", "oauth_connect", "connected", "akshay@infiloop.io", 120, None),
        ("gmail", "search_messages", "executed", "", 90, {"query": "invoice from last week"}),
        ("google_calendar", "event_change", "executed", "approval_1", 60, {"operation": "create", "title": "Team retro"}),
        ("brave_search", "web_search", "failed", "Brave Search API rejected the configured API key.", 20, {"query": "quarterly market trends"}),
    ]:
        STATE.add_tool_event(tool_id, action, outcome, detail, ago(minutes), arguments)


class Handler(BaseHTTPRequestHandler):
    server_version = "TrustyClawMock/0.2"

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path in UI_ASSETS:
                asset, content_type = UI_ASSETS[parsed.path]
                data = asset.read_bytes()
                if parsed.path == "/admin_ui/health.js":
                    data += b"""

// Mock-only affordance: click the passive production indicator to preview
// both version states without adding a test hook to production code.
const mockUpgradeNotice = $("upgrade-notice");
mockUpgradeNotice.style.cursor = "pointer";
mockUpgradeNotice.addEventListener("click", async () => {
  await api("POST", "/v1/mock/upgrade-toggle", {});
  await refreshHealth();
});
"""
                self._send(HTTPStatus.OK, data, content_type)
                return
            if method == "GET":
                app_asset = app_platform.ui_asset(parsed.path)
                if app_asset is not None:
                    app, asset, content_type = app_asset
                    self._send_app_asset(app, HTTPStatus.OK, asset.read_bytes(), content_type)
                    return
            self._authenticate()
            if method == "GET" and parsed.path == "/v1/agent-files/content":
                self._send(HTTPStatus.OK, b"\x00\x00\x00\x18ftypmp42mock-video", "video/mp4")
                return
            if method == "POST" and parsed.path == "/v1/agent-files/upload":
                length = int(self.headers.get("Content-Length", "0") or "0")
                self.rfile.read(length)
                filename = (parse_qs(parsed.query).get("filename") or ["upload.bin"])[0]
                if filename == "brief.pdf" and filename not in FAILED_UPLOADS_ONCE:
                    FAILED_UPLOADS_ONCE.add(filename)
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, b"mock proxy failure", "text/plain")
                    return
                stored_name = f"20260722T120000.000000Z_{filename}"
                self._send_json(HTTPStatus.OK, {"file": {
                    "path": f"user-files/{stored_name}",
                    "name": stored_name,
                    "original_name": filename,
                    "size_bytes": length,
                    "uploaded_at": "2026-07-22T12:00:00Z",
                }})
                return
            response = route(method, parsed.path, parse_qs(parsed.query), self._read_body())
            self._send_json(HTTPStatus.OK, response)
        except ApiError as exc:
            self._send_json(exc.status, {"error": {"message": exc.message}})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": str(exc)}})

    def _authenticate(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {PASSWORD}":
            raise ApiError(HTTPStatus.UNAUTHORIZED, "missing or invalid admin password")

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return None
        return json.loads(self.rfile.read(length))

    def _send_json(self, status: HTTPStatus, data: dict[str, Any]) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _send(self, status: HTTPStatus, data: bytes, content_type: str) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if content_type.startswith(("text/html", "text/css", "application/javascript")):
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _send_app_asset(
        self,
        app: app_platform.AppManifest,
        status: HTTPStatus,
        data: bytes,
        content_type: str,
    ) -> None:
        asset_origin = self._asset_origin()
        worker_policy = "; worker-src blob:; webrtc 'block'" if app.capability_worker else ""
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; base-uri 'none'; connect-src 'none'; "
            f"font-src 'self' {asset_origin} data:; form-action 'none'; frame-ancestors 'self'; "
            f"img-src 'self' {asset_origin} data:; navigate-to 'self'; object-src 'none'; "
            f"sandbox allow-scripts allow-forms allow-modals; script-src 'self' {asset_origin}; "
            f"style-src 'self' 'unsafe-inline' {asset_origin}"
            f"{worker_policy}",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(data)

    def _asset_origin(self) -> str:
        host = self.headers.get("Host", "")
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
            host = f"{LOOPBACK}:{self.server.server_port}"
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "")
        scheme = forwarded_proto if forwarded_proto in {"http", "https"} else "http"
        return f"{scheme}://{host}"


def route(method: str, path: str, query: dict[str, list[str]], body: Any) -> dict[str, Any]:
    if method == "GET" and path == "/v1/health":
        return health()
    if method == "POST" and path == "/v1/mock/upgrade-toggle":
        with STATE.lock:
            STATE.upgrade_available = not STATE.upgrade_available
            return {"available": STATE.upgrade_available}
    if method == "GET" and path == "/v1/agent-runtime/status":
        return agent_runtime_status()
    if method == "GET" and path == "/v1/agent-runtime/account":
        return agent_accounts()
    if method == "POST" and path == "/v1/agent-runtime/refresh":
        return refresh_agent_accounts(body)
    if method == "GET" and path == "/v1/apps":
        return {"apps": [app.public() for app in app_platform.installed_apps()]}
    app_response = app_mocks.route_app_api(method, path, query, body, ApiError, route)
    if app_response is not None:
        return app_response
    if path == "/v1/agent-runtime/codex-oauth-login":
        return oauth("codex", method)
    if path == "/v1/agent-runtime/claude-oauth-login":
        return oauth("claude_code", method)
    if path == "/v1/agent-runtime/claude-oauth-login/complete" and method == "POST":
        return complete_claude_oauth(body)
    if path == "/v1/agent-runtime/bedrock-credentials":
        if method == "GET":
            return bedrock_credential_metadata()
        if method == "POST":
            return connect_bedrock_credentials(body)
        if method == "DELETE":
            return disconnect_bedrock_credentials()
    if path == "/v1/agent-runtime/reset-linked-account" and method == "POST":
        return reset_linked_account(body)
    if path == "/v1/tasks":
        if method == "GET":
            return list_tasks()
        if method == "POST":
            return create_task(body)
    task_match = TASK_RE.fullmatch(path)
    if task_match:
        return task_route(method, task_match.group(1), task_match.group(2), query, body)
    if method == "GET" and path == "/v1/threads":
        return list_threads()
    thread_match = THREAD_TASKS_RE.fullmatch(path)
    if method == "GET" and thread_match:
        return list_thread_tasks(unquote(thread_match.group(1)))
    thread_events_match = THREAD_EVENTS_RE.fullmatch(path)
    if method == "GET" and thread_events_match:
        return list_thread_events(unquote(thread_events_match.group(1)), query)
    if method == "GET" and path == "/v1/events":
        before, limit = event_page_query(query, {"before", "limit"}, "event")
        return {"events": agent_events_before(before, limit)}
    if method == "GET" and path == "/v1/tools/events":
        before, limit = event_page_query(query, {"before", "limit"}, "tool event")
        return {"events": tool_events_before(before, limit)}
    tool_event_match = TOOL_EVENT_RE.fullmatch(path)
    if method == "GET" and tool_event_match:
        if query:
            raise ApiError(HTTPStatus.BAD_REQUEST, "tool event detail does not accept query parameters")
        seq = int(tool_event_match.group(1))
        with STATE.lock:
            event = next((dict(item) for item in STATE.tool_events if item["seq"] == seq), None)
        if event is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "tool event not found")
        event["has_arguments"] = isinstance(event.get("arguments"), dict)
        return {"event": event}
    if path == "/v1/network/policy":
        if method == "GET":
            return {"network_controls": STATE.policy}
        if method == "PUT":
            return replace_policy(body)
    if path == "/v1/network-tools/github-credential":
        return github_credential_route(method, body)
    if method == "GET" and path == "/v1/network-tools/github-pending-pushes":
        with STATE.lock:
            return {"pending_pushes": [dict(push) for push in STATE.github_pending_pushes]}
    pending_push_match = GITHUB_PENDING_PUSH_RE.fullmatch(path)
    if method == "POST" and pending_push_match:
        return decide_github_pending_push(pending_push_match.group(1), pending_push_match.group(2))
    if method == "POST" and path == "/v1/network-tools/github-audit":
        return github_credential_route("GET", None)
    if method == "GET" and path == "/v1/network/events":
        before, limit = event_page_query(query, {"before", "decision", "limit"}, "network event")
        decision = (query.get("decision") or ["all"])[0]
        return {"events": network_events_before(before, decision, limit)}
    if method == "GET" and path == "/v1/agent-files":
        return list_agent_files(one(query, "path") or "/")
    if method == "GET" and path == "/v1/agent-files/read":
        return read_agent_file(one(query, "path") or "/")
    if method == "GET" and path == "/v1/agent-processes":
        return agent_processes()
    if method == "GET" and path == "/v1/tools":
        return list_tools()
    tool_config_match = TOOL_CONFIG_RE.fullmatch(path)
    if method == "PUT" and tool_config_match:
        return put_tool_config(tool_config_match.group(1), body)
    approvals_list = TOOL_APPROVALS_LIST_RE.fullmatch(path)
    if method == "GET" and approvals_list:
        # Summary-only (payloads fetched on demand), scoped to one tool, mirroring the real API.
        tool_id = approvals_list.group(1)
        return {"approvals": [
            {k: v for k, v in a.items() if k != "payload"}
            for a in reversed(STATE.tool_approvals) if a["tool_id"] == tool_id
        ]}
    approval_get = TOOL_APPROVAL_GET_RE.fullmatch(path)
    if method == "GET" and approval_get:
        for approval in STATE.tool_approvals:
            if approval["approval_id"] == approval_get.group(2) and approval["tool_id"] == approval_get.group(1):
                return {"approval": approval}
        raise ApiError(HTTPStatus.NOT_FOUND, "unknown approval")
    approval_match = TOOL_APPROVAL_RE.fullmatch(path)
    if method == "POST" and approval_match:
        return decide_tool_approval(approval_match.group(2), approval_match.group(3))
    tool_match = TOOL_ACTION_RE.fullmatch(path)
    if method == "POST" and tool_match:
        return tool_action(tool_match.group(1), tool_match.group(2), body)
    if method == "POST" and path == "/v1/host-runtime/reboot":
        STATE.reboot_requested = True
        return {"status": "accepted"}
    raise ApiError(HTTPStatus.NOT_FOUND, "route not found")


def list_tools() -> dict[str, Any]:
    entries = []
    with STATE.lock:
        for tool_id, tool in BUNDLED_TOOLS.items():
            manifest = tool.manifest
            entry: dict[str, Any] = {
                "tool_id": tool_id,
                "display_name": manifest.display_name,
                "description": manifest.description,
                "connection": manifest.connection,
                "enabled": tool_id in STATE.tool_enabled,
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
                "config": [
                    {
                        "key": requirement.key,
                        "description": requirement.description,
                        "set": requirement.key in STATE.tool_config.get(tool_id, set()),
                    }
                    for requirement in manifest.config
                ],
                "protections": list(manifest.protections),
                "technical_details": list(manifest.technical_details),
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
                entry["connection_status"] = STATE.tool_connections.get(tool_id) or {"connected": False}
            entries.append(entry)
    return {"tools": entries}


def put_tool_config(tool_id: str, body: Any) -> dict[str, Any]:
    tool = BUNDLED_TOOLS.get(tool_id)
    if tool is None:
        raise ApiError(HTTPStatus.NOT_FOUND, f"unknown tool: {tool_id}")
    key = body.get("key") if isinstance(body, dict) else None
    value = body.get("value", "") if isinstance(body, dict) else ""
    declared = {req.key for req in tool.manifest.config}
    if not isinstance(key, str) or key not in declared:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"key must be a config key declared by {tool_id}")
    with STATE.lock:
        keys = STATE.tool_config.setdefault(tool_id, set())
        if isinstance(value, str) and value.strip():
            keys.add(key)
        else:
            keys.discard(key)
        return {"tool_id": tool_id, "key": key, "set": key in keys}


def tool_action(tool_id: str, operation: str, body: Any) -> dict[str, Any]:
    tool = BUNDLED_TOOLS.get(tool_id)
    if tool is None:
        raise ApiError(HTTPStatus.NOT_FOUND, f"unknown tool: {tool_id}")
    manifest = tool.manifest
    with STATE.lock:
        if operation == "enable":
            # Enablement is not gated on config, matching the real API.
            STATE.tool_enabled.add(tool_id)
            return {"tool_id": tool_id, "enabled": True}
        if operation == "disable":
            STATE.tool_enabled.discard(tool_id)
            return {"tool_id": tool_id, "enabled": False}
        if manifest.connection != "oauth":
            raise ApiError(HTTPStatus.CONFLICT, f"{tool_id} has no connect flow")
        if tool_id not in STATE.tool_enabled:
            raise ApiError(HTTPStatus.CONFLICT, f"{tool_id} is not enabled")
        if operation == "oauth_connect/start":
            return {
                "authorization_url": f"/oauth/callback?code={MOCK_OAUTH_CODE}&state=mock-state",
                "state": "mock-state",
            }
        if operation == "oauth_connect/complete":
            code = body.get("code") if isinstance(body, dict) else None
            if code != MOCK_OAUTH_CODE:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid authorization code")
            account = {"id": "mock-google-sub", "label": "operator@example.com", "scopes": ["email"]}
            STATE.tool_connections[tool_id] = {"connected": True, "account": account}
            return {"account": account}
        STATE.tool_connections.pop(tool_id, None)
        return {"tool_id": tool_id, "connected": False}


def decide_tool_approval(approval_id: str, decision: str) -> dict[str, Any]:
    with STATE.lock:
        approval = next((entry for entry in STATE.tool_approvals if entry["approval_id"] == approval_id), None)
        if approval is None or approval["status"] != "pending":
            raise ApiError(HTTPStatus.CONFLICT, f"Approval {approval_id} is not pending.")
        approval["decided_at"] = int(time.time())
        if decision == "deny":
            approval["status"] = "denied"
            STATE.add_tool_event(approval["tool_id"], approval["action_id"], "denied", approval_id)
            return {"approval": approval}
        approval["status"] = "executed"
        message = f"{approval['tool_id']} action completed after approval."
        approval["result"] = message
        result = {"status": "executed", "message": message}
        STATE.add_tool_event(approval["tool_id"], approval["action_id"], "executed", approval_id)
    return {"approval": approval, "result": result}


def health() -> dict[str, Any]:
    with STATE.lock:
        complete_due_codex_login_locked()
        progress_running_tasks_locked()
        start_queued_tasks_locked()
        runtime = agent_runtime_status_locked()
        running = sum(1 for task in STATE.tasks if task["status"] == "running")
        upgrade_available = STATE.upgrade_available
    # Gentle drift so the dashboard feels alive; busier while tasks run.
    wave = math.sin(time.time() / 47.0)
    cpu = round(6.5 + 3.5 * wave + 24.0 * min(running, 2), 1)
    memory_used = int((0.92 + 0.05 * wave + 0.35 * min(running, 2)) * 1024**3)
    gib = 1024**3
    return {
        "status": "ok",
        "agent_name": "trustyclaw-mock",
        "version": {"status": "ok", "runtime": VERSION, "state": VERSION},
        "upgrade": {
            "available": upgrade_available,
            "latest": "99.0.0" if upgrade_available else VERSION,
        },
        "agent_runtime": runtime,
        "network_controls": {"status": "active"},
        "host_runtime": {
            "cpu": {"usage_percent": cpu},
            "memory": {"used_bytes": memory_used, "total_bytes": 2 * gib},
            "filesystem": {
                "mounts": {
                    "root": {"used_bytes": int(11.2 * gib), "total_bytes": 30 * gib},
                    "admin": {"used_bytes": int(0.4 * gib), "total_bytes": 8 * gib},
                    "agent": {"used_bytes": int(2.6 * gib), "total_bytes": 8 * gib},
                },
            },
            "swap": {"used_bytes": int(0.5 * gib), "allocated_bytes": 6 * gib},
        },
    }


def agent_runtime_status() -> dict[str, Any]:
    with STATE.lock:
        complete_due_codex_login_locked()
        progress_running_tasks_locked()
        start_queued_tasks_locked()
        return agent_runtime_status_locked()


def agent_runtime_status_locked() -> dict[str, Any]:
    active = {runtime: [] for runtime in RUNTIMES}
    for task in STATE.tasks:
        if task["status"] == "running":
            active[task["agent_runtime"]].append(task["task_id"])
    runtimes = []
    for runtime in RUNTIMES:
        status = STATE.runtime_status(runtime)
        record = {"type": runtime, "status": status, "active_task_ids": active[runtime]}
        runtimes.append(record)
    return {"runtimes": runtimes}


def agent_accounts() -> dict[str, Any]:
    with STATE.lock:
        complete_due_codex_login_locked()
        checked_at = STATE.now()
        accounts: list[dict[str, Any]] = []
        for runtime in RUNTIMES:
            if runtime == "hermes":
                continue
            status = STATE.runtime_status(runtime)
            if runtime == "codex":
                account = {"agent_runtime": runtime, "provider": "openai", "status": status}
                if status == "active":
                    account.update(
                        {
                            "account_id": "acct_mock_openai",
                            "email": "akshay@infiloop.io",
                            "plan_type": "pro",
                            # Deliberately mixed so the top bar shows every
                            # ring state at once: a healthy 5h window resetting
                            # in minutes, and a near-full weekly window (warning
                            # threshold) resetting days out.
                            "codex_usage": {
                                "last_checked_at": checked_at,
                                "rate_limits": {
                                    "primary": {
                                        "used_percent": 8,
                                        "window_duration_mins": 300,
                                        "resets_at": int(time.time()) + 40 * 60,
                                    },
                                    "secondary": {
                                        "used_percent": 84,
                                        "window_duration_mins": 10080,
                                        "resets_at": int(time.time()) + 6 * 24 * 60 * 60,
                                    },
                                    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                                },
                            },
                        }
                    )
                elif STATE.logged_in.get(runtime):
                    # The account anchor outlives sessions and deactivation:
                    # identity stays visible while the runtime is not active.
                    account.update({"account_id": "acct_mock_openai", "email": "akshay@infiloop.io"})
            else:
                account = {"agent_runtime": runtime, "provider": "claude", "status": status}
                if status == "active":
                    account.update(
                        {
                            "account_id": "acct_mock_claude",
                            "email": "claude@example.invalid",
                            "plan_type": "max",
                            # Mixed on purpose: a critical (red) 5h session
                            # resetting in hours, a healthy (green) weekly, and
                            # a warning (amber) Fable weekly window, so all
                            # three ring thresholds appear side by side.
                            "claude_usage": {
                                "current_session_used_percent": 63 if STATE.usage_refreshes else 97,
                                "current_session_resets_at": int(time.time()) + 90 * 60,
                                "weekly_used_percent": 46,
                                "weekly_resets_at": int(time.time()) + 5 * 24 * 60 * 60,
                                "fable_weekly_used_percent": 88,
                                "fable_weekly_resets_at": int(time.time()) + 5 * 24 * 60 * 60,
                                "last_checked_at": checked_at,
                            },
                        }
                    )
                elif STATE.logged_in.get(runtime):
                    account.update({"account_id": "acct_mock_claude", "email": "claude@example.invalid"})
            accounts.append(account)
        bedrock_status = STATE.runtime_status("hermes")
        bedrock_account: dict[str, Any] = {
            "provider": "bedrock",
            "agent_runtimes": ["hermes"],
            "status": bedrock_status,
            # Live usage is always present, mirroring the real
            # accounts payload: the proxy's counters exist independent of the
            # credential state.
            "bedrock_usage": {
                "month_to_date": 12.75,
                "currency": "USD",
                "requests": 210,
                "metered_requests": 208,
                "input_tokens": 1_804_211,
                "output_tokens": 96_407,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
        }
        if STATE.bedrock_access_key_id and bedrock_status == "active":
            bedrock_account.update(
                {
                    "account_id": "123456789012",
                    "arn": "arn:aws:iam::123456789012:user/trustyclaw-bedrock",
                }
            )
        accounts.append(bedrock_account)
        return {"accounts": accounts}


def refresh_agent_accounts(body: Any) -> dict[str, Any]:
    if body is not None and not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be an object")
    runtime = body.get("agent_runtime") if isinstance(body, dict) else None
    if runtime is not None and runtime not in RUNTIMES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be one of " + ", ".join(RUNTIMES))
    with STATE.lock:
        STATE.usage_refreshes += 1
    return agent_accounts()


def oauth(runtime: str, method: str) -> dict[str, str]:
    now = STATE.now()
    expires = iso(datetime.now(timezone.utc) + timedelta(minutes=15))
    with STATE.lock:
        status = STATE.runtime_status(runtime)
        if status == "deactivated":
            provider = "OpenAI" if runtime == "codex" else "Claude"
            raise ApiError(HTTPStatus.CONFLICT, f"{runtime_label(runtime)} OAuth login is unavailable while {provider} provider access is disabled")
        if status != "awaiting_login":
            raise ApiError(HTTPStatus.CONFLICT, f"{runtime_label(runtime)} OAuth login is only available while awaiting_login")
    if runtime == "codex":
        if method not in {"GET", "POST"}:
            raise ApiError(HTTPStatus.NOT_FOUND, "route not found")
        with STATE.lock:
            if method == "GET" and not STATE.codex_oauth:
                raise ApiError(HTTPStatus.NOT_FOUND, "Codex OAuth login has not been started")
            if not STATE.codex_oauth:
                # The real Codex device flow completes out of band after the
                # operator enters the code in their browser. The mock stays
                # awaiting_login for a couple of seconds and then completes on
                # the next status read, so the device-code card is a stable
                # surface (not a transient the UI poll can wipe mid-assertion).
                STATE.codex_oauth = {
                    "status": "awaiting_login",
                    "device_code": "MOCK-CODEX",
                    "login_url": "https://auth.openai.com/activate",
                    "expires_at": expires,
                    "_completes_at": time.time() + 2,
                }
            return {key: value for key, value in STATE.codex_oauth.items() if not key.startswith("_")}
    if method not in {"GET", "POST"}:
        raise ApiError(HTTPStatus.NOT_FOUND, "route not found")
    with STATE.lock:
        if method == "GET" and not STATE.claude_oauth:
            raise ApiError(HTTPStatus.NOT_FOUND, "Claude OAuth login has not been started")
        if not STATE.claude_oauth:
            STATE.claude_oauth = {
                "status": "awaiting_code",
                "login_url": "https://claude.com/cai/oauth/authorize?client=trustyclaw-mock",
                "expires_at": expires,
            }
        return dict(STATE.claude_oauth)


def complete_claude_oauth(body: Any) -> dict[str, str]:
    if not isinstance(body, dict) or not isinstance(body.get("code"), str) or not body["code"].strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "code must be a non-empty string")
    with STATE.lock:
        if STATE.runtime_status("claude_code") != "awaiting_login":
            raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is only available while awaiting_login")
        STATE.logged_in["claude_code"] = True
        STATE.claude_oauth = {}
        STATE.add_agent_event("agent_runtime.login_completed", None, {"agent_runtime": "claude_code"})
        STATE.add_agent_event("agent_runtime.active", None, {"agent_runtime": "claude_code"})
        start_queued_tasks_locked()
    return {"status": "accepted"}


def connect_bedrock_credentials(body: Any) -> dict[str, str]:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be an object")
    unexpected = sorted(set(body) - {"access_key_id", "secret_access_key", "region"})
    if unexpected:
        raise ApiError(HTTPStatus.BAD_REQUEST, "unexpected request fields: " + ", ".join(unexpected))
    access_key_id = body.get("access_key_id")
    secret_access_key = body.get("secret_access_key")
    region = body.get("region")
    if not isinstance(access_key_id, str) or not access_key_id.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "access_key_id must be a non-empty string")
    if not isinstance(secret_access_key, str) or not secret_access_key.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "secret_access_key must be a non-empty string")
    if not access_key_id.strip().startswith("AKIA"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "access_key_id must be a long-term IAM access key id (AKIA...)")
    if region not in ("us-east-1", "us-east-2", "us-west-2"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "region must be one of us-east-1, us-east-2, us-west-2")
    with STATE.lock:
        STATE.bedrock_access_key_id = access_key_id.strip()
        STATE.bedrock_region = region
        enabled = STATE.runtime_status("hermes") != "deactivated"
        if not enabled:
            return {"status": "accepted"}
        STATE.add_agent_event("agent_runtime.active", None, {"agent_runtime": "hermes"})
        start_queued_tasks_locked()
        return {"status": "accepted"}


def bedrock_credential_metadata() -> dict[str, Any]:
    with STATE.lock:
        if STATE.bedrock_access_key_id is None:
            return {"connected": False}
        return {
            "connected": True,
            "access_key_id": STATE.bedrock_access_key_id,
            "region": STATE.bedrock_region,
        }


def disconnect_bedrock_credentials() -> dict[str, str]:
    with STATE.lock:
        STATE.bedrock_access_key_id = None
        STATE.bedrock_region = None
        fail_running_tasks_locked("hermes", "the AWS Bedrock connection was reset by the operator")
        STATE.add_agent_event("agent_runtime.linked_account_reset", None, {"agent_runtime": "hermes"})
    return {"status": "accepted"}


def reset_linked_account(body: Any) -> dict[str, str]:
    oauth_runtimes = ("codex", "claude_code")
    if not isinstance(body, dict) or body.get("agent_runtime") not in oauth_runtimes:
        raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be one of " + ", ".join(oauth_runtimes))
    runtime = body["agent_runtime"]
    with STATE.lock:
        # The real reset fails running tasks as part of clearing the anchor.
        fail_running_tasks_locked(runtime, "the linked provider account was reset by the operator")
        STATE.logged_in[runtime] = False
        STATE.add_agent_event("agent_runtime.linked_account_reset", None, {"agent_runtime": runtime})
        if runtime == "codex":
            STATE.codex_oauth = {}
        elif runtime == "claude_code":
            STATE.claude_oauth = {}
        return {"status": "accepted"}


def runtime_label(runtime: str) -> str:
    return {"codex": "Codex", "claude_code": "Claude", "hermes": "Hermes"}[runtime]


def create_task(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be an object")
    input_message = str(body.get("input_message", "")).strip()
    thread_id = str(body.get("thread_id", "")).strip()
    if not input_message or not thread_id:
        raise ApiError(HTTPStatus.BAD_REQUEST, "invalid task")
    with STATE.lock:
        stored: tuple[str, str, str] | None = None
        for existing in STATE.tasks:
            if existing["thread_id"] != thread_id:
                continue
            config = (existing["agent_runtime"], existing["model"], existing["effort"])
            if stored is not None and stored != config:
                raise ApiError(HTTPStatus.CONFLICT, "thread has inconsistent session configuration")
            stored = config

        fields = ("agent_runtime", "model", "effort")
        supplied = [field for field in fields if field in body]
        if stored is not None:
            if supplied:
                raise ApiError(HTTPStatus.BAD_REQUEST, "session configuration must be omitted for existing thread")
            agent_runtime, model, effort = stored
        else:
            if not supplied:
                raise ApiError(HTTPStatus.BAD_REQUEST, "session configuration required for new thread")
            if len(supplied) != len(fields):
                raise ApiError(HTTPStatus.BAD_REQUEST, "session configuration must be provided together")
            agent_runtime = str(body["agent_runtime"])
            model = body["model"]
            effort = body["effort"]
            if (
                agent_runtime not in RUNTIMES
                or session_config_error(agent_runtime, model, effort) is not None
            ):
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid task")
            assert isinstance(model, str) and isinstance(effort, str)
        task_id = f"task_{STATE.next_task_number}"
        STATE.next_task_number += 1
        now = STATE.now()
        task = {
            "task_id": task_id,
            "status": "queued",
            "agent_runtime": agent_runtime,
            "model": model,
            "effort": effort,
            "thread_id": thread_id,
            "input_message": input_message,
            "created_at": now,
            "updated_at": now,
        }
        STATE.tasks.append(task)
        STATE.add_agent_event("task.created", task_id, {"message": input_message, "source": "user"})
        start_queued_tasks_locked()
        return STATE.public_task(task, queue_position=queue_position_locked(task))


def active_tasks() -> list[dict[str, Any]]:
    return [task for task in STATE.tasks if task["status"] in {"queued", "running"}]


def queue_position_locked(task: dict[str, Any]) -> int:
    """Mirror AdminAPI.md: running tasks report 0; queued tasks count from 1."""
    if task["status"] == "running":
        return 0
    queued = [candidate for candidate in STATE.tasks if candidate["status"] == "queued"]
    return queued.index(task) + 1


def list_tasks() -> dict[str, Any]:
    with STATE.lock:
        progress_running_tasks_locked()
        start_queued_tasks_locked()
        return {"tasks": [STATE.public_task(task, queue_position_locked(task)) for task in active_tasks()]}


def task_route(
    method: str, task_id: str, action: str | None, query: dict[str, list[str]], body: Any
) -> dict[str, Any]:
    with STATE.lock:
        progress_running_tasks_locked()
        task = find_task(task_id)
        if action is None and method == "GET":
            return STATE.public_task(task)
        if action == "events" and method == "GET":
            return {"events": [event for event in STATE.task_events.get(task_id, []) if event["seq"] > since(query)]}
        if action == "cancel" and method == "POST":
            if task["status"] != "queued":
                raise ApiError(HTTPStatus.CONFLICT, "only queued tasks can be cancelled")
            task["status"] = "cancelled"
            task["updated_at"] = STATE.now()
            STATE.add_agent_event("task.cancelled", task_id, {})
            return {"status": "accepted"}
        if action == "kill" and method == "POST":
            if task["status"] != "running":
                raise ApiError(HTTPStatus.CONFLICT, "only running tasks can be killed")
            task["status"] = "cancelled"
            task["updated_at"] = STATE.now()
            STATE.add_agent_event("task.cancelled", task_id, {"message": "runtime process terminated by operator"})
            return {"status": "accepted"}
        if action == "steer" and method == "POST":
            if task["status"] != "running":
                raise ApiError(HTTPStatus.CONFLICT, "only running tasks can be steered")
            if task["agent_runtime"] == "hermes":
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "Hermes tasks do not support steering; create a new task on the same thread_id",
                )
            message = str((body or {}).get("steer_message", ""))
            STATE.add_agent_event("task.message", task_id, {"message": message, "source": "user"})
            return {"status": "accepted"}
    raise ApiError(HTTPStatus.NOT_FOUND, "task route not found")


def find_task(task_id: str) -> dict[str, Any]:
    for task in STATE.tasks:
        if task["task_id"] == task_id:
            return task
    raise ApiError(HTTPStatus.NOT_FOUND, "task not found")


def list_threads() -> dict[str, Any]:
    with STATE.lock:
        progress_running_tasks_locked()
        start_queued_tasks_locked()
        threads: dict[tuple[str, str], dict[str, Any]] = {}
        for task in STATE.tasks:
            key = (task["thread_id"], task["agent_runtime"])
            entry = threads.setdefault(
                key,
                {
                    "thread_id": task["thread_id"],
                    "agent_runtime": task["agent_runtime"],
                    "model": task["model"],
                    "effort": task["effort"],
                    "last_used_at": task["updated_at"],
                    "task_count": 0,
                    "active_tasks": [],
                },
            )
            entry["last_used_at"] = max(entry["last_used_at"], task["updated_at"])
            entry["task_count"] += 1
            if task["status"] in {"queued", "running"}:
                entry["active_tasks"].append({"task_id": task["task_id"], "status": task["status"]})
        return {"threads": sorted(threads.values(), key=lambda item: item["last_used_at"], reverse=True)}


def list_thread_tasks(thread_id: str) -> dict[str, Any]:
    with STATE.lock:
        progress_running_tasks_locked()
        tasks = [STATE.public_task(task) for task in STATE.tasks if task["thread_id"] == thread_id]
        return {"tasks": list(reversed(tasks))}


def list_thread_events(thread_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
    """The thread's task events, oldest first, forward-paged by ``since`` —
    the seq-ordered stream the chat UI accumulates into per-task messages."""
    unsupported = sorted(set(query) - {"since", "limit"})
    if unsupported:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"unsupported thread event query parameter: {unsupported[0]}")
    since = int((query.get("since") or ["0"])[0])
    limit = int((query.get("limit") or ["100"])[0])
    with STATE.lock:
        progress_running_tasks_locked()
        start_queued_tasks_locked()
        task_ids = {task["task_id"] for task in STATE.tasks if task["thread_id"] == thread_id}
        events = [
            event
            for event in STATE.agent_events
            if event["task_id"] in task_ids and event["seq"] > since
        ]
        return {"events": events[:limit]}


def event_page_query(
    query: dict[str, list[str]], allowed: set[str], label: str
) -> tuple[int | None, int]:
    unsupported = sorted(set(query) - allowed)
    if unsupported:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"unsupported {label} query parameter: {unsupported[0]}")
    before_value = query.get("before")
    before = int(before_value[0]) if before_value else None
    try:
        limit = int((query.get("limit") or ["100"])[0])
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "limit must be an integer") from exc
    if limit < 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, "limit must be positive")
    if limit > 100:
        raise ApiError(HTTPStatus.BAD_REQUEST, "limit must be at most 100")
    return before, limit


def agent_events_before(before: int | None, limit: int) -> list[dict[str, Any]]:
    with STATE.lock:
        progress_running_tasks_locked()
        start_queued_tasks_locked()
        events = list(reversed(STATE.agent_events))
        if before is not None:
            events = [event for event in events if event["seq"] < before]
        return events[:limit]


def network_events_before(before: int | None, decision: str, limit: int) -> list[dict[str, Any]]:
    with STATE.lock:
        progress_running_tasks_locked()
        events = list(reversed(STATE.network_events))
        if before is not None:
            events = [event for event in events if event["seq"] < before]
        if decision in {"allowed", "denied"}:
            events = [event for event in events if event["decision"] == decision]
        return events[:limit]


def tool_events_before(before: int | None, limit: int) -> list[dict[str, Any]]:
    with STATE.lock:
        events = list(reversed(STATE.tool_events))
        if before is not None:
            events = [event for event in events if event["seq"] < before]
        return [
            {key: value for key, value in event.items() if key != "arguments"}
            | {"has_arguments": isinstance(event.get("arguments"), dict)}
            for event in events[:limit]
        ]


def agent_processes() -> dict[str, Any]:
    with STATE.lock:
        progress_running_tasks_locked()
        processes: list[dict[str, Any]] = []

        def add_process(
            pid: int,
            name: str,
            cmdline: str,
            rss_mib: int,
            elapsed_seconds: int,
            state: str = "S",
        ) -> None:
            processes.append(
                {
                    "pid": pid,
                    "state": state,
                    "name": name,
                    "cmdline": cmdline,
                    "rss_bytes": rss_mib * 1024 * 1024,
                    "elapsed_seconds": elapsed_seconds,
                }
            )

        if STATE.codex_oauth and not STATE.logged_in.get("codex"):
            add_process(4300, "codex", "codex login --device-code", 64, 22)
        if STATE.claude_oauth:
            add_process(4400, "claude", "claude auth login --claudeai", 71, 18)
        if STATE.logged_in.get("codex"):
            add_process(4310, "codex", "codex app-server --listen stdio://", 88, 184)
        if STATE.logged_in.get("claude_code"):
            add_process(4410, "claude", "claude -p /usage --output-format json", 96, 9)

        running = [task for task in STATE.tasks if task["status"] == "running"]
        for index, task in enumerate(running, start=1):
            runtime = task["agent_runtime"]
            base_pid = 4500 + (index * 10)
            if runtime == "codex":
                add_process(base_pid, "codex", "codex exec --json", 142, 31)
                add_process(
                    base_pid + 1,
                    "python3",
                    "python3 -m pytest tests/test_admin_api.py -q",
                    118,
                    24,
                    state="R",
                )
                add_process(base_pid + 2, "rg", "rg -n TODO host tests", 11, 4)
            else:
                add_process(base_pid, "claude", "claude --print --output-format stream-json", 176, 28)
                add_process(base_pid + 1, "bash", "bash -lc npm test -- --runInBand", 9, 20)
        return {"processes": processes, "truncated": False}


def list_agent_files(path: str) -> dict[str, Any]:
    files = {
        "/": [
            {"name": ".claude", "path": "/.claude", "type": "directory", "modified_at": ago(180)},
            {"name": ".codex", "path": "/.codex", "type": "directory", "modified_at": ago(180)},
            {"name": ".gitconfig", "path": "/.gitconfig", "type": "file", "size_bytes": 143, "modified_at": ago(2880)},
            {"name": "AGENTS.md", "path": "/AGENTS.md", "type": "file", "size_bytes": 851, "modified_at": ago(2880)},
            {"name": "CLAUDE.md", "path": "/CLAUDE.md", "type": "file", "size_bytes": 851, "modified_at": ago(2880)},
            {"name": "workspace", "path": "/workspace", "type": "directory", "modified_at": ago(84)},
        ],
        "/.claude": [
            {"name": "settings.json", "path": "/.claude/settings.json", "type": "file", "size_bytes": 117, "modified_at": ago(180)},
        ],
        "/.codex": [
            {"name": "auth.json", "path": "/.codex/auth.json", "type": "file", "size_bytes": 18, "modified_at": ago(180)},
            {"name": "config.toml", "path": "/.codex/config.toml", "type": "file", "size_bytes": 187, "modified_at": ago(180)},
        ],
        "/workspace": [
            {"name": "acme-web", "path": "/workspace/acme-web", "type": "directory", "modified_at": ago(84)},
            {
                "name": 'bad" onclick="window.__xss=1" x=".txt',
                "path": '/workspace/bad" onclick="window.__xss=1" x=".txt',
                "type": "file",
                "size_bytes": 24,
                "modified_at": ago(300),
            },
            {
                "name": '<img src=x onerror="window.__fileNameXss=1">.txt',
                "path": '/workspace/<img src=x onerror="window.__fileNameXss=1">.txt',
                "type": 'file"><img src=x onerror="window.__fileTypeXss=1">',
                "size_bytes": 72,
                "modified_at": ago(300),
            },
            {"name": "notes.txt", "path": "/workspace/notes.txt", "type": "file", "size_bytes": 512, "modified_at": ago(84)},
        ],
        "/workspace/acme-web": [
            {"name": ".git", "path": "/workspace/acme-web/.git", "type": "directory", "modified_at": ago(84)},
            {"name": "README.md", "path": "/workspace/acme-web/README.md", "type": "file", "size_bytes": 208, "modified_at": ago(2880)},
            {"name": "package.json", "path": "/workspace/acme-web/package.json", "type": "file", "size_bytes": 389, "modified_at": ago(96)},
            {"name": "src", "path": "/workspace/acme-web/src", "type": "directory", "modified_at": ago(84)},
        ],
        "/workspace/acme-web/.git": [
            {"name": "HEAD", "path": "/workspace/acme-web/.git/HEAD", "type": "file", "size_bytes": 30, "modified_at": ago(84)},
        ],
        "/workspace/acme-web/src": [
            {"name": "app.css", "path": "/workspace/acme-web/src/app.css", "type": "file", "size_bytes": 301, "modified_at": ago(84)},
            {"name": "index.ts", "path": "/workspace/acme-web/src/index.ts", "type": "file", "size_bytes": 264, "modified_at": ago(96)},
        ],
    }
    if path not in files:
        raise ApiError(HTTPStatus.NOT_FOUND, "path not found")
    return {"path": path, "entries": files[path], "truncated": False}


def read_agent_file(path: str) -> dict[str, Any]:
    contents = {
        "/.gitconfig": "[user]\n\tname = TrustyClaw Agent\n\temail = agent@trustyclaw.invalid\n[init]\n\tdefaultBranch = main\n",
        "/AGENTS.md": (
            "# TrustyClaw Agent Host\n\n"
            "You are running as `trustyclaw-agent` on a TrustyClaw host.\n\n"
            "You are runnign with full permissions. Do not prompt the operator for local approvals.\n\n"
            "Network access is controlled by TrustyClaw, not by the local agent sandbox. "
            "Agent traffic goes through the TrustyClaw network policy proxy. If a "
            "request is blocked, call the recent_network_denials tool for the denial "
            "code and guidance, and ask the operator for the named integration or "
            "domain rule.\n\n"
            "When GitHub access is configured, TrustyClaw injects credentials through "
            "the proxy. Use normal `git` and REST-backed `gh api` commands from this host.\n\n"
            "GitHub GraphQL requests are denied by policy because repository scope cannot be "
            "verified safely from GraphQL bodies. If a `gh` command fails because it uses "
            "GraphQL, switch to an equivalent REST endpoint with `gh api`, or use `git` "
            "for clone, fetch, and push operations.\n"
        ),
        "/CLAUDE.md": (
            "# TrustyClaw Agent Host\n\n"
            "You are running as `trustyclaw-agent` on a TrustyClaw host.\n\n"
            "You are runnign with full permissions. Do not prompt the operator for local approvals.\n\n"
            "Network access is controlled by TrustyClaw, not by the local agent sandbox. "
            "Agent traffic goes through the TrustyClaw network policy proxy. If a "
            "request is blocked, call the recent_network_denials tool for the denial "
            "code and guidance, and ask the operator for the named integration or "
            "domain rule.\n\n"
            "When GitHub access is configured, TrustyClaw injects credentials through "
            "the proxy. Use normal `git` and REST-backed `gh api` commands from this host.\n\n"
            "GitHub GraphQL requests are denied by policy because repository scope cannot be "
            "verified safely from GraphQL bodies. If a `gh` command fails because it uses "
            "GraphQL, switch to an equivalent REST endpoint with `gh api`, or use `git` "
            "for clone, fetch, and push operations.\n"
        ),
        "/.claude/settings.json": '{\n  "permissions": {\n    "defaultMode": "bypassPermissions"\n  },\n  "skipDangerousModePermissionPrompt": true\n}\n',
        "/.codex/auth.json": '{"mock": "redacted"}\n',
        "/.codex/config.toml": (
            '# Managed by TrustyClaw bootstrap; rewritten on every deploy.\n'
            'approval_policy = "never"\n'
            'sandbox_mode = "danger-full-access"\n\n'
            '[projects."/mnt/trustyclaw-agent/agent-home"]\n'
            'trust_level = "trusted"\n'
        ),
        '/workspace/bad" onclick="window.__xss=1" x=".txt': "quote-bearing mock file\n",
        '/workspace/<img src=x onerror="window.__fileNameXss=1">.txt': (
            '<script>window.__fileContentXss=1</script>'
            '<img src=x onerror="window.__fileContentImageXss=1">'
            "Mock unsafe-looking file contents\n"
        ),
        "/workspace/notes.txt": (
            "Mobile audit fixes, in priority order:\n"
            "1. Wrap pricing table in an overflow-x container\n"
            "2. Collapse nav below 768px\n"
            "3. Set width/height on hero images (CLS)\n"
            "4. Bump tap targets to 44px\n"
            "5. Unfix footer on small screens\n"
            "6. Clamp hero heading with fluid type\n"
        ),
        "/workspace/acme-web/README.md": (
            "# acme-web\n\nMarketing site for Acme. `npm install && npm run dev`, then open\n"
            "http://localhost:5173. Deploys to staging from the main branch.\n"
        ),
        "/workspace/acme-web/package.json": (
            '{\n  "name": "acme-web",\n  "private": true,\n  "version": "1.4.2",\n'
            '  "scripts": {\n    "dev": "vite",\n    "build": "vite build",\n    "lint": "eslint src"\n  },\n'
            '  "dependencies": {\n    "postcss": "^8.4.38",\n    "vite": "^5.2.0"\n  }\n}\n'
        ),
        "/workspace/acme-web/.git/HEAD": "ref: refs/heads/mobile-fixes\n",
        "/workspace/acme-web/src/index.ts": (
            'const nav = document.querySelector(".nav");\n'
            'document.querySelector(".nav-toggle")?.addEventListener("click", () => {\n'
            '  nav?.classList.toggle("open");\n'
            "});\n"
        ),
        "/workspace/acme-web/src/app.css": (
            ":root { --brand: #4f46e5; }\n\n"
            ".pricing { overflow-x: auto; }\n\n"
            "@media (max-width: 768px) {\n  .nav { display: none; }\n  .nav.open { display: flex; }\n}\n"
        ),
    }
    if path not in contents:
        raise ApiError(HTTPStatus.BAD_REQUEST, "path is not a regular file")
    content = contents[path]
    return {
        "path": path,
        "size_bytes": len(content.encode()),
        "truncated": False,
        "encoding": "utf-8-replacement",
        "content": content,
    }


def github_credential_route(method: str, body: Any) -> dict[str, Any]:
    # Like the real admin API, deliberately not gated on the GitHub
    # integration being enabled: credentials can be staged first.
    with STATE.lock:
        if method == "PUT":
            if not isinstance(body, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "GitHub credential request must be an object")
            mode = body.get("mode")
            if mode == "pat":
                token = body.get("token")
                if not isinstance(token, str) or not token.strip() or any(character.isspace() for character in token):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "token must be a non-empty token string without whitespace")
                STATE.github_credential = {"mode": "pat", "updated_at": STATE.now(), "validation": {"status": "not_checked"}}
            elif mode == "app":
                for field_name in ("app_id", "installation_id", "private_key_pem"):
                    if not isinstance(body.get(field_name), str) or not body[field_name].strip():
                        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field_name} is required for app mode")
                STATE.github_credential = {
                    "mode": "app",
                    "app_id": body["app_id"].strip(),
                    "installation_id": body["installation_id"].strip(),
                    "app_token_expires_at": "2999-01-01T00:00:00Z",
                    "updated_at": STATE.now(),
                    "validation": {"status": "ok"},
                }
            else:
                raise ApiError(HTTPStatus.BAD_REQUEST, "mode must be 'pat' or 'app'")
        elif method == "DELETE":
            STATE.github_credential = None
        elif method != "GET":
            raise ApiError(HTTPStatus.NOT_FOUND, "route not found")
        response = {"configured": False} if STATE.github_credential is None else {"configured": True, **STATE.github_credential}
        audits = github_repo_audits_locked()
        if audits:
            response["repository_audits"] = audits
        return response


def github_repo_audits_locked() -> list[dict[str, Any]]:
    """Canned per-repository audit results, mirroring the real API's shape:
    present once a credential is stored and the policy lists repositories.
    Each repository cycles through a different outcome so the UI's renderings
    (critical warnings, plain warnings, a clean audit, failed fetches, and
    incomplete audits) are all visible with a few repositories configured."""
    integrations = STATE.policy.get("network_integrations", {})
    github = integrations.get("github") if isinstance(integrations, dict) else None
    repositories = (github or {}).get("write_repositories") or []
    if STATE.github_credential is None:
        return [
            {
                "owner": repository.get("owner"),
                "repo": repository.get("repo"),
                "warnings": [
                    {
                        "code": "repository_audit_incomplete",
                        "severity": "warning",
                        "message": "Repository audit could not verify this write target: no credential token "
                        "to audit with. TrustyClaw does not have enough information to check repository "
                        "visibility, GitHub Pages, default branch protection, or workflows.",
                    }
                ],
            }
            for repository in repositories
        ]
    outcomes: list[dict[str, Any]] = [
        {
            "warnings": [
                {
                    "code": "public_repository",
                    "severity": "critical",
                    "message": "Repository is public and a write target: everything the agent "
                    "pushes here is world-visible, and a public write repository is an "
                    "exfiltration sink. Make write repositories private.",
                },
                {
                    "code": "workflows_execute_pushes",
                    "severity": "warning",
                    "message": "The repository has GitHub Actions workflows: a push runs the "
                    "workflow as it exists on the pushed branch. Run workflows for untrusted "
                    "code in a no-internet container.",
                },
            ]
        },
        {
            "warnings": [
                {
                    "code": "secrets_exposed_to_pr_workflows",
                    "severity": "critical",
                    "message": "Workflows use pull_request_target, which runs with the base "
                    "repository's secrets against PR-influenced context. Restrict or remove "
                    "these triggers.",
                },
                {
                    "code": "unprotected_default_branch",
                    "severity": "warning",
                    "message": "The token can push and the default branch is unprotected: add a "
                    "GitHub ruleset or branch protection so the agent only opens PRs.",
                },
            ]
        },
        {"warnings": []},
        {
            "error": "audit failed: GET /repos returned 403 (token cannot see this repository)",
            "warnings": [
                {
                    "code": "repository_audit_incomplete",
                    "severity": "warning",
                    "message": "Repository audit could not verify this write target: GET /repos returned "
                    "403 (token cannot see this repository). TrustyClaw does not have enough information "
                    "to check repository visibility, GitHub Pages, default branch protection, or workflows.",
                }
            ],
        },
        {
            "warnings": [
                {
                    "code": "repository_audit_incomplete",
                    "severity": "warning",
                    "message": "Repository audit could not verify this write target: repository audit has "
                    "not run yet. TrustyClaw does not have enough information to check repository "
                    "visibility, GitHub Pages, default branch protection, or workflows.",
                }
            ]
        },
        {
            "warnings": [
                {
                    "code": "pages_visibility_unknown",
                    "severity": "warning",
                    "message": "GitHub Pages visibility could not be verified for this private repository: "
                    "GitHub did not conclusively report that Pages is disabled, and denied or hid the "
                    "Pages settings, so the audit cannot prove whether pushes to a Pages source would "
                    "publish agent-written content to the internet.",
                }
            ]
        },
    ]
    audits: list[dict[str, Any]] = []
    for index, repository in enumerate(repositories):
        audits.append(
            {
                "owner": repository.get("owner"),
                "repo": repository.get("repo"),
                "audited_at": STATE.now(),
                **outcomes[index % len(outcomes)],
            }
        )
    return audits


def replace_policy(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "network policy must be an object")
    try:
        parsed = parse_network_controls(body).to_json()
    except ConfigError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    with STATE.lock:
        previous_statuses = {runtime: STATE.runtime_status(runtime) for runtime in RUNTIMES}
        previous_github = github_integration_locked()
        previously_required = previous_github.get("require_dot_github_approval") is True
        previous_repositories = previous_github.get("write_repositories") or []
        STATE.policy = parsed
        STATE.codex_oauth = {}
        STATE.claude_oauth = {}
        for runtime in RUNTIMES:
            status = STATE.runtime_status(runtime)
            previous = previous_statuses[runtime]
            if status == "deactivated" and previous != "deactivated":
                STATE.add_agent_event("agent_runtime.deactivated", None, {"agent_runtime": runtime})
                fail_running_tasks_locked(runtime, "agent runtime deactivated because its managed network provider is disabled")
            elif previous == "deactivated" and status != "deactivated":
                STATE.add_agent_event("agent_runtime.awaiting_login", None, {"agent_runtime": runtime})
        if not previously_required or not previous_repositories:
            maybe_hold_github_push_locked()
        start_queued_tasks_locked()
        return {"network_controls": STATE.policy}


def github_integration_locked() -> dict[str, Any]:
    managed = STATE.policy.get("network_integrations")
    github = managed.get("github") if isinstance(managed, dict) else None
    return github if isinstance(github, dict) else {}


def maybe_hold_github_push_locked() -> None:
    """Simulate the .github approval gate catching a push the moment the gate
    turns on: like the seeded tasks, this gives the UI a realistic held push to
    decide on without a real agent pushing."""
    github = github_integration_locked()
    if github.get("require_dot_github_approval") is not True:
        return
    repositories = github.get("write_repositories") or []
    if not repositories or any(push["status"] == "pending" for push in STATE.github_pending_pushes):
        return
    repository = repositories[0]
    STATE.github_pending_pushes.append({
        "id": f"{len(STATE.github_pending_pushes) + 1:04x}",
        "owner": repository.get("owner"),
        "repo": repository.get("repo"),
        "status": "pending",
        "requested_at": int(time.time()) - 40,
        "ref_updates": [{
            "ref": "refs/heads/agent/harden-ci",
            "old_oid": "9c1f7e2ab3d44a5f8e6b7c8d9e0f1a2b3c4d5e6f",
            "new_oid": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
        }],
        "changed_paths": [".github/workflows/deploy.yml", ".github/CODEOWNERS"],
    })


def decide_github_pending_push(push_id: str, decision: str) -> dict[str, Any]:
    with STATE.lock:
        push = next((row for row in STATE.github_pending_pushes if row["id"] == push_id), None)
        if push is None:
            raise ApiError(HTTPStatus.NOT_FOUND, f"unknown pending push: {push_id}")
        if push["status"] != "pending":
            raise ApiError(HTTPStatus.CONFLICT, f"push-{push_id} is already {push['status']}")
        if decision == "approve":
            push["status"] = "approved"
            STATE.add_network_event(
                "POST", "github.com", f"/{push['owner']}/{push['repo']}.git/git-receive-pack", "allowed"
            )
        else:
            push["status"] = "rejected"
        return {"pending_push": dict(push)}


def complete_due_codex_login_locked() -> None:
    """Complete a pending Codex device login once its out-of-band approval
    window has passed; runs on every status read, like the real poller."""
    record = STATE.codex_oauth
    if not record or STATE.logged_in.get("codex") or record.get("_completes_at", 0) > time.time():
        return
    STATE.logged_in["codex"] = True
    STATE.codex_oauth = {}
    STATE.add_agent_event("agent_runtime.login_completed", None, {"agent_runtime": "codex"})
    STATE.add_agent_event("agent_runtime.active", None, {"agent_runtime": "codex"})
    start_queued_tasks_locked()


def fail_running_tasks_locked(runtime: str, error_message: str) -> None:
    now = STATE.now()
    for task in STATE.tasks:
        if task["agent_runtime"] == runtime and task["status"] == "running":
            task["status"] = "failed"
            task["error_message"] = error_message
            task["updated_at"] = now
            STATE.add_agent_event("task.failed", task["task_id"], {"error_message": error_message})


def task_duration_seconds(task_id: str) -> float:
    number = int(task_id.rsplit("_", 1)[-1]) if task_id.rsplit("_", 1)[-1].isdigit() else 0
    return 8.0 + (number * 3) % 7


def start_queued_tasks_locked() -> None:
    """Claim queued tasks the way the real orchestrator does.

    One task per thread at a time, oldest first, capped per runtime and in
    total. Claimed tasks run for several seconds; ``progress_running_tasks_locked``
    moves them along on subsequent polls.
    """
    running_threads = {task["thread_id"] for task in STATE.tasks if task["status"] == "running"}
    running_by_runtime = {runtime: 0 for runtime in RUNTIMES}
    for task in STATE.tasks:
        if task["status"] == "running":
            running_by_runtime[task["agent_runtime"]] += 1
    for task in STATE.tasks:
        if task["status"] != "queued" or STATE.runtime_status(task["agent_runtime"]) != "active":
            continue
        if task["thread_id"] in running_threads:
            continue
        if running_by_runtime[task["agent_runtime"]] >= MAX_RUNNING_PER_RUNTIME:
            continue
        if sum(running_by_runtime.values()) >= MAX_RUNNING_TOTAL:
            break
        now = STATE.now()
        task["status"] = "running"
        task["started_at"] = now
        task["updated_at"] = now
        task["_started_monotonic"] = time.monotonic()
        task["_progress_emitted"] = 0
        running_threads.add(task["thread_id"])
        running_by_runtime[task["agent_runtime"]] += 1
        STATE.add_agent_event("task.started", task["task_id"], {})


def progress_running_tasks_locked() -> None:
    """Advance running tasks based on elapsed wall time.

    Emits the scripted progress messages (plus matching provider network
    events) as their milestones pass, then completes the task with an output
    that echoes the request.
    """
    for task in STATE.tasks:
        if task["status"] != "running" or "_started_monotonic" not in task:
            continue
        duration = task_duration_seconds(task["task_id"])
        fraction = (time.monotonic() - task["_started_monotonic"]) / duration
        emitted = task["_progress_emitted"]
        for milestone, message in PROGRESS_SCRIPT[emitted:]:
            if fraction < milestone:
                break
            STATE.add_agent_event("task.message", task["task_id"], {"message": message, "source": "agent"})
            host, api_path = PROVIDER_TRAFFIC[task["agent_runtime"]]
            STATE.add_network_event("POST", host, api_path, "allowed")
            task["_progress_emitted"] += 1
        if fraction >= 1.0:
            now = STATE.now()
            summary = task["input_message"].strip().splitlines()[0][:80].rstrip(".")
            task["status"] = "completed"
            task["output_message"] = (
                f"Done: {summary}.\nChecks passed; see the thread events for the step-by-step log."
            )
            task["completed_at"] = now
            task["updated_at"] = now
            task.pop("_started_monotonic", None)
            task.pop("_progress_emitted", None)
            STATE.add_agent_event("task.completed", task["task_id"], {})


def since(query: dict[str, list[str]]) -> int:
    values = query.get("since", ["0"])
    try:
        return int(values[0])
    except ValueError:
        return 0


def one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    return values[0]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True, help="Local port to bind, for example 3100.")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Start pre-configured (GitHub enabled with repositories, an App credential, manual domain rules) "
        "for interactive exploration; the UI smoke needs the default empty state.",
    )
    return parser.parse_args(argv)


def seed_demo_state() -> None:
    """A configured host for interactive exploration: GitHub enabled with
    repositories cycling through every audit outcome, a GitHub App
    credential, and a couple of manual domain rules."""
    STATE.policy = {
        "network_integrations": {
            "github": {
                "enabled": True,
                "write_repositories": [
                    {"owner": "infiloop2", "repo": "trustyclaw"},
                    {"owner": "infiloop2", "repo": "infibot"},
                    {"owner": "infiloop2", "repo": "dotfiles"},
                    {"owner": "acme", "repo": "private-infra"},
                    {"owner": "acme", "repo": "docs-site"},
                    {"owner": "acme", "repo": "pages-source"},
                ],
            },
            "bedrock": {"enabled": True},
            "custom": {
                "domains": {
                    "api.example.com": {"allow_http_methods": ["GET", "HEAD"], "path_guards": ["^/v1(?:/.*)?$"]},
                    "*.assets.example.com": {"allow_http_methods": ["GET"]},
                },
            },
        },
    }
    STATE.github_credential = {
        "mode": "app",
        "app_id": "12345",
        "installation_id": "67890",
        "app_token_expires_at": "2026-07-06T23:59:00Z",
        "updated_at": STATE.now(),
        "validation": {"status": "ok", "checked_at": STATE.now()},
    }
    STATE.bedrock_access_key_id = "AKIAMOCKOPERATOR0001"
    STATE.bedrock_region = "us-east-1"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    seed_state()
    if args.demo:
        seed_demo_state()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    actual_host, actual_port = server.server_address
    print(f"TrustyClaw mock admin UI: http://{actual_host}:{actual_port}/")
    print(f"Admin password: {PASSWORD}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
