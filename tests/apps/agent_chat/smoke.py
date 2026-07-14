"""Agent Chat smoke-test backend and UI checks."""

from __future__ import annotations

from http import HTTPStatus
import re
from typing import Any, Callable

from host.session_options import public_session_options


ApiErrorFactory = Callable[[HTTPStatus, str], Exception]
HostApi = Callable[[str, str, dict[str, list[str]], Any], dict[str, Any]]
AGENT_CHAT_THREADS: dict[str, dict[str, Any]] = {
    "website-redesign": {
        "thread_id": "website-redesign",
        "archived": False,
    }
}
AGENT_CHAT_TASK_IDS: dict[str, str] = {"task_3": "website-redesign", "task_4": "website-redesign"}


def route_app_api(
    method: str,
    relative: str,
    body: Any,
    api_error: ApiErrorFactory,
    host_api: HostApi,
) -> dict[str, Any]:
    if method == "GET" and relative == "session-options":
        return {"session_options": public_session_options()}
    if method == "GET" and relative == "threads":
        return {"threads": _list_threads(host_api)}
    match = re.fullmatch(r"threads/([^/]+)/tasks", relative)
    if method == "GET" and match:
        thread_id = match.group(1)
        _require_agent_chat_thread(thread_id, api_error)
        known = {task_id for task_id, known_thread_id in AGENT_CHAT_TASK_IDS.items() if known_thread_id == thread_id}
        tasks = host_api("GET", f"/v1/threads/{thread_id}/tasks", {}, None)["tasks"]
        return {"tasks": [task for task in tasks if task["task_id"] in known]}
    match = re.fullmatch(r"threads/([^/]+)/archive", relative)
    if method == "POST" and match:
        thread_id = match.group(1)
        _require_agent_chat_thread(thread_id, api_error, include_archived=True)
        AGENT_CHAT_THREADS[thread_id]["archived"] = True
        return {"thread": dict(AGENT_CHAT_THREADS[thread_id])}
    if method == "POST" and relative == "tasks":
        if (
            isinstance(body, dict)
            and body.get("thread_id") in AGENT_CHAT_THREADS
            and any(field in body for field in ("agent_runtime", "model", "effort"))
        ):
            raise api_error(HTTPStatus.BAD_REQUEST, "follow-up must use the stored session configuration")
        task = host_api("POST", "/v1/tasks", {}, body)
        thread_id = task["thread_id"]
        AGENT_CHAT_THREADS[thread_id] = {
            "thread_id": thread_id,
            "archived": False,
        }
        AGENT_CHAT_TASK_IDS[task["task_id"]] = thread_id
        return task
    match = re.fullmatch(r"tasks/([^/]+)(?:/(cancel|kill|steer))?", relative)
    if match:
        task_id, action = match.groups()
        if task_id not in AGENT_CHAT_TASK_IDS:
            raise api_error(HTTPStatus.NOT_FOUND, "task not found")
        suffix = "" if action is None else f"/{action}"
        return host_api(method, f"/v1/tasks/{task_id}{suffix}", {}, body)
    raise api_error(HTTPStatus.NOT_FOUND, "mock app route not found")


def desktop_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    expect(page.locator("#sidebar-apps")).to_contain_text("Apps")
    page.locator("#app-tabs").get_by_role("button", name="Agent Chat", exact=True).click()
    expect(page.locator("#panel-app-agent_chat")).to_be_visible()
    frame = page.frame_locator('iframe[title="Agent Chat"]')

    expect(frame.locator(".app-frame-title")).to_have_text("Agent Chat")
    expect(frame.locator("#status")).to_be_hidden()
    expect(frame.locator("#threads")).to_contain_text("website-redesign")

    frame.locator("#threads .thread-item", has_text="website-redesign").click()
    expect(frame.locator("#thread-detail")).to_contain_text("website-redesign")
    expect(frame.locator("#thread-detail")).to_contain_text("denied by policy")
    expect(frame.locator("#thread-detail .task-card").nth(0)).to_contain_text("Audit the marketing site")

    frame.get_by_role("button", name="+ New thread").click()
    expect(frame.locator("#thread-detail .thread-title")).to_have_text("New thread")
    frame.locator("#new-task").fill("agent app smoke task")
    frame.locator("#new-task-thread").fill("agent-app-smoke")
    frame.locator("#new-task-runtime").select_option("codex")
    expect(frame.locator("#new-task-model option")).to_have_count(3)
    frame.locator("#new-task-model").select_option("gpt-5.6-luna")
    expect(frame.locator("#new-task-effort option")).to_have_count(2)
    expect(frame.locator("#new-task-effort")).not_to_contain_text("Ultra")
    frame.locator("#new-task-effort").select_option("max")
    frame.get_by_role("button", name="Create task").click()
    expect(frame.locator("#thread-detail")).to_contain_text("agent-app-smoke")
    expect(frame.locator("#thread-detail")).to_contain_text("agent app smoke task")
    expect(frame.locator("#thread-detail")).to_contain_text("task_")
    frame.locator("#new-task").fill("agent app smoke follow up")
    frame.get_by_role("button", name="Create task").click()
    expect(frame.locator("#thread-detail")).to_contain_text("agent app smoke follow up")
    frame.get_by_role("button", name="Archive").click()
    expect(frame.locator("#thread-detail .thread-title")).to_have_text("New thread")
    expect(frame.locator("#threads")).not_to_contain_text("agent-app-smoke")


def mobile_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    expect(page.locator("#sidebar-apps")).to_contain_text("Apps")
    page.locator("#mobile-nav-toggle").click()
    expect(page.locator("#nav-backdrop")).to_be_visible()
    page.locator("#app-tabs").get_by_role("button", name="Agent Chat", exact=True).click()
    expect(page.locator("#nav-backdrop")).to_be_hidden()
    expect(page.locator("#panel-app-agent_chat")).to_be_visible()
    frame = page.frame_locator('iframe[title="Agent Chat"]')
    expect(frame.locator("#threads")).to_contain_text("website-redesign")
    frame.locator("#threads .thread-item", has_text="website-redesign").click()
    expect(frame.locator("#thread-detail")).to_contain_text("denied by policy")
    _assert_frame_no_horizontal_overflow(frame, "agent_chat app")


def _list_threads(host_api: HostApi) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    for app_thread in AGENT_CHAT_THREADS.values():
        if app_thread["archived"]:
            continue
        known = {task_id for task_id, known_thread_id in AGENT_CHAT_TASK_IDS.items() if known_thread_id == app_thread["thread_id"]}
        host_tasks = host_api("GET", f"/v1/threads/{app_thread['thread_id']}/tasks", {}, None)["tasks"]
        tasks = [task for task in host_tasks if task["task_id"] in known]
        if not tasks:
            continue
        configs = {
            (task["agent_runtime"], task["model"], task["effort"])
            for task in tasks
        }
        if len(configs) != 1:
            raise AssertionError(f"host returned inconsistent thread configuration: {tasks}")
        runtime, model, effort = configs.pop()
        timestamps = [
            str(task.get("updated_at") or task.get("created_at"))
            for task in tasks
            if isinstance(task.get("updated_at") or task.get("created_at"), str)
        ]
        threads.append(
            {
                "thread_id": app_thread["thread_id"],
                "agent_runtime": runtime,
                "model": model,
                "effort": effort,
                "last_used_at": max(timestamps, default=""),
                "task_count": len(tasks),
                "active_tasks": [
                    {"task_id": task["task_id"], "status": task["status"]}
                    for task in tasks
                    if task.get("status") in {"queued", "running"}
                ],
            }
        )
    return sorted(threads, key=lambda item: item["last_used_at"], reverse=True)


def _require_agent_chat_thread(
    thread_id: str,
    api_error: ApiErrorFactory,
    *,
    include_archived: bool = False,
) -> None:
    thread = AGENT_CHAT_THREADS.get(thread_id)
    if thread is None or (thread["archived"] and not include_archived):
        raise api_error(HTTPStatus.NOT_FOUND, "thread not found")


def _assert_frame_no_horizontal_overflow(frame: Any, label: str) -> None:
    overflow = frame.locator("html").evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    if overflow > 1:
        raise AssertionError(f"{label} frame overflows horizontally by {overflow}px")
