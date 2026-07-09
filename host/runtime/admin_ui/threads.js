// Read-only agent session view: thread list, thread history with task cards,
// and per-task event tailing.

import { api } from "./api.js";
import { $, badge, esc, formatDateTime, runtimeLabel, setHtml } from "./helpers.js";

const TASK_EVENT_PAGE_BATCH = 10;

let threads = [], threadTasks = [];
let selectedThreadId = null, selectedThreadRuntime = null;
let expandedTaskEvents = new Map();

export async function loadThreads() {
  const listed = await api("GET", "/v1/threads");
  threads = listed.threads || [];
  renderThreads();
}

function renderThreads() {
  if (!threads.length) {
    setHtml($("threads"), `<div class="empty-state">No retained sessions yet.</div>`);
    return;
  }
  setHtml($("threads"), threads.map(thread => `
    <button class="thread-item${thread.thread_id === selectedThreadId ? " selected" : ""}"
            data-action="show-thread" data-thread-id="${esc(thread.thread_id)}" data-runtime="${esc(thread.agent_runtime)}">
      <span class="thread-name">${esc(thread.thread_id)}</span>
      <span class="thread-meta">${esc(runtimeLabel(thread.agent_runtime))} &middot; ${esc(thread.task_count)} task${thread.task_count === 1 ? "" : "s"}
        ${(thread.active_tasks || []).map(task => badge(task.status)).join(" ")}</span>
      <span class="thread-meta">${esc(formatDateTime(thread.last_used_at))}</span>
    </button>`).join(""));
}

export async function showThread(threadId, agentRuntime) {
  if (threadId !== selectedThreadId) clearTaskEventsDetail();
  selectedThreadId = threadId;
  selectedThreadRuntime = agentRuntime;
  renderThreads();
  await refreshSelectedThread();
}

export async function refreshSelectedThread() {
  if (selectedThreadId === null) { renderThreadHistory(); return; }
  const response = await api(
    "GET",
    `/v1/threads/${encodeURIComponent(selectedThreadId)}/tasks`
  );
  threadTasks = response.tasks || [];
  renderThreadHistory();
}

function taskNumber(taskId) {
  const tail = String(taskId).split("_").pop();
  return /^\d+$/.test(tail) ? Number(tail) : 0;
}

function taskRecency(task) {
  return task.updated_at || task.created_at || "";
}

export function renderThreadHistory() {
  if (selectedThreadId === null) {
    setHtml($("thread-detail"), `
      <div class="thread-head">
        <span class="thread-title">Agent session log</span>
      </div>
      <div class="empty-state thread-empty">Select a session to inspect retained tasks and events.</div>`);
    return;
  }
  const ordered = threadTasks.slice().sort((a, b) =>
    taskRecency(a) > taskRecency(b) ? -1
      : taskRecency(a) < taskRecency(b) ? 1
        : taskNumber(b.task_id) - taskNumber(a.task_id));
  setHtml($("thread-detail"), `
    <div class="thread-head">
      <span class="thread-title">${esc(selectedThreadId)}</span>
      <span class="muted">${esc(runtimeLabel(selectedThreadRuntime))}</span>
    </div>
    ${ordered.length ? ordered.map(renderTaskCard).join("")
      : `<div class="empty-state thread-empty">No retained tasks for this session yet.</div>`}`);
}

function renderTaskCard(task) {
  const eventState = expandedTaskEvents.get(task.task_id);
  const expanded = Boolean(eventState);
  const actions = `<button class="ghost sm" data-action="show-task-events" data-task-id="${esc(task.task_id)}" aria-expanded="${expanded}">${expanded ? "Hide events" : "Events"}</button>`;
  return `
    <div class="task-card">
      <div class="task-head">
        <span class="mono muted">${esc(task.task_id)}</span>
        ${badge(task.status)}
        <span class="muted time">${esc(formatDateTime(task.created_at))}</span>
        <span class="task-actions">${actions}</span>
      </div>
      <div class="msg user"><pre>${esc(task.input_message)}</pre></div>
      ${task.output_message ? `<div class="msg agent"><pre>${esc(task.output_message)}</pre></div>` : ""}
      ${task.error_message ? `<div class="msg error"><pre>${esc(task.error_message)}</pre></div>` : ""}
      ${eventState ? `<div class="task-events-inline">${taskEventsHtml(task, eventState)}</div>` : ""}
    </div>`;
}

async function loadTaskEventBatch(taskId, eventState) {
  for (let page = 0; page < TASK_EVENT_PAGE_BATCH; page += 1) {
    const response = await api(
      "GET",
      `/v1/tasks/${encodeURIComponent(taskId)}/events` +
        (eventState.since === null ? "" : "?since=" + eventState.since)
    );
    if (!response.events.length) return false;
    for (const event of response.events) {
      eventState.events.push(event);
      eventState.since = event.seq;
    }
  }
  return true;
}

export async function showTaskEvents(taskId) {
  if (expandedTaskEvents.has(taskId)) {
    expandedTaskEvents.delete(taskId);
    renderThreadHistory();
    return;
  }
  await refreshTaskEvents(taskId);
}

async function refreshTaskEvents(taskId) {
  if (selectedThreadId !== null) await refreshSelectedThread();
  const eventState = { events: [], since: null, hasMore: false };
  expandedTaskEvents.set(taskId, eventState);
  eventState.hasMore = await loadTaskEventBatch(taskId, eventState);
  renderTaskEventsDetail();
}

export async function loadMoreTaskEvents(taskId) {
  const eventState = expandedTaskEvents.get(taskId);
  if (!eventState) {
    await showTaskEvents(taskId);
    return;
  }
  eventState.hasMore = await loadTaskEventBatch(taskId, eventState);
  renderTaskEventsDetail();
}

function renderTaskEventsDetail() {
  renderThreadHistory();
  $("task-events-detail").innerHTML = "";
}

function taskEventsHtml(task, eventState) {
  return `
    <div class="table-scroll"><table>
      <tr><th>seq</th><th>time</th><th>type</th><th>source</th><th>payload</th></tr>
      ${eventState.events.length ? eventState.events.map(event => `
        <tr>
          <td>${esc(event.seq)}</td>
          <td class="muted time">${esc(formatDateTime(event.timestamp))}</td>
          <td>${esc(event.event_type)}</td>
          <td>${esc(event.payload.source || "")}</td>
          <td><pre>${esc(event.payload.message || event.payload.error_message || JSON.stringify(event.payload))}</pre></td>
        </tr>`).join("") : `<tr><td colspan="5" class="muted">No retained events for this task.</td></tr>`}
    </table></div>
    ${eventState.hasMore ? `<div class="actions"><button data-action="load-more-task-events" data-task-id="${esc(task.task_id)}">Load more events</button></div>` : ""}`;
}

function clearTaskEventsDetail() {
  expandedTaskEvents = new Map();
  $("task-events-detail").innerHTML = "";
}
