const pending = new Map();
let nextRequestId = 1;
let threads = [];
let tasks = [];
let selectedThreadId = null;
let selectedThreadRuntime = null;
let selectedThreadModel = null;
let selectedThreadEffort = null;
let sessionOptions = {};
// Render guards: the 5-second poll re-renders only when data actually
// changed, so a steering draft or the reading scroll position survives
// refreshes that bring nothing new.
let renderedThreadsKey = null;
let renderedHistoryKey = null;
let renderedHistoryThread = null;
let forceScrollBottom = false;

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
const relativeTime = value => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || "");
  const minutes = Math.round((Date.now() - date.getTime()) / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
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
  const key = JSON.stringify([selectedThreadId, threads]);
  if (key === renderedThreadsKey) return;
  renderedThreadsKey = key;
  if (!threads.length) {
    $("threads").innerHTML = `<div class="sidebar-empty">No threads yet. Send a task below to start one.</div>`;
    return;
  }
  $("threads").innerHTML = threads.map(thread => {
    const active = (thread.active_tasks || []).length > 0;
    const count = `${thread.task_count} task${thread.task_count === 1 ? "" : "s"}`;
    return `
    <button class="thread-item${thread.thread_id === selectedThreadId ? " selected" : ""}" data-thread-id="${esc(thread.thread_id)}" data-runtime="${esc(thread.agent_runtime)}" data-model="${esc(thread.model)}" data-effort="${esc(thread.effort)}">
      <span class="thread-name"><span>${esc(thread.thread_id)}</span>${active ? `<span class="thread-dot running"></span>` : ""}</span>
      <span class="thread-meta">${esc(runtimeLabel(thread.agent_runtime))} &middot; ${esc(thread.model)}</span>
      <span class="thread-meta">${esc(count)} &middot; ${esc(relativeTime(thread.last_used_at))}</span>
    </button>`;
  }).join("");
}

function updateComposer() {
  const hasThread = selectedThreadId !== null;
  $("thread-title").textContent = hasThread ? selectedThreadId : "New thread";
  const subtitle = hasThread
    ? `${runtimeLabel(selectedThreadRuntime)} · ${selectedThreadModel} · ${optionLabel(selectedThreadEffort)}`
    : "";
  $("thread-subtitle").textContent = subtitle;
  $("thread-subtitle").hidden = !subtitle;
  $("archive-thread").hidden = !hasThread;
  // Follow-up tasks reuse the thread's stored session configuration, so the
  // pills only show while composing the first task of a new thread.
  $("new-task-thread").hidden = hasThread;
  $("new-task-runtime").hidden = hasThread;
  $("new-task-model").hidden = hasThread;
  $("new-task-effort").hidden = hasThread;
  $("new-task").placeholder = hasThread ? "Describe what the agent should do next" : "Describe a task for the agent";
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
  const key = JSON.stringify([selectedThreadId, tasks]);
  if (key === renderedHistoryKey) return;
  renderedHistoryKey = key;
  const switched = renderedHistoryThread !== selectedThreadId;
  renderedHistoryThread = selectedThreadId;
  if (!selectedThreadId) {
    $("thread-detail").innerHTML = `
      <div class="chat-hero">
        <h2>What should the agent work on?</h2>
        <p>Each message starts a task in this thread; follow-ups reuse the same agent session. Pick a runtime and model below, then press Enter to send.</p>
      </div>`;
    return;
  }
  const scroller = $("chat-scroll");
  const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 60;
  // Keep an in-progress steering draft (and its focus) across the re-render.
  const steerDrafts = new Map();
  let focusedSteerTask = null;
  document.querySelectorAll(".task-steer-input").forEach(input => {
    if (input.value) steerDrafts.set(input.dataset.taskId, input.value);
    if (input === document.activeElement) focusedSteerTask = input.dataset.taskId;
  });
  const ordered = tasks.slice().sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  $("thread-detail").innerHTML = ordered.length
    ? ordered.map(renderTurn).join("")
    : `<div class="chat-hero"><p>No retained tasks for this thread yet.</p></div>`;
  document.querySelectorAll(".task-steer-input").forEach(input => {
    const draft = steerDrafts.get(input.dataset.taskId);
    if (draft) input.value = draft;
    if (input.dataset.taskId === focusedSteerTask) input.focus();
  });
  if (switched || nearBottom || forceScrollBottom) scroller.scrollTop = scroller.scrollHeight;
  forceScrollBottom = false;
}

function renderTurn(task) {
  return `
    <article class="turn">
      <div class="turn-user"><div class="bubble"><pre>${esc(task.input_message)}</pre></div></div>
      <div class="turn-meta">
        ${task.status === "completed" ? "" : badge(task.status)}
        <span class="mono">${esc(task.task_id)}</span>
        <span title="${esc(formatDateTime(task.created_at))}">${esc(relativeTime(task.created_at))}</span>
        ${task.status === "queued" ? `<button class="ghost sm" data-task-action="cancel" data-task-id="${esc(task.task_id)}">Cancel</button>` : ""}
        ${task.status === "running" ? `<button class="danger ghost sm" data-task-action="kill" data-task-id="${esc(task.task_id)}">Stop</button>` : ""}
      </div>
      ${task.output_message ? `<div class="turn-agent"><pre>${esc(task.output_message)}</pre></div>` : ""}
      ${task.error_message ? `<div class="turn-error"><pre>${esc(task.error_message)}</pre></div>` : ""}
      ${task.status === "running" ? `
        <div class="task-steer">
          <input class="task-steer-input" data-task-id="${esc(task.task_id)}" placeholder="Steer this task" aria-label="Steering message for ${esc(task.task_id)}">
          <button class="ghost sm" data-task-action="steer" data-task-id="${esc(task.task_id)}">Steer</button>
        </div>` : ""}
    </article>`;
}

async function createTask() {
  const message = $("new-task").value.trim();
  const threadId = $("new-task-thread").value.trim();
  const runtime = $("new-task-runtime").value;
  const model = $("new-task-model").value;
  const effort = $("new-task-effort").value;
  if (!message || !threadId || !model || !effort || $("create-task").disabled) return;
  const request = { input_message: message, thread_id: threadId };
  if (selectedThreadId === null) Object.assign(request, { agent_runtime: runtime, model, effort });
  const task = await api("POST", "/tasks", request);
  $("new-task").value = "";
  autosizeComposer();
  selectedThreadId = threadId;
  selectedThreadRuntime = task.agent_runtime;
  selectedThreadModel = task.model;
  selectedThreadEffort = task.effort;
  forceScrollBottom = true;
  updateComposer();
  await refresh();
}

async function taskAction(button) {
  const taskId = button.dataset.taskId;
  const action = button.dataset.taskAction;
  if (action === "cancel") {
    await api("POST", `/tasks/${taskId}/cancel`);
    await refreshSelectedThread();
  } else if (action === "kill") {
    if (!confirm("Stop running task " + taskId + "?")) return;
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
  startNewThread();
  await refresh();
}

function startNewThread() {
  selectedThreadId = null;
  selectedThreadRuntime = null;
  selectedThreadModel = null;
  selectedThreadEffort = null;
  tasks = [];
  updateComposer();
  renderThreadHistory();
  renderThreads();
}

// Must match the drawer breakpoint in agent_chat.css.
const drawerMedia = window.matchMedia("(max-width: 720px)");

function setSidebarOpen(open, restoreFocus = false) {
  const mobile = drawerMedia.matches;
  const isOpen = mobile && open;
  const pane = document.querySelector(".thread-pane");
  $("chat-app").classList.toggle("sidebar-open", isOpen);
  // The closed drawer is only moved off-canvas by a transform, so drop it
  // (and, while open, the pane behind it) from the tab order the same way
  // the host mobile nav does.
  pane.inert = mobile && !isOpen;
  document.querySelector(".chat-main").inert = isOpen;
  $("sidebar-backdrop").hidden = !isOpen;
  $("sidebar-open").setAttribute("aria-expanded", String(isOpen));
  if (isOpen) $("sidebar-close").focus();
  else if (restoreFocus && mobile) $("sidebar-open").focus();
}

function autosizeComposer() {
  const area = $("new-task");
  area.style.height = "auto";
  area.style.height = `${Math.min(area.scrollHeight, 200)}px`;
}

document.addEventListener("click", event => {
  const thread = event.target.closest && event.target.closest(".thread-item");
  if (thread) {
    setSidebarOpen(false);
    showThread(thread.dataset.threadId, thread.dataset.runtime, thread.dataset.model, thread.dataset.effort).catch(error => setStatus(error.message));
    return;
  }
  const taskButton = event.target.closest && event.target.closest("button[data-task-action]");
  if (taskButton) taskAction(taskButton).catch(error => setStatus(error.message));
});

document.addEventListener("keydown", event => {
  if (event.key !== "Enter" || event.shiftKey) return;
  const steerInput = event.target.closest && event.target.closest(".task-steer-input");
  if (!steerInput) return;
  event.preventDefault();
  const steerButton = steerInput.closest(".task-steer").querySelector("button[data-task-action=steer]");
  taskAction(steerButton).catch(error => setStatus(error.message));
});

$("new-thread").addEventListener("click", () => {
  setSidebarOpen(false);
  $("new-task-thread").value = "main";
  $("new-task-runtime").value = "codex";
  setSessionOptions();
  startNewThread();
  $("new-task").focus();
});
$("archive-thread").addEventListener("click", () => archiveSelectedThread().catch(error => setStatus(error.message)));
$("create-task").addEventListener("click", () => createTask().catch(error => setStatus(error.message)));
$("new-task-runtime").addEventListener("change", () => setSessionOptions());
$("new-task-model").addEventListener("change", () => setSessionOptions($("new-task-model").value));
$("new-task").addEventListener("input", autosizeComposer);
$("new-task").addEventListener("keydown", event => {
  const sendKey = event.key === "Enter" && !event.isComposing && (!event.shiftKey || event.metaKey || event.ctrlKey);
  if (!sendKey) return;
  event.preventDefault();
  createTask().catch(error => setStatus(error.message));
});
$("sidebar-open").addEventListener("click", () => setSidebarOpen(true));
$("sidebar-close").addEventListener("click", () => setSidebarOpen(false, true));
$("sidebar-backdrop").addEventListener("click", () => setSidebarOpen(false, true));
drawerMedia.addEventListener("change", () => setSidebarOpen(false));

setSessionOptions();
updateComposer();
renderThreadHistory();
autosizeComposer();
setSidebarOpen(false);
refresh();
setInterval(refresh, 5000);
