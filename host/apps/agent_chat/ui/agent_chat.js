const pending = new Map();
let nextRequestId = 1;
let threads = [];
let tasks = [];
let selectedThreadId = null;
let selectedThreadRuntime = null;

const $ = id => document.getElementById(id);
const runtimeLabel = runtime => runtime === "claude_code" ? "Claude Code" : runtime === "codex" ? "Codex" : runtime;
const esc = value => {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
};
const badge = value => `<span class="status ${esc(value)}">${esc(value)}</span>`;
const formatDateTime = value => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || "");
  return date.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
};

window.addEventListener("message", event => {
  const message = event.data;
  if (!message || message.type !== "trustyclaw-app-api-result") return;
  const callbacks = pending.get(message.request_id);
  if (!callbacks) return;
  pending.delete(message.request_id);
  if (message.ok) callbacks.resolve(message.body);
  else callbacks.reject(new Error(message.error || "request failed"));
});

function api(method, path, body) {
  if (!path.startsWith("/")) throw new Error("app API path must be absolute");
  const requestId = String(nextRequestId++);
  parent.postMessage({ type: "trustyclaw-app-api", request_id: requestId, method, path: "/v1/apps/agent_chat/api" + path, body }, "*");
  return new Promise((resolve, reject) => {
    pending.set(requestId, { resolve, reject });
    setTimeout(() => {
      if (!pending.has(requestId)) return;
      pending.delete(requestId);
      reject(new Error("request timed out"));
    }, 30000);
  });
}

function setStatus(message) {
  if (message === "Connected") {
    $("status").hidden = true;
    $("status").textContent = "";
    return;
  }
  $("status").hidden = false;
  $("status").textContent = message;
}

async function refresh() {
  try {
    const backend = await api("GET", "/health").catch(error => ({ status: "unavailable", detail: error.message }));
    const response = await api("GET", "/threads");
    threads = response.threads || [];
    renderThreads();
    if (selectedThreadId) await refreshSelectedThread();
    setStatus(backend.status === "ok" ? "Connected" : `Host connected; app backend ${backend.status}`);
  } catch (error) {
    setStatus(error.message);
  }
}

function renderThreads() {
  if (!threads.length) {
    $("threads").innerHTML = `<div class="empty-state">No threads yet. Start one on the right.</div>`;
    return;
  }
  $("threads").innerHTML = threads.map(thread => `
    <button class="thread-item${thread.thread_id === selectedThreadId ? " selected" : ""}" data-thread-id="${esc(thread.thread_id)}" data-runtime="${esc(thread.agent_runtime)}">
      <span class="thread-name">${esc(thread.thread_id)}</span>
      <span class="thread-meta">${esc(runtimeLabel(thread.agent_runtime))} &middot; ${esc(thread.task_count)} task${thread.task_count === 1 ? "" : "s"}
        ${(thread.active_tasks || []).map(task => badge(task.status)).join(" ")}</span>
      <span class="thread-meta">${esc(formatDateTime(thread.last_used_at))}</span>
    </button>`).join("");
}

function updateComposer() {
  const hasThread = selectedThreadId !== null;
  $("thread-field").hidden = hasThread;
  $("runtime-field").hidden = hasThread;
  document.querySelector(".composer").classList.toggle("new-thread", !hasThread);
  $("composer-target").textContent = hasThread ? "New task" : "New thread";
  $("new-task").placeholder = hasThread ? "Describe what the agent should do next" : "Describe the first task in this thread";
  if (hasThread) {
    $("new-task-thread").value = selectedThreadId;
    $("new-task-runtime").value = selectedThreadRuntime;
  }
}

async function showThread(threadId, runtime) {
  selectedThreadId = threadId;
  selectedThreadRuntime = runtime;
  updateComposer();
  renderThreads();
  await refreshSelectedThread();
}

async function refreshSelectedThread() {
  if (!selectedThreadId) {
    renderThreadHistory();
    return;
  }
  const response = await api("GET", `/threads/${encodeURIComponent(selectedThreadId)}/tasks`);
  tasks = response.tasks || [];
  renderThreadHistory();
}

function renderThreadHistory() {
  if (!selectedThreadId) {
    $("thread-detail").innerHTML = `
      <div class="thread-head">
        <span class="thread-kicker">Thread</span>
        <span class="thread-title">New thread</span>
      </div>`;
    return;
  }
  const ordered = tasks.slice().sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  $("thread-detail").innerHTML = `
    <div class="thread-head">
      <span class="thread-kicker">Thread</span>
      <span class="thread-title">${esc(selectedThreadId)}</span>
      <span class="muted">${esc(runtimeLabel(selectedThreadRuntime))}</span>
      <span class="task-actions">
        <button class="ghost sm" data-thread-action="archive">Archive</button>
      </span>
    </div>
    ${ordered.length ? ordered.map(renderTaskCard).join("") : `<div class="empty-state thread-empty">No retained tasks for this thread yet.</div>`}`;
}

function renderTaskCard(task) {
  const canRefresh = task.status === "running" || task.status === "queued";
  return `
    <div class="task-card">
      <div class="task-head">
        <span class="mono muted">${esc(task.task_id)}</span>
        ${badge(task.status)}
        <span class="muted time">${esc(formatDateTime(task.created_at))}</span>
        <span class="task-actions">
          ${task.status === "running" ? `<button class="danger sm" data-task-action="kill" data-task-id="${esc(task.task_id)}">Kill</button>` : ""}
          ${task.status === "queued" ? `<button class="ghost sm" data-task-action="cancel" data-task-id="${esc(task.task_id)}">Cancel</button>` : ""}
          ${canRefresh ? `<button class="ghost sm" data-task-action="refresh" data-task-id="${esc(task.task_id)}">Refresh</button>` : ""}
        </span>
      </div>
      <div class="msg user"><pre>${esc(task.input_message)}</pre></div>
      ${task.status === "running" ? `
        <div class="task-steer">
          <input class="task-steer-input" placeholder="Steer this task" aria-label="Steering message for ${esc(task.task_id)}">
          <button class="sm" data-task-action="steer" data-task-id="${esc(task.task_id)}">Steer</button>
        </div>` : ""}
      ${task.output_message ? `<div class="msg agent"><pre>${esc(task.output_message)}</pre></div>` : ""}
      ${task.error_message ? `<div class="msg error"><pre>${esc(task.error_message)}</pre></div>` : ""}
    </div>`;
}

async function createTask() {
  const message = $("new-task").value.trim();
  const threadId = $("new-task-thread").value.trim();
  const runtime = $("new-task-runtime").value;
  if (!message || !threadId) return;
  await api("POST", "/tasks", { input_message: message, thread_id: threadId, agent_runtime: runtime });
  $("new-task").value = "";
  selectedThreadId = threadId;
  selectedThreadRuntime = runtime;
  updateComposer();
  await refresh();
}

async function taskAction(button) {
  const taskId = button.dataset.taskId;
  const action = button.dataset.taskAction;
  if (action === "refresh") {
    await refreshSelectedThread();
  } else if (action === "cancel") {
    await api("POST", `/tasks/${taskId}/cancel`);
    await refreshSelectedThread();
  } else if (action === "kill") {
    if (!confirm("Kill running task " + taskId + "?")) return;
    await api("POST", `/tasks/${taskId}/kill`);
    await refreshSelectedThread();
  } else if (action === "steer") {
    const input = button.closest(".task-steer").querySelector(".task-steer-input");
    const message = input.value.trim();
    if (!message) return;
    await api("POST", `/tasks/${taskId}/steer`, { steer_message: message });
    input.value = "";
  }
}

async function archiveSelectedThread() {
  if (!selectedThreadId) return;
  await api("POST", `/threads/${encodeURIComponent(selectedThreadId)}/archive`);
  selectedThreadId = null;
  selectedThreadRuntime = null;
  tasks = [];
  updateComposer();
  renderThreadHistory();
  await refresh();
}

document.addEventListener("click", event => {
  const thread = event.target.closest && event.target.closest(".thread-item");
  if (thread) {
    showThread(thread.dataset.threadId, thread.dataset.runtime).catch(error => setStatus(error.message));
    return;
  }
  const threadButton = event.target.closest && event.target.closest("button[data-thread-action]");
  if (threadButton && threadButton.dataset.threadAction === "archive") {
    archiveSelectedThread().catch(error => setStatus(error.message));
    return;
  }
  const taskButton = event.target.closest && event.target.closest("button[data-task-action]");
  if (taskButton) taskAction(taskButton).catch(error => setStatus(error.message));
});

$("new-thread").addEventListener("click", () => {
  selectedThreadId = null;
  selectedThreadRuntime = null;
  tasks = [];
  $("new-task-thread").value = "main";
  updateComposer();
  renderThreadHistory();
  renderThreads();
  $("new-task-thread").focus();
});
$("create-task").addEventListener("click", () => createTask().catch(error => setStatus(error.message)));
$("new-task").addEventListener("keydown", event => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") createTask().catch(error => setStatus(error.message));
});

updateComposer();
renderThreadHistory();
refresh();
setInterval(refresh, 5000);
