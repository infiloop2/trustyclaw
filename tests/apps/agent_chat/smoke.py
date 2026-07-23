"""Agent Chat smoke-test backend and UI checks."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

from host.session_options import public_session_options


ApiErrorFactory = Callable[[HTTPStatus, str], Exception]
HostApi = Callable[[str, str, dict[str, list[str]], Any], dict[str, Any]]
# Agent Chat surfaces a curated set of the host's seeded threads, a lived-in mix
# of runtimes and task states: a shipped infra fix (codex, two completed turns),
# a design audit with a failed deploy (claude_code), an active incident (codex,
# running), a cancelled dependency audit (codex), and a queued docs cleanup
# (claude_code). The app-facing thread id must equal the host thread id, since
# tasks and archive routes query the host by it directly.
AGENT_CHAT_THREADS: dict[str, dict[str, Any]] = {
    thread_id: {"thread_id": thread_id, "archived": False}
    for thread_id in ("website-redesign", "thread-1", "thread-2", "thread-3")
}
AGENT_CHAT_TASK_IDS: dict[str, str] = {
    "task_3": "website-redesign",
    "task_4": "website-redesign",
    "task_8": "thread-1",
    "task_9": "thread-1",
    "task_10": "thread-1",
    "task_11": "thread-1",
    "task_12": "thread-1",
    "task_13": "thread-2",
    "task_14": "thread-2",
    "task_15": "thread-3",
    "task_16": "thread-3",
}


def route_app_api(
    method: str,
    relative: str,
    query: dict[str, list[str]],
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
    match = re.fullmatch(r"threads/([^/]+)/events", relative)
    if method == "GET" and match:
        thread_id = match.group(1)
        _require_agent_chat_thread(thread_id, api_error)
        since = (query.get("since") or ["0"])[0]
        return host_api("GET", f"/v1/threads/{thread_id}/events", {"since": [since]}, None)
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
        if isinstance(body, dict) and "thread_id" not in body:
            # Mirror the real backend: no thread_id means a new thread with
            # the next successive generated name.
            body = {**body, "thread_id": _generate_thread_id()}
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

    # Agent Chat is the hero app: it sits directly below Home, outside the
    # Apps section, and the home tab opens with its Begin chat CTA.
    expect(page.locator("#home-hero")).to_contain_text("Agent Chat")
    expect(page.locator("#sidebar-apps")).to_contain_text("Apps")
    expect(page.locator("#stable-app-tabs").get_by_role("button", name="Agent Chat", exact=True)).to_have_count(0)
    expect(page.locator("#beta-app-tabs").get_by_role("button", name="Agent Chat", exact=True)).to_have_count(0)
    page.locator("#home-hero").get_by_role("button", name="Begin chat", exact=True).click()
    expect(page.locator("#panel-app-agent_chat")).to_be_visible()
    page.get_by_role("button", name="Home", exact=True).click()
    page.locator("#hero-app-tab").get_by_role("button", name="Agent Chat", exact=True).click()
    expect(page.locator("#panel-app-agent_chat")).to_be_visible()
    frame = page.frame_locator('iframe[title="Agent Chat"]')

    expect(frame.locator(".app-frame-title")).to_have_text("Agent Chat")
    expect(frame.locator("#status")).to_be_hidden()
    # A lived-in list: several threads across runtimes and task states.
    expect(frame.locator("#threads")).to_contain_text("website-redesign")
    expect(frame.locator("#threads")).to_contain_text("thread-1")

    frame.locator("#threads .thread-item", has_text="website-redesign").click()
    expect(frame.locator(".thread-title")).to_have_text("website-redesign")
    expect(frame.locator("#thread-detail")).to_contain_text("denied by policy")
    expect(frame.locator("#thread-detail .turn").nth(0)).to_contain_text("Audit the marketing site")

    frame.get_by_role("button", name="New thread").click()
    expect(frame.locator(".thread-title")).to_have_text("New thread")
    # The operator never types a thread id: the composer has no thread field
    # and the backend generates the next successive name on send.
    expect(frame.locator("#new-task-thread")).to_have_count(0)
    upload_requests = []
    page.on(
        "request",
        lambda request: upload_requests.append(request.url)
        if "/v1/agent-files/upload?" in request.url
        else None,
    )
    with page.expect_file_chooser() as chooser:
        frame.get_by_role("button", name="Attach files").click()
    chooser.value.set_files([
        {
            "name": "reference image.png",
            "mimeType": "image/png",
            "buffer": b"mock-image-bytes",
        },
        {
            "name": "notes.txt",
            "mimeType": "text/plain",
            "buffer": b"remove me",
        },
        {
            "name": "brief.pdf",
            "mimeType": "application/pdf",
            "buffer": b"mock-pdf-bytes",
        },
    ])
    expect(frame.locator("#attachments .attachment")).to_have_count(3)
    expect(frame.locator("#attachments")).to_contain_text("reference image.png")
    expect(frame.locator("#attachments")).to_contain_text("brief.pdf")
    frame.get_by_role("button", name="Remove notes.txt").click()
    expect(frame.locator("#attachments .attachment")).to_have_count(2)
    expect(frame.locator("#attachments")).not_to_contain_text("notes.txt")
    assert upload_requests == [], "selecting and removing attachments must not upload them before Send"
    frame.locator("#new-task").fill("agent app smoke task")
    frame.locator("#new-task-runtime").select_option("codex")
    expect(frame.locator("#new-task-model option")).to_have_count(3)
    frame.locator("#new-task-model").select_option("gpt-5.6-luna")
    expect(frame.locator("#new-task-effort option")).to_have_count(2)
    expect(frame.locator("#new-task-effort")).not_to_contain_text("Ultra")
    frame.locator("#new-task-effort").select_option("max")
    frame.get_by_role("button", name="Send").click()
    expect(frame.locator("#status")).to_contain_text("Service Unavailable")
    expect(frame.locator(".thread-title")).to_have_text("New thread")
    assert len(upload_requests) == 2, "the first Send must stop after the second attachment fails"
    frame.get_by_role("button", name="Send").click()
    expect(frame.locator(".thread-title")).to_have_text(re.compile(r"^thread-[0-9]+$"))
    assert len(upload_requests) == 3, "retry must upload only the unfinished attachment"
    assert sum("reference%20image.png" in url for url in upload_requests) == 1
    assert sum("brief.pdf" in url for url in upload_requests) == 2
    generated_thread = frame.locator(".thread-title").inner_text()
    expect(frame.locator("#thread-detail")).to_contain_text("agent app smoke task")
    expect(frame.locator("#thread-detail")).to_contain_text(
        "[User-uploaded file: user-files/20260722T120000.000000Z_reference image.png]"
    )
    expect(frame.locator("#thread-detail")).to_contain_text(
        "[User-uploaded file: user-files/20260722T120000.000000Z_brief.pdf]"
    )
    expect(frame.locator("#thread-detail")).not_to_contain_text("notes.txt")
    expect(frame.locator("#thread-detail")).to_contain_text("task_")
    with tempfile.TemporaryDirectory() as temporary_directory:
        oversized = Path(temporary_directory) / "oversized.bin"
        with oversized.open("wb") as file:
            file.truncate(25 * 1024 * 1024 + 1)
        with page.expect_file_chooser() as chooser:
            frame.get_by_role("button", name="Attach files").click()
        chooser.value.set_files(str(oversized))
        expect(frame.locator("#attachments")).to_contain_text("25 MiB max")
        expect(frame.get_by_role("button", name="Send")).to_be_disabled()
        assert len(upload_requests) == 3, "an oversized selection must not start an upload"
        frame.get_by_role("button", name="Remove oversized.bin").click()
        expect(frame.locator("#attachments")).to_be_hidden()
        expect(frame.get_by_role("button", name="Send")).to_be_enabled()
    with page.expect_file_chooser() as chooser:
        frame.get_by_role("button", name="Attach files").click()
    chooser.value.set_files(
        [
            {
                "name": f"extra-{index}.txt",
                "mimeType": "text/plain",
                "buffer": b"extra",
            }
            for index in range(11)
        ]
    )
    expect(frame.locator("#status")).to_have_text("You can attach up to 10 files.")
    expect(frame.locator("#attachments")).to_be_hidden()
    assert len(upload_requests) == 3, "too many selections must not start an upload"
    frame.locator("#new-task").fill("agent app smoke follow up")
    frame.get_by_role("button", name="Send").click()
    expect(frame.locator("#thread-detail")).to_contain_text("agent app smoke follow up")
    frame.get_by_role("button", name="Archive").click()
    expect(frame.locator(".thread-title")).to_have_text("New thread")
    expect(frame.locator("#threads")).not_to_contain_text(generated_thread)
    _assert_single_scroll(page, frame, "agent_chat app (desktop)")


def mobile_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    # The home hero navigator is the phone entry point into chat.
    expect(page.locator("#home-hero")).to_contain_text("Agent Chat")
    page.locator("#home-hero").get_by_role("button", name="Begin chat", exact=True).click()
    expect(page.locator("#panel-app-agent_chat")).to_be_visible()
    frame = page.frame_locator('iframe[title="Agent Chat"]')
    # The thread list sits behind a drawer on narrow viewports; while closed
    # it is off-canvas and must stay out of the tab order (inert).
    expect(frame.locator(".thread-pane")).to_have_js_property("inert", True)
    frame.get_by_role("button", name="Show thread list").click()
    expect(frame.locator(".thread-pane")).to_have_js_property("inert", False)
    expect(frame.locator("#threads")).to_contain_text("website-redesign")
    expect(frame.locator("#threads")).to_contain_text("thread-1")
    expect(frame.locator("#threads")).to_contain_text("thread-2")
    # Closing the drawer from the backdrop restores the chat pane.
    # The drawer is 304px wide inside a ~364px iframe: aim the tap at the
    # backdrop strip right of the drawer but inside the frame.
    frame.locator("#sidebar-backdrop").click(position={"x": 330, "y": 400})
    expect(frame.locator(".thread-pane")).to_have_js_property("inert", True)
    frame.get_by_role("button", name="Show thread list").click()
    frame.locator("#threads .thread-item", has_text="thread-1").click()
    expect(frame.locator(".thread-pane")).to_have_js_property("inert", True)
    expect(frame.locator("#thread-detail")).to_contain_text("tour of the codebase")
    _assert_frame_no_horizontal_overflow(frame, "agent_chat app")
    _assert_single_scroll(page, frame, "agent_chat app (mobile)")
    _assert_full_message_stream(frame)
    _assert_mobile_chat_scrolling(page, frame)
    _assert_mobile_composer_ergonomics(frame)
    _assert_mobile_steering(frame)
    _assert_mobile_send_flow(frame)


def _assert_full_message_stream(frame: Any) -> None:
    """A finished task renders its whole message stream inline, not just
    prompt and final answer: interim agent progress and mid-task operator
    steering both appear, the steering marked as a follow-up nudge."""
    from playwright.sync_api import expect

    flash_turn = frame.locator("#thread-detail .turn", has_text="Fix the flash")
    # Interim agent progress from the seeded stream.
    expect(flash_turn).to_contain_text("Reproduced the flash with CPU throttling")
    # The mid-task steer renders as a follow-up bubble on the user's side.
    steer = flash_turn.locator(".turn-user .steer-bubble", has_text="scrollbar matches")
    expect(steer).to_have_count(1)
    # The opening prompt is a plain bubble, not a steer bubble.
    opening = flash_turn.locator(".turn-user .bubble", has_text="Fix the flash")
    expect(opening).not_to_have_class(re.compile(r"steer-bubble"))
    # The final answer still lands after the interim stream.
    expect(flash_turn).to_contain_text("no flash in 20 cold loads")


def _assert_mobile_chat_scrolling(page: Any, frame: Any) -> None:
    """The long seeded thread must scroll freely on a phone and a background
    poll must not touch the DOM under the reader's finger."""
    from playwright.sync_api import expect

    scroller = frame.locator("#chat-scroll")
    expect(frame.locator("#thread-detail .turn")).to_have_count(5)
    metrics = scroller.evaluate(
        "element => [element.scrollHeight, element.clientHeight, element.scrollTop]"
    )
    scroll_height, client_height, scroll_top = metrics
    if scroll_height - client_height < client_height:
        raise AssertionError(
            f"seeded thread-1 is not long enough to exercise scrolling: {metrics}"
        )
    # Opening a thread lands at the newest turn.
    if scroll_top < scroll_height - client_height - 2:
        raise AssertionError(f"opening a thread did not land at the bottom: {metrics}")
    # The whole history is reachable: jump to the top and read the first turn.
    scroller.evaluate("element => { element.scrollTop = 0; }")
    expect(frame.locator("#thread-detail .turn").nth(0)).to_be_in_viewport()
    # Park mid-history, mark the first rendered turn, and sit through a full
    # 5-second poll: the scroll position must hold and the DOM node must be
    # the same object (an innerHTML rebuild would kill touch momentum).
    scroller.evaluate("element => { element.scrollTop = Math.floor(element.scrollHeight / 2); }")
    parked = scroller.evaluate("element => element.scrollTop")
    frame.locator("#thread-detail .turn").nth(0).evaluate("element => { element.dataset.smokeProbe = 'kept'; }")
    page.wait_for_timeout(6000)
    after = scroller.evaluate("element => element.scrollTop")
    if after != parked:
        raise AssertionError(f"a background poll moved the reading position: {parked} -> {after}")
    probe = frame.locator("#thread-detail .turn").nth(0).evaluate("element => element.dataset.smokeProbe")
    if probe != "kept":
        raise AssertionError("a background poll rebuilt the chat history DOM while reading")


def _assert_mobile_composer_ergonomics(frame: Any) -> None:
    """iOS-critical input ergonomics: 16px fields so focus does not zoom the
    page, and thumb-sized primary controls."""
    composer_font = frame.locator("#new-task").evaluate(
        "element => parseFloat(getComputedStyle(element).fontSize)"
    )
    if composer_font < 16:
        raise AssertionError(f"composer font below 16px zooms iOS on focus: {composer_font}px")
    send_box = frame.locator("#create-task").bounding_box()
    if not send_box or send_box["height"] < 43 or send_box["width"] < 43:
        raise AssertionError(f"send button is below thumb size on a phone: {send_box}")
    attach_box = frame.locator("#attach-file").bounding_box()
    if not attach_box or attach_box["height"] < 43 or attach_box["width"] < 43:
        raise AssertionError(f"attach button is below thumb size on a phone: {attach_box}")
    clipped = frame.locator(".composer").evaluate(
        "element => element.getBoundingClientRect().bottom > window.innerHeight + 1"
    )
    if clipped:
        raise AssertionError("composer is clipped below the app frame viewport")


def _assert_mobile_steering(frame: Any) -> None:
    """The seeded running task exposes steering inline; Enter submits and
    clears the field."""
    from playwright.sync_api import expect

    frame.get_by_role("button", name="Show thread list").click()
    frame.locator("#threads .thread-item", has_text="thread-2").click()
    expect(frame.locator("#thread-detail")).to_contain_text("launch blog post")
    running_turn = frame.locator("#thread-detail .turn", has_text="Tighten the intro")
    expect(running_turn.locator(".status")).to_have_text("running")
    steer_input = running_turn.locator(".task-steer-input")
    steer_font = steer_input.evaluate("element => parseFloat(getComputedStyle(element).fontSize)")
    if steer_font < 16:
        raise AssertionError(f"steer input font below 16px zooms iOS on focus: {steer_font}px")
    steer_input.fill("keep the beta tester thank-you")
    steer_input.press("Enter")
    expect(steer_input).to_have_value("")
    expect(running_turn.get_by_role("button", name="Stop")).to_be_visible()


def _assert_mobile_send_flow(frame: Any) -> None:
    """Starting a thread from a phone: no thread-id typing, generated name,
    the sent message lands in view at the bottom."""
    from playwright.sync_api import expect

    frame.get_by_role("button", name="Show thread list").click()
    frame.get_by_role("button", name="New thread").click()
    expect(frame.locator(".thread-title")).to_have_text("New thread")
    expect(frame.locator("#new-task-thread")).to_have_count(0)
    frame.locator("#new-task").fill("mobile smoke: check the deploy status")
    frame.get_by_role("button", name="Send").click()
    expect(frame.locator(".thread-title")).to_have_text(re.compile(r"^thread-[0-9]+$"))
    expect(frame.locator("#thread-detail")).to_contain_text("mobile smoke: check the deploy status")
    sent_bubble = frame.locator("#thread-detail .turn-user").last
    expect(sent_bubble).to_be_in_viewport()
    frame.get_by_role("button", name="Archive").click()
    expect(frame.locator(".thread-title")).to_have_text("New thread")


def _generate_thread_id() -> str:
    numbers = [
        int(match.group(1))
        for thread_id in AGENT_CHAT_THREADS
        if (match := re.fullmatch(r"thread-([1-9][0-9]*)", thread_id)) is not None
    ]
    return f"thread-{max(numbers, default=0) + 1}"


def _list_threads(host_api: HostApi) -> list[dict[str, Any]]:
    """Mirror the real backend: one bulk host summaries call, shown only for
    unarchived threads with recorded tasks, with count and active ids taken
    from the app's own recorded tasks (never the host's raw totals)."""
    recorded: dict[str, set[str]] = {}
    for task_id, thread_id in AGENT_CHAT_TASK_IDS.items():
        thread = AGENT_CHAT_THREADS.get(thread_id)
        if thread is not None and not thread["archived"]:
            recorded.setdefault(thread_id, set()).add(task_id)
    summaries = host_api("GET", "/v1/threads", {}, None)["threads"]
    threads = [
        {
            **summary,
            "task_count": len(recorded[summary["thread_id"]]),
            "active_tasks": [
                task for task in summary["active_tasks"] if task["task_id"] in recorded[summary["thread_id"]]
            ],
        }
        for summary in summaries
        if summary["thread_id"] in recorded
    ]
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


def _assert_single_scroll(page: Any, frame: Any, label: str) -> None:
    """With an app tab open, neither the outer page nor the iframe document
    may scroll vertically — only the app's internal panes do. This guards the
    nested page + iframe double-scrollbar regression."""
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


def _assert_frame_no_horizontal_overflow(frame: Any, label: str) -> None:
    overflow = frame.locator("html").evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    if overflow > 1:
        raise AssertionError(f"{label} frame overflows horizontally by {overflow}px")
