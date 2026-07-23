const pending = new Map();
let nextRequestId = 1;
let threads = [];
let tasks = [];
// The selected thread's accumulated task events (its full message stream),
// fetched forward-paged by seq; EVENTS_PAGE mirrors the host page size.
let threadEvents = [];
let threadEventsSeq = 0;
const EVENTS_PAGE = 100;
let selectedThreadId = null;
let selectedThreadRuntime = null;
let selectedThreadModel = null;
let selectedThreadEffort = null;
let sessionOptions = {};
let pendingAttachments = [];
let attachmentActivity = null;
const ATTACHMENT_LIMIT = 10;
const ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024;
// Render guards: the 5-second poll re-renders only when data actually
// changed, so a steering draft or the reading scroll position survives
// refreshes that bring nothing new.
let renderedThreadsKey = null;
let renderedHistoryKey = null;
let renderedHistoryThread = null;
// Per-task rendered HTML, so a poll only patches turns that actually changed.
const renderedTurnHtml = new Map();
let forceScrollBottom = false;

const $ = id => document.getElementById(id);
const runtimeLabel = runtime => runtime === "claude_code" ? "Claude Code" : runtime === "codex" ? "Codex" : runtime === "hermes" ? "Hermes" : runtime;
const optionLabel = value => value.split(/[-_]/).map(part => part.charAt(0).toUpperCase() + part.slice(1)).join(" ");
const modelLabel = (runtime, value) => runtime === "codex" ? value : optionLabel(value);
const esc = value => {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
};
const escAttr = value => esc(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
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
  if (!message || ![
    "trustyclaw-app-api-result",
    "trustyclaw-app-upload-file-result",
  ].includes(message.type)) return;
  const callbacks = pending.get(message.request_id);
  if (!callbacks) return;
  pending.delete(message.request_id);
  if (message.ok) callbacks.resolve(message.cancelled ? null : message.body);
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

function requestFileUpload(action, selectionId, maximumFiles) {
  const requestId = String(nextRequestId++);
  parent.postMessage({
    type: "trustyclaw-app-upload-file",
    request_id: requestId,
    action,
    ...(selectionId ? { selection_id: selectionId } : {}),
    ...(maximumFiles ? { max_files: maximumFiles } : {}),
  }, "*");
  return new Promise((resolve, reject) => {
    pending.set(requestId, { resolve, reject });
    setTimeout(() => {
      if (!pending.has(requestId)) return;
      pending.delete(requestId);
      reject(new Error("file operation timed out"));
    }, 5 * 60 * 1000);
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
  // pills only show while composing the first task of a new thread. The
  // thread id itself is backend-generated, never typed.
  $("new-task-runtime").hidden = hasThread;
  $("new-task-model").hidden = hasThread;
  $("new-task-effort").hidden = hasThread;
  $("new-task").placeholder = hasThread ? "Describe what the agent should do next" : "Describe a task for the agent";
  if (hasThread) {
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
    updateComposerActions();
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
  updateComposerActions();
}

function updateComposerActions() {
  const hasSessionOption = Boolean($("new-task-model").value && $("new-task-effort").value);
  const hasOversizedAttachment = pendingAttachments.some(attachment => attachment.size_bytes > ATTACHMENT_MAX_BYTES);
  $("create-task").disabled = attachmentActivity !== null || hasOversizedAttachment || !hasSessionOption;
  $("attach-file").disabled = attachmentActivity !== null || pendingAttachments.length >= ATTACHMENT_LIMIT;
}

function renderAttachments() {
  const container = $("attachments");
  container.hidden = attachmentActivity === null && !pendingAttachments.length;
  container.innerHTML = [
    ...pendingAttachments.map(attachment => {
      const tooLarge = attachment.size_bytes > ATTACHMENT_MAX_BYTES;
      return `
        <div class="attachment${tooLarge ? " invalid" : ""}">
          <span class="attachment-name" title="${escAttr(attachment.original_name)}">${esc(attachment.original_name)}</span>
          ${tooLarge ? `<span class="attachment-error">25 MiB max</span>` : ""}
          <button
            class="attachment-clear"
            data-remove-attachment="${escAttr(attachment.selection_id)}"
            aria-label="Remove ${escAttr(attachment.original_name)}"
            title="Remove ${escAttr(attachment.original_name)}"
            ${attachmentActivity !== null ? "disabled" : ""}
          >&times;</button>
        </div>`;
    }),
    attachmentActivity === null ? "" : `<div class="attachment activity"><span>${esc(attachmentActivity)}</span></div>`,
  ].join("");
  updateComposerActions();
}

async function attachFile() {
  const remaining = ATTACHMENT_LIMIT - pendingAttachments.length;
  if (remaining <= 0) return;
  attachmentActivity = "Selecting file…";
  renderAttachments();
  try {
    const response = await requestFileUpload("select", null, remaining);
    if (response === null) return;
    if (!Array.isArray(response.selections) || !response.selections.length) {
      throw new Error("file selection returned an invalid response");
    }
    for (const selection of response.selections) {
      if (
        typeof selection.selection_id !== "string" ||
        typeof selection.original_name !== "string" ||
        typeof selection.size_bytes !== "number"
      ) {
        throw new Error("file selection returned an invalid response");
      }
    }
    if (pendingAttachments.length + response.selections.length > ATTACHMENT_LIMIT) {
      throw new Error(`You can attach up to ${ATTACHMENT_LIMIT} files.`);
    }
    pendingAttachments.push(...response.selections);
  } finally {
    attachmentActivity = null;
    renderAttachments();
  }
}

async function removeAttachment(selectionId) {
  const index = pendingAttachments.findIndex(attachment => attachment.selection_id === selectionId);
  if (index < 0) return;
  const [attachment] = pendingAttachments.splice(index, 1);
  renderAttachments();
  if (!attachment.file) {
    await requestFileUpload("discard", attachment.selection_id);
  }
}

async function showThread(threadId, runtime, model, effort) {
  selectedThreadId = threadId;
  selectedThreadRuntime = runtime;
  selectedThreadModel = model;
  selectedThreadEffort = effort;
  threadEvents = [];
  threadEventsSeq = 0;
  updateComposer();
  renderThreads();
  await refreshSelectedThread();
}

async function refreshSelectedThread() {
  if (!selectedThreadId) {
    renderThreadHistory();
    return;
  }
  // Capture the id: a thread switch mid-flight must not let a stale response
  // land in the newly selected thread's state.
  const threadId = selectedThreadId;
  const response = await api("GET", `/threads/${encodeURIComponent(threadId)}/tasks`);
  if (threadId !== selectedThreadId) return;
  tasks = response.tasks || [];
  await drainThreadEvents(threadId);
  if (threadId !== selectedThreadId) return;
  renderThreadHistory();
}

async function drainThreadEvents(threadId) {
  // Forward-paged accumulation: each page picks up after the last seen seq,
  // so the first open drains the backlog and later polls fetch only news.
  for (;;) {
    const response = await api(
      "GET",
      `/threads/${encodeURIComponent(threadId)}/events?since=${threadEventsSeq}`,
    );
    if (threadId !== selectedThreadId) return;
    const events = response.events || [];
    // Only accept events past the cursor, so a server that re-sends an
    // overlapping page can never double-append into the stream.
    const fresh = events.filter(event => event.seq > threadEventsSeq);
    if (fresh.length) {
      threadEvents.push(...fresh);
      threadEventsSeq = fresh[fresh.length - 1].seq;
    }
    // Keep paging only while the cursor advanced by a full page; a short or
    // no-progress page means the backlog is drained (and prevents a loop if a
    // server ignores `since` and keeps returning the same rows).
    if (fresh.length < EVENTS_PAGE) return;
  }
}

function renderThreadHistory() {
  const key = JSON.stringify([selectedThreadId, tasks, threadEventsSeq]);
  if (key === renderedHistoryKey) return;
  renderedHistoryKey = key;
  const switched = renderedHistoryThread !== selectedThreadId;
  renderedHistoryThread = selectedThreadId;
  const detail = $("thread-detail");
  if (!selectedThreadId) {
    renderedTurnHtml.clear();
    detail.innerHTML = `
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
  if (switched || !ordered.length) {
    renderedTurnHtml.clear();
    detail.innerHTML = ordered.length
      ? ""
      : `<div class="chat-hero"><p>No retained tasks for this thread yet.</p></div>`;
  }
  // Patch turns in place instead of rebuilding the whole history: a poll that
  // brings a task-field change only touches that task's article, so an
  // in-flight touch scroll (and its momentum) survives the refresh.
  const messagesByTask = new Map();
  for (const event of threadEvents) {
    if (event.event_type !== "task.message") continue;
    if (!messagesByTask.has(event.task_id)) messagesByTask.set(event.task_id, []);
    messagesByTask.get(event.task_id).push(event);
  }
  if (ordered.length) {
    ordered.forEach((task, index) => {
      const html = renderTurn(task, messagesByTask.get(task.task_id) || []);
      const current = detail.children[index];
      if (current && current.dataset.taskId === task.task_id) {
        if (renderedTurnHtml.get(task.task_id) !== html) detail.replaceChild(turnElement(html), current);
      } else {
        detail.insertBefore(turnElement(html), current || null);
      }
      renderedTurnHtml.set(task.task_id, html);
    });
    while (detail.children.length > ordered.length) {
      renderedTurnHtml.delete(detail.lastElementChild.dataset.taskId);
      detail.lastElementChild.remove();
    }
  }
  document.querySelectorAll(".task-steer-input").forEach(input => {
    const draft = steerDrafts.get(input.dataset.taskId);
    if (draft) input.value = draft;
    if (input.dataset.taskId === focusedSteerTask) input.focus();
  });
  // Instant jump when landing in a thread; smooth glide when the operator
  // just sent a message; stick to the bottom while reading there.
  if (switched) scroller.scrollTop = scroller.scrollHeight;
  else if (forceScrollBottom) scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
  else if (nearBottom) scroller.scrollTop = scroller.scrollHeight;
  forceScrollBottom = false;
}

function turnElement(html) {
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  return template.content.firstElementChild;
}

function renderTurn(task, messages) {
  // The full message stream renders inline: the runtime echoes the input as
  // the task's first user message, so that one is skipped (the bubble above
  // already shows it); later user messages are steering; agent messages are
  // interim progress. The stored output renders only when the stream's last
  // agent message is not already the same text.
  const stream = [];
  let inputEchoSkipped = false;
  let lastAgentText = null;
  for (const event of messages) {
    const text = event.payload && event.payload.message;
    if (typeof text !== "string" || !text) continue;
    if (event.payload.source === "user") {
      if (!inputEchoSkipped && text === task.input_message) {
        inputEchoSkipped = true;
        continue;
      }
      stream.push(`<div class="turn-user"><div class="bubble steer-bubble"><pre>${esc(text)}</pre></div></div>`);
    } else {
      stream.push(`<div class="turn-agent"><pre>${esc(text)}</pre></div>`);
      lastAgentText = text;
    }
  }
  const output = task.output_message && task.output_message !== lastAgentText
    ? `<div class="turn-agent"><pre>${esc(task.output_message)}</pre></div>`
    : "";
  return `
    <article class="turn" data-task-id="${esc(task.task_id)}">
      <div class="turn-user"><div class="bubble"><pre>${esc(task.input_message)}</pre></div></div>
      <div class="turn-meta">
        ${task.status === "completed" ? "" : badge(task.status)}
        <span class="mono">${esc(task.task_id)}</span>
        <span title="${esc(formatDateTime(task.created_at))}">${esc(relativeTime(task.created_at))}</span>
        ${task.status === "queued" ? `<button class="ghost sm" data-task-action="cancel" data-task-id="${esc(task.task_id)}">Cancel</button>` : ""}
        ${task.status === "running" ? `<button class="danger ghost sm" data-task-action="kill" data-task-id="${esc(task.task_id)}">Stop</button>` : ""}
      </div>
      ${stream.join("")}
      ${output}
      ${task.error_message ? `<div class="turn-error"><pre>${esc(task.error_message)}</pre></div>` : ""}
      ${task.status === "running" && task.agent_runtime !== "hermes" ? `
        <div class="task-steer">
          <input class="task-steer-input" data-task-id="${esc(task.task_id)}" placeholder="Steer this task" aria-label="Steering message for ${esc(task.task_id)}">
          <button class="ghost sm" data-task-action="steer" data-task-id="${esc(task.task_id)}">Steer</button>
        </div>` : ""}
    </article>`;
}

async function createTask() {
  const message = $("new-task").value.trim();
  const runtime = $("new-task-runtime").value;
  const model = $("new-task-model").value;
  const effort = $("new-task-effort").value;
  if ((!message && !pendingAttachments.length) || !model || !effort || $("create-task").disabled) return;
  // A request without thread_id asks the backend to open a new thread with a
  // generated successive name (thread-1, thread-2, ...).
  const startingNewThread = selectedThreadId === null;
  const request = { input_message: "" };
  if (startingNewThread) Object.assign(request, { agent_runtime: runtime, model, effort });
  else request.thread_id = selectedThreadId;
  for (const [index, attachment] of pendingAttachments.entries()) {
    if (attachment.file) continue;
    attachmentActivity = `Uploading ${index + 1} of ${pendingAttachments.length}…`;
    renderAttachments();
    try {
      const response = await requestFileUpload("upload", attachment.selection_id);
      if (!response.file || typeof response.file.path !== "string" || typeof response.file.name !== "string") {
        throw new Error("file upload returned an invalid response");
      }
      attachment.file = response.file;
    } finally {
      attachmentActivity = null;
      renderAttachments();
    }
  }
  const uploadedFiles = pendingAttachments.map(attachment => attachment.file);
  const fileReferences = uploadedFiles
    .map(file => `[User-uploaded file: ${file.path}]`)
    .join("\n");
  const inputMessage = uploadedFiles.length
    ? `${message || (uploadedFiles.length === 1 ? "Please review the uploaded file." : "Please review the uploaded files.")}\n\n${fileReferences}`
    : message;
  request.input_message = inputMessage;
  const task = await api("POST", "/tasks", request);
  $("new-task").value = "";
  pendingAttachments = [];
  renderAttachments();
  autosizeComposer();
  if (startingNewThread) {
    // A brand-new thread has no prior event stream to keep; start its
    // accumulator clean so the first poll drains only this task's events.
    threadEvents = [];
    threadEventsSeq = 0;
  }
  selectedThreadId = task.thread_id;
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
  threadEvents = [];
  threadEventsSeq = 0;
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
  if (taskButton) {
    taskAction(taskButton).catch(error => setStatus(error.message));
    return;
  }
  const removeAttachmentButton = event.target.closest && event.target.closest("button[data-remove-attachment]");
  if (removeAttachmentButton) {
    removeAttachment(removeAttachmentButton.dataset.removeAttachment).catch(error => setStatus(error.message));
  }
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
  $("new-task-runtime").value = "codex";
  setSessionOptions();
  startNewThread();
  $("new-task").focus();
});
$("archive-thread").addEventListener("click", () => archiveSelectedThread().catch(error => setStatus(error.message)));
$("create-task").addEventListener("click", () => createTask().catch(error => setStatus(error.message)));
$("attach-file").addEventListener("click", () => attachFile().catch(error => setStatus(error.message)));
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
renderAttachments();
setSidebarOpen(false);
refresh();
setInterval(refresh, 5000);
