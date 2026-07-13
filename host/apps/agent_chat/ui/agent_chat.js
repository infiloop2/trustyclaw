const pending = new Map();
let nextRequestId = 1;
let threads = [];
let tasks = [];
let selectedThreadId = null;
let selectedThreadRuntime = null;
let selectedThreadModel = null;
let selectedThreadEffort = null;
let sessionOptions = {};

const $ = id => document.getElementById(id);
const runtimeLabel = runtime => runtime === "claude_code" ? "Claude Code" : runtime === "codex" ? "Codex" : runtime;
const optionLabel = value => value.split(/[-_]/).map(part => part.charAt(0).toUpperCase() + part.slice(1)).join(" ");
const modelLabel = (runtime, value) => runtime === "codex" ? value : optionLabel(value);
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
  // An empty message means healthy: hide the banner. A non-empty message is
  // an error to show.
  $("status").hidden = !message;
  $("status").textContent = message;
}

async function refresh() {
  try {
    if (!Object.keys(sessionOptions).length) {
      const optionResponse = await api("GET", "/session-options");
      if (!optionResponse.session_options || typeof optionResponse.session_options !== "object") {
        throw new Error("Agent Chat returned invalid session options");
      }
      sessionOptions = optionResponse.session_options;
      setSessionOptions(selectedThreadModel, selectedThreadEffort);
    }
    // A successful /threads already proves the backend is up; when it is down
    // the bridge's own 502 ("app backend unavailable") surfaces as the error.
    const response = await api("GET", "/threads");
    threads = response.threads || [];
    renderThreads();
    if (selectedThreadId) await refreshSelectedThread();
    setStatus("");
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
    <button class="thread-item${thread.thread_id === selectedThreadId ? " selected" : ""}" data-thread-id="${esc(thread.thread_id)}" data-runtime="${esc(thread.agent_runtime)}" data-model="${esc(thread.model)}" data-effort="${esc(thread.effort)}">
      <span class="thread-name">${esc(thread.thread_id)}</span>
      <span class="thread-meta">${esc(runtimeLabel(thread.agent_runtime))} &middot; ${esc(thread.model)} &middot; ${esc(optionLabel(thread.effort))}</span>
      <span class="thread-meta">${esc(thread.task_count)} task${thread.task_count === 1 ? "" : "s"}
        ${(thread.active_tasks || []).map(task => badge(task.status)).join(" ")}</span>
      <span class="thread-meta">${esc(formatDateTime(thread.last_used_at))}</span>
    </button>`).join("");
}

function updateComposer() {
  const hasThread = selectedThreadId !== null;
  $("thread-field").hidden = hasThread;
  $("runtime-field").hidden = hasThread;
  $("model-field").hidden = hasThread;
  $("effort-field").hidden = hasThread;
  document.querySelector(".composer").classList.toggle("new-thread", !hasThread);
  $("composer-target").textContent = hasThread ? "New task" : "New thread";
  $("new-task").placeholder = hasThread ? "Describe what the agent should do next" : "Describe the first task in this thread";
  if (hasThread) {
    $("new-task-thread").value = selectedThreadId;
    $("new-task-runtime").value = selectedThreadRuntime;
    setSessionOptions(selectedThreadModel, selectedThreadEffort);
  }
}

function setSessionOptions(preferredModel, preferredEffort) {
  const runtime = $("new-task-runtime").value;
  const models = sessionOptions[runtime] || {};
  const modelValues = Object.keys(models);
  if (!modelValues.length) {
    $("new-task-model").innerHTML = "";
    $("new-task-effort").innerHTML = "";
    $("new-task-model").disabled = true;
    $("new-task-effort").disabled = true;
    $("create-task").disabled = true;
    return;
  }
  const model = preferredModel && models[preferredModel] ? preferredModel : modelValues[0];
  $("new-task-model").innerHTML = modelValues
    .map(value => `<option value="${esc(value)}">${esc(modelLabel(runtime, value))}</option>`)
    .join("");
  $("new-task-model").value = model;
  const efforts = models[model];
  const effort = preferredEffort && efforts.includes(preferredEffort) ? preferredEffort : efforts[0];
  $("new-task-effort").innerHTML = efforts
    .map(value => `<option value="${esc(value)}">${esc(optionLabel(value))}</option>`)
    .join("");
  $("new-task-effort").value = effort;
  $("new-task-model").disabled = false;
  $("new-task-effort").disabled = false;
  $("create-task").disabled = false;
}

async function showThread(threadId, runtime, model, effort) {
  selectedThreadId = threadId;
  selectedThreadRuntime = runtime;
  selectedThreadModel = model;
  selectedThreadEffort = effort;
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
      <span class="muted">${esc(runtimeLabel(selectedThreadRuntime))} &middot; ${esc(selectedThreadModel)} &middot; ${esc(optionLabel(selectedThreadEffort))}</span>
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
  const model = $("new-task-model").value;
  const effort = $("new-task-effort").value;
  if (!message || !threadId || !model || !effort) return;
  const request = { input_message: message, thread_id: threadId };
  if (selectedThreadId === null) Object.assign(request, { agent_runtime: runtime, model, effort });
  const task = await api("POST", "/tasks", request);
  $("new-task").value = "";
  selectedThreadId = threadId;
  selectedThreadRuntime = task.agent_runtime;
  selectedThreadModel = task.model;
  selectedThreadEffort = task.effort;
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
  selectedThreadModel = null;
  selectedThreadEffort = null;
  tasks = [];
  updateComposer();
  renderThreadHistory();
  await refresh();
}

document.addEventListener("click", event => {
  const thread = event.target.closest && event.target.closest(".thread-item");
  if (thread) {
    showThread(thread.dataset.threadId, thread.dataset.runtime, thread.dataset.model, thread.dataset.effort).catch(error => setStatus(error.message));
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
  selectedThreadModel = null;
  selectedThreadEffort = null;
  tasks = [];
  $("new-task-thread").value = "main";
  $("new-task-runtime").value = "codex";
  setSessionOptions();
  updateComposer();
  renderThreadHistory();
  renderThreads();
  $("new-task-thread").focus();
});
$("create-task").addEventListener("click", () => createTask().catch(error => setStatus(error.message)));
$("new-task-runtime").addEventListener("change", () => setSessionOptions());
$("new-task-model").addEventListener("change", () => setSessionOptions($("new-task-model").value));
$("new-task").addEventListener("keydown", event => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") createTask().catch(error => setStatus(error.message));
});

setSessionOptions();
updateComposer();
renderThreadHistory();
refresh();
setInterval(refresh, 5000);
