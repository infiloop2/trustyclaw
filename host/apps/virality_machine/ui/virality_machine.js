const pending = new Map();
let nextRequestId = 1;
let snapshot = null;
let renderJobs = { render_jobs: [], total: 0, max: 200 };
let sessionOptions = {};
let draftSettings = null;
let openArtifactId = null;
let openArtifactUpdatedAt = null;
const fullMessages = new Map();

// Named artifacts the bespoke rail surfaces directly. Any other artifact the
// agent creates (starting with "ideas") shows up in the general Artifacts card.
const STORYBOARD_ID = "storyboard";
const PUBLISH_QUEUE_ID = "publish_queue";
const NAMED_ARTIFACT_IDS = new Set([STORYBOARD_ID, PUBLISH_QUEUE_ID]);

const $ = id => document.getElementById(id);
const runtimeLabel = runtime => runtime === "claude_code" ? "Claude Code" : runtime === "codex" ? "Codex" : runtime === "hermes" ? "Hermes" : runtime;
const optionLabel = value => String(value).split(/[-_]/).map(part => part.charAt(0).toUpperCase() + part.slice(1)).join(" ");
const modelLabel = (runtime, model) => runtime === "codex" ? model : optionLabel(model);
const formatDateTime = value => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || "");
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
};
const formatTime = value => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || "");
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
};

// Only the admin shell parent may answer bridge requests; agent-authored
// content never runs script in this frame, but the check keeps any future
// nested context from spoofing API results.
window.addEventListener("message", event => {
  if (event.source !== window.parent) return;
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
  parent.postMessage({ type: "trustyclaw-app-api", request_id: requestId, method, path: "/v1/apps/virality_machine/api" + path, body }, "*");
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
  if (!message) {
    $("status").hidden = true;
    $("status").textContent = "";
    return;
  }
  $("status").hidden = false;
  $("status").textContent = message;
}

const CONNECTION_LABELS = {
  ready: "Connections healthy",
  degraded: "Connections degraded",
  blocked: "Connections needed",
  unknown: "Connections unknown",
};

async function refreshConnections() {
  const pill = $("connections-pill");
  try {
    const report = await api("GET", "/connections");
    const status = CONNECTION_LABELS[report.status] ? report.status : "unknown";
    pill.textContent = CONNECTION_LABELS[status];
    pill.className = `connections-pill conn-${status}`;
    const lines = (report.tools || []).map(tool => {
      const suffix = tool.state === "ready" ? "ready" : `${tool.state}${tool.detail ? ` — ${tool.detail}` : ""}`;
      return `${tool.title}: ${suffix}`;
    });
    pill.title = lines.join("\n") || "No tools recorded yet";
    pill.hidden = false;
  } catch (error) {
    pill.hidden = true;
  }
}

async function refresh() {
  try {
    if (!Object.keys(sessionOptions).length) {
      const response = await api("GET", "/session-options");
      if (!response.session_options || typeof response.session_options !== "object") {
        throw new Error("Virality Machine returned invalid agent settings");
      }
      sessionOptions = response.session_options;
      activeRuntimes = Array.isArray(response.active_runtimes) ? response.active_runtimes : null;
      draftSettings = defaultAgentSettings();
    }
    snapshot = await api("GET", "/workspace");
    refreshConnections();
    if (snapshot.workspace.agent_runtime) {
      renderJobs = await api("GET", "/render_jobs");
      if ($("agent-settings-popover").hidden) draftSettings = workspaceAgentSettings();
    }
    render();
    setStatus("");
    await maybeRefreshOpenArtifact();
  } catch (error) {
    setStatus(error.message);
  }
}

function render() {
  if (!snapshot) return;
  const started = Boolean(snapshot.workspace.agent_runtime);
  const returning = !started && snapshot.messages.length > 0;
  $("hero").hidden = started;
  $("workspace").hidden = !started;
  $("deactivate-app").hidden = !started;
  $("hero-send").querySelector("span").textContent = returning ? "Reactivate" : "Activate";
  $("hero-send").setAttribute("aria-label", returning ? "Reactivate" : "Activate");
  $("hero-hint").textContent = returning
    ? "Reactivates the app. Existing schedules stay paused until you resume them."
    : "Starts the app and the routines above. The conversation begins whenever you send the first message.";
  renderAgentSettings();
  if (!started) return;
  renderGoal();
  renderFeed();
  renderBusy();
  renderStoryboard();
  renderRenderQueue();
  renderPublishQueue();
  renderArtifacts();
  renderSchedules();
  renderMemories();
  renderTools();
}

let activeRuntimes = null;

// Offer only activated providers in the runtime selector; if the host state
// is unknown or nothing is activated, fall back to every configured runtime
// (the connections pill carries the bad news instead of an empty selector).
function availableRuntimes() {
  const runtimes = Object.keys(sessionOptions);
  if (!Array.isArray(activeRuntimes)) return runtimes;
  const filtered = runtimes.filter(value => activeRuntimes.includes(value));
  return filtered.length ? filtered : runtimes;
}

function defaultAgentSettings() {
  const runtime = availableRuntimes()[0];
  const models = sessionOptions[runtime] || {};
  const model = Object.keys(models)[0];
  return { runtime, model, effort: (models[model] || [])[0] };
}

function workspaceAgentSettings() {
  return {
    runtime: snapshot.workspace.agent_runtime,
    model: snapshot.workspace.model,
    effort: snapshot.workspace.effort,
  };
}

function renderAgentSettings() {
  const settings = snapshot.workspace.agent_runtime ? workspaceAgentSettings() : draftSettings;
  const button = $("agent-settings-toggle");
  button.disabled = !settings || !settings.runtime || !settings.model || !settings.effort;
  button.textContent = button.disabled
    ? "Agent settings"
    : `${runtimeLabel(settings.runtime)} · ${modelLabel(settings.runtime, settings.model)} · ${optionLabel(settings.effort)}`;
  const busy = Boolean(snapshot.workspace.agent_runtime && (snapshot.busy || []).length);
  $("agent-settings-apply").disabled = busy || button.disabled;
  const selected = selectedAgentSettings();
  const changed = Boolean(snapshot.workspace.agent_runtime) && (
    selected.runtime !== settings.runtime ||
    selected.model !== settings.model ||
    selected.effort !== settings.effort
  );
  $("agent-settings-warning").hidden = !changed;
  $("agent-settings-note").textContent = busy
    ? "Finish or discard queued and running work before changing settings."
    : snapshot.workspace.agent_runtime
      ? "Changes apply from the next message."
      : "These settings start the conversation.";
}

function setAgentSettingsControls(preferred = {}) {
  const runtimes = availableRuntimes();
  const runtime = runtimes.includes(preferred.runtime) ? preferred.runtime : runtimes[0];
  $("agent-runtime").innerHTML = runtimes
    .map(value => `<option value="${escAttr(value)}">${esc(runtimeLabel(value))}</option>`)
    .join("");
  $("agent-runtime").value = runtime;

  const models = sessionOptions[runtime] || {};
  const modelValues = Object.keys(models);
  const model = modelValues.includes(preferred.model) ? preferred.model : modelValues[0];
  $("agent-model").innerHTML = modelValues
    .map(value => `<option value="${escAttr(value)}">${esc(modelLabel(runtime, value))}</option>`)
    .join("");
  $("agent-model").value = model;

  const efforts = models[model] || [];
  const effort = efforts.includes(preferred.effort) ? preferred.effort : efforts[0];
  $("agent-effort").innerHTML = efforts
    .map(value => `<option value="${escAttr(value)}">${esc(optionLabel(value))}</option>`)
    .join("");
  $("agent-effort").value = effort;
}

function selectedAgentSettings() {
  return {
    runtime: $("agent-runtime").value,
    model: $("agent-model").value,
    effort: $("agent-effort").value,
  };
}

function openAgentSettings() {
  const settings = snapshot.workspace.agent_runtime ? workspaceAgentSettings() : draftSettings;
  setAgentSettingsControls(settings || {});
  setHeaderPopover("agent-settings", true);
  renderAgentSettings();
}

function setHeaderPopover(name, open) {
  const pairs = [
    ["agent-settings", "agent-settings-toggle", "agent-settings-popover"],
    ["info", "info-toggle", "info-popover"],
  ];
  pairs.forEach(([pairName, triggerId, popoverId]) => {
    const expanded = pairName === name && open;
    $(popoverId).hidden = !expanded;
    $(triggerId).setAttribute("aria-expanded", String(expanded));
  });
}

function closeHeaderPopovers() {
  setHeaderPopover("", false);
}

async function applyAgentSettings() {
  const settings = selectedAgentSettings();
  if (snapshot.workspace.agent_runtime) {
    await api("POST", "/settings", {
      agent_runtime: settings.runtime,
      model: settings.model,
      effort: settings.effort,
    });
  } else {
    draftSettings = settings;
  }
  closeHeaderPopovers();
  await refresh();
}

function renderGoal() {
  const goal = snapshot.workspace.goal;
  const measurement = snapshot.workspace.measurement;
  $("goal-banner").hidden = !goal;
  $("goal-banner").innerHTML = goal
    ? `<span class="goal-kicker">Channel goal</span> ${esc(goal)}` +
      (measurement ? `<span class="goal-measurement"><span class="goal-kicker">Measured by</span> ${esc(measurement)}</span>` : "")
    : "";
}

function renderFeed() {
  const feed = $("feed");
  const stickToBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
  feed.innerHTML = snapshot.messages.map(renderMessage).join("") ||
    `<div class="empty-state">Say hello to get started.</div>`;
  if (stickToBottom) feed.scrollTop = feed.scrollHeight;
}

function renderMessage(message) {
  const time = `<span class="msg-time">${esc(formatTime(message.created_at))}</span>`;
  const full = fullMessages.get(message.id);
  const content = full != null ? full : message.content;
  const more = message.truncated && full == null
    ? `<button class="ghost sm msg-expand" data-expand-message="${esc(message.id)}">Show full message</button>`
    : "";
  if (message.role === "user") {
    const meta = message.meta || {};
    if (meta.action === "artifact_interaction") {
      const interaction = meta.control_type === "button"
        ? `Pressed ${meta.control_label}`
        : `${meta.control_label}: ${meta.control_type === "toggle" ? (meta.value ? "On" : "Off") : meta.value}`;
      return `<div class="msg-row user"><div class="msg user interaction-message">
        <span class="interaction-context">${esc(meta.artifact_title)}</span>
        <div class="msg-body">${esc(interaction)}</div>${time}
      </div></div>`;
    }
    return `<div class="msg-row user"><div class="msg user"><div class="msg-body">${mdLite(content)}</div>${more}${time}</div></div>`;
  }
  if (message.role === "agent") {
    return `<div class="msg-row agent"><div class="msg agent"><div class="msg-body">${mdLite(content)}</div>${more}${time}</div></div>`;
  }
  if (message.role === "error") {
    return `<div class="msg-row agent"><div class="msg error"><div class="msg-body">${esc(content)}</div>${more}${time}</div></div>`;
  }
  const meta = message.meta || {};
  const clickable = meta.artifact_id ? ` data-open-artifact="${esc(meta.artifact_id)}" role="button" tabindex="0"` : "";
  return `<div class="msg-row event"><span class="event-chip${meta.artifact_id ? " clickable" : ""}"${clickable}>` +
    `<svg width="12" height="12" viewBox="0 0 20 20" aria-hidden="true"><rect x="3" y="5" width="14" height="10" rx="2" fill="none" stroke="currentColor" stroke-width="2"/></svg>` +
    `${esc(message.content)}</span></div>`;
}

function renderBusy() {
  const busy = snapshot.busy || [];
  const bar = $("busy-bar");
  bar.hidden = busy.length === 0;
  if (!busy.length) return;
  const current = busy.find(run => run.status === "active") || busy[0];
  const kindText = current.kind === "schedule" ? "Running a scheduled turn" : "Agent is working";
  let text = current.host_status === "queued" || current.status === "pending" ? `${kindText} (queued)` : kindText;
  if (current.last_error) text = `Waiting to start: ${current.last_error}`;
  if (busy.length > 1) text += ` · ${busy.length - 1} more queued`;
  $("busy-text").textContent = text;
  const stop = $("stop-task");
  stop.hidden = !current.task_id && current.status !== "pending";
  stop.textContent = current.task_id ? "Stop" : "Discard";
  stop.dataset.taskId = current.task_id || "";
  stop.dataset.runId = String(current.run_id);
}

// --------------------------------------------------------------------------
// Domain panels

function namedArtifact(id) {
  return (snapshot.artifacts || []).find(artifact => artifact.artifact_id === id) || null;
}

function renderStoryboard() {
  const storyboard = namedArtifact(STORYBOARD_ID);
  if (!storyboard) {
    $("storyboard").innerHTML = `<div class="empty-state">No storyboard yet. Ask your agent to lay out the shots for a reel.</div>`;
    return;
  }
  $("storyboard").innerHTML = `
    <div class="rail-item clickable" data-open-artifact="${esc(storyboard.artifact_id)}" role="button" tabindex="0">
      <div class="rail-item-main">
        <span class="rail-item-title">${esc(storyboard.title)}</span>
        <span class="rail-item-sub">updated ${esc(formatDateTime(storyboard.updated_at))} · ${storyboard.has_view ? "open to edit shots" : "data only"}</span>
      </div>
      <span class="rail-item-chevron" aria-hidden="true">&rsaquo;</span>
    </div>`;
}

const RENDER_STATUS_CLASS = { succeeded: "ok", failed: "bad", cancelled: "bad", running: "warn", pending: "" };

function renderRenderQueue() {
  const jobs = renderJobs.render_jobs || [];
  $("render-count").textContent = renderJobs.total
    ? `${renderJobs.total} of ${renderJobs.max}`
    : "";
  if (!jobs.length) {
    $("render-queue").innerHTML = `<div class="empty-state">No render jobs yet. Runway generations the agent starts appear here with live status.</div>`;
    return;
  }
  $("render-queue").innerHTML = jobs.map(job => {
    const statusClass = RENDER_STATUS_CLASS[job.status] || "";
    const prompt = job.prompt ? `${esc(job.prompt)}${job.prompt_truncated ? "…" : ""}` : "";
    // The app sandbox cannot navigate to arbitrary agent-authored URLs. Keep
    // the exact temporary URL visible and provide an explicit copy action.
    const playback = job.video_path
      ? `<div class="render-playback"><button class="primary sm" data-open-video="${escAttr(job.video_path)}">View finished video</button><code class="render-url">${esc(job.video_path)}</code></div>`
      : "";
    const link = job.output_url && !job.video_path
      ? `<div class="render-link"><button class="ghost sm" data-copy-output="${escAttr(job.output_url)}">Copy temporary URL</button><code class="render-url">${esc(job.output_url)}</code></div>`
      : "";
    return `
    <div class="rail-item render-job">
      <div class="rail-item-main">
        <span class="rail-item-title mono">${esc(job.id)}</span>
        <span class="rail-item-sub">
          <span class="chip">${esc(job.kind)}</span>
          <span class="chip ${statusClass}">${esc(job.status)}</span>
          <span class="render-updated">updated ${esc(formatDateTime(job.updated_at))}</span>
        </span>
        ${prompt ? `<span class="rail-item-sub render-prompt">${prompt}</span>` : ""}
        ${playback}
        ${link}
      </div>
    </div>`;
  }).join("");
}

function renderPublishQueue() {
  const publish = namedArtifact(PUBLISH_QUEUE_ID);
  if (!publish) {
    $("publish-queue").innerHTML = `<div class="empty-state">No reels queued for publishing. Finished reels land here for approval-gated publishing to Instagram.</div>`;
    return;
  }
  $("publish-queue").innerHTML = `
    <div class="rail-item clickable" data-open-artifact="${esc(publish.artifact_id)}" role="button" tabindex="0">
      <div class="rail-item-main">
        <span class="rail-item-title">${esc(publish.title)}</span>
        <span class="rail-item-sub">updated ${esc(formatDateTime(publish.updated_at))} · publishing needs your approval</span>
      </div>
      <span class="rail-item-chevron" aria-hidden="true">&rsaquo;</span>
    </div>`;
}

function renderArtifacts() {
  const rows = (snapshot.artifacts || []).filter(artifact => !NAMED_ARTIFACT_IDS.has(artifact.artifact_id));
  if (!rows.length) {
    $("artifacts").innerHTML = `<div class="empty-state">No other artifacts yet. Ideas and reports appear here as your agent builds them.</div>`;
    return;
  }
  $("artifacts").innerHTML = rows.map(artifact => `
    <div class="rail-item clickable" data-open-artifact="${esc(artifact.artifact_id)}" role="button" tabindex="0">
      <div class="rail-item-main">
        <span class="rail-item-title">${esc(artifact.title)}</span>
        <span class="rail-item-sub">updated ${esc(formatDateTime(artifact.updated_at))} · ${artifact.has_view ? "view" : "data"}</span>
      </div>
      <span class="rail-item-chevron" aria-hidden="true">&rsaquo;</span>
    </div>`).join("");
}

function renderMemories() {
  const rows = snapshot.memories || [];
  if (!rows.length) {
    $("memories").innerHTML = `<div class="empty-state">Nothing remembered yet. Creative rules and durable channel facts appear here.</div>`;
    return;
  }
  $("memories").innerHTML = rows.map(memory => `
    <div class="rail-item memory">
      <div class="rail-item-main">
        <span class="rail-item-title mono">${esc(memory.memory_id)}</span>
        <span class="rail-item-sub memory-content">${esc(memory.content)}</span>
      </div>
      <span class="rail-item-actions">
        <button class="ghost sm" data-memory-edit="${esc(memory.memory_id)}">Edit</button>
        <button class="danger sm" data-memory-delete="${esc(memory.memory_id)}">Forget</button>
      </span>
    </div>`).join("");
}

async function editMemory(memoryId) {
  const memory = (snapshot.memories || []).find(entry => entry.memory_id === memoryId);
  const content = prompt(`Edit memory "${memoryId}" (max 300 chars):`, memory ? memory.content : "");
  if (content == null || !content.trim()) return;
  await api("POST", `/memories/${encodeURIComponent(memoryId)}`, { content: content.trim() });
  await refresh();
}

function cadenceText(schedule) {
  if (schedule.every_minutes == null) {
    return schedule.next_run_at ? `once at ${formatDateTime(schedule.next_run_at)}` : "once · already ran";
  }
  const minutes = schedule.every_minutes;
  if (minutes % 1440 === 0) return minutes === 1440 ? "every day" : `every ${minutes / 1440} days`;
  if (minutes % 60 === 0) return minutes === 60 ? "every hour" : `every ${minutes / 60} hours`;
  return `every ${minutes} minutes`;
}

function renderSchedules() {
  const rows = snapshot.schedules || [];
  if (!rows.length) {
    $("schedules").innerHTML = `<div class="empty-state">No schedules yet. Ask your agent to schedule a run.</div>`;
    return;
  }
  $("schedules").innerHTML = rows.map(schedule => {
    const state = schedule.enabled ? "" : " paused";
    const nextPart = schedule.enabled && schedule.next_run_at ? ` · next ${formatDateTime(schedule.next_run_at)}` : "";
    const last = schedule.last_run_status
      ? `<span class="chip ${schedule.last_run_status === "completed" ? "ok" : schedule.last_run_status === "failed" ? "bad" : ""}">last ${esc(schedule.last_run_status)}</span>`
      : "";
    const toggle = schedule.every_minutes == null && !schedule.next_run_at
      ? ""
      : `<button class="ghost sm" data-schedule-action="${schedule.enabled ? "disable" : "enable"}" data-schedule-id="${esc(schedule.schedule_id)}">${schedule.enabled ? "Pause" : "Resume"}</button>`;
    return `
    <div class="rail-item schedule${state}">
      <div class="rail-item-main">
        <span class="rail-item-title"><span class="state-dot${schedule.enabled ? " on" : ""}"></span>${esc(schedule.title)}</span>
        <span class="rail-item-sub">${esc(cadenceText(schedule))}${esc(nextPart)}</span>
        <span class="rail-item-sub">${last}${schedule.enabled ? "" : `<span class="chip">paused</span>`}</span>
      </div>
      <span class="rail-item-actions">
        ${toggle}
        <button class="danger sm" data-schedule-action="delete" data-schedule-id="${esc(schedule.schedule_id)}">Delete</button>
      </span>
    </div>`;
  }).join("");
}

const TOOL_STATUS_LABELS = { enabled: "enabled", implemented: "needs enabling", not_implemented: "not implemented" };

function renderTools() {
  const rows = snapshot.tools || [];
  if (!rows.length) {
    $("tools").innerHTML = `<div class="empty-state">No tools recorded. The agent fills this in during setup.</div>`;
    return;
  }
  $("tools").innerHTML = rows.map(tool => `
    <div class="rail-item">
      <div class="rail-item-main">
        <span class="rail-item-title">${esc(tool.title)}</span>
        <span class="rail-item-sub">
          <span class="chip ${tool.priority === "must_have" ? "bad" : ""}">${tool.priority === "must_have" ? "must have" : "good to have"}</span>
          <span class="chip ${tool.status === "enabled" ? "ok" : ""}">${esc(TOOL_STATUS_LABELS[tool.status] || tool.status)}</span>
        </span>
        ${tool.note ? `<span class="rail-item-sub">${esc(tool.note)}</span>` : ""}
      </div>
      <span class="rail-item-actions">
        <button class="danger sm" data-tool-delete="${esc(tool.tool_id)}">Delete</button>
      </span>
    </div>`).join("");
}

// --------------------------------------------------------------------------
// Artifact overlay and block renderer

async function openArtifact(artifactId) {
  try {
    const response = await api("GET", `/artifacts/${encodeURIComponent(artifactId)}`);
    const artifact = response.artifact;
    openArtifactId = artifact.artifact_id;
    openArtifactUpdatedAt = artifact.updated_at;
    $("artifact-title").textContent = artifact.title;
    $("artifact-updated").textContent = `updated ${formatDateTime(artifact.updated_at)}`;
    $("artifact-delete").dataset.artifactId = artifact.artifact_id;
    const blocks = artifact.view ? artifact.view.map(block => renderBlock(block, artifact.artifact_id)).join("") : "";
    $("artifact-body").innerHTML = blocks
      ? blocks
      : `<pre class="artifact-json">${esc(JSON.stringify(artifact.data, null, 2))}</pre>`;
    $("artifact-overlay").hidden = false;
    attachChartTooltips($("artifact-body"));
  } catch (error) {
    setStatus(error.message);
  }
}

function closeArtifact() {
  openArtifactId = null;
  openArtifactUpdatedAt = null;
  $("artifact-overlay").hidden = true;
}

async function maybeRefreshOpenArtifact() {
  if (!openArtifactId || !snapshot) return;
  const listed = (snapshot.artifacts || []).find(artifact => artifact.artifact_id === openArtifactId);
  if (!listed) {
    closeArtifact();
    return;
  }
  const focused = document.activeElement;
  if (focused && $("artifact-overlay").contains(focused) && focused.matches("input, textarea")) return;
  if (listed.updated_at !== openArtifactUpdatedAt) await openArtifact(openArtifactId);
}

// --------------------------------------------------------------------------
// Actions

async function activateWorkspace() {
  if (!draftSettings) throw new Error("Agent settings are not loaded yet");
  await api("POST", "/activate", {
    agent_runtime: draftSettings.runtime,
    model: draftSettings.model,
    effort: draftSettings.effort,
  });
  await refresh();
}

async function deactivateApp() {
  if (!confirm("Deactivate this app? This stops queued and active agent work and pauses every schedule. Workspace data is preserved.")) return;
  const result = await api("POST", "/deactivate");
  closeArtifact();
  closeHeaderPopovers();
  await refresh();
  setStatus(result.stopping_tasks
    ? `App deactivated. ${result.stopping_tasks} active agent turn is stopping; all schedules are paused.`
    : "App deactivated. All schedules are paused and workspace data is preserved.");
}

async function sendMessage() {
  const input = $("chat-input");
  const content = input.value.trim();
  if (!content) return;
  await api("POST", "/messages", { content });
  input.value = "";
  await refresh();
}

async function scheduleAction(button) {
  const scheduleId = button.dataset.scheduleId;
  const action = button.dataset.scheduleAction;
  if (action === "delete") {
    if (!confirm(`Delete schedule "${scheduleId}"?`)) return;
    await api("DELETE", `/schedules/${encodeURIComponent(scheduleId)}`);
  } else {
    await api("POST", `/schedules/${encodeURIComponent(scheduleId)}/${action}`);
  }
  await refresh();
}

async function expandMessage(messageId) {
  const response = await api("GET", `/messages/${encodeURIComponent(messageId)}`);
  const suffix = response.message.truncated ? "\n… (truncated)" : "";
  fullMessages.set(response.message.id, response.message.content + suffix);
  renderFeed();
}

async function submitArtifactInteraction(control, value) {
  const artifactId = control.dataset.artifactId;
  const controlId = control.dataset.controlId;
  if (!artifactId || !controlId) throw new Error("artifact control is missing its identity");
  const group = control.closest(".b-control") || control;
  const inputs = [...group.querySelectorAll("button, input")];
  inputs.forEach(input => { input.disabled = true; });
  group.setAttribute("aria-busy", "true");
  try {
    const result = await api("POST", "/interactions", {
      artifact_id: artifactId,
      control_id: controlId,
      value,
    });
    const submit = group.querySelector("button");
    if (submit && submit.matches("[data-field-submit]")) {
      submit.setAttribute("aria-label", "Field submitted");
      submit.title = "Field submitted";
    } else if (submit) {
      submit.textContent = "Sent";
    }
    await refresh();
    setStatus(result.steered ? "Sent to the agent's current turn." : "Queued for the agent.");
  } catch (error) {
    inputs.forEach(input => { input.disabled = false; });
    group.removeAttribute("aria-busy");
    throw error;
  }
}

document.addEventListener("click", event => {
  const videoButton = event.target.closest && event.target.closest("button[data-open-video]");
  if (videoButton) {
    parent.postMessage({ type: "trustyclaw-app-open-file", path: videoButton.dataset.openVideo }, "*");
    return;
  }
  const outputCopy = event.target.closest && event.target.closest("button[data-copy-output]");
  if (outputCopy) {
    navigator.clipboard.writeText(outputCopy.dataset.copyOutput)
      .then(() => setStatus("Output URL copied."))
      .catch(() => setStatus("Could not copy the output URL."));
    return;
  }
  const artifactButton = event.target.closest && event.target.closest("button[data-artifact-interaction]");
  if (artifactButton) {
    submitArtifactInteraction(artifactButton, true).catch(error => setStatus(error.message));
    return;
  }
  const fieldSubmit = event.target.closest && event.target.closest("button[data-field-submit]");
  if (fieldSubmit) {
    const field = fieldSubmit.closest(".b-field-control").querySelector("input[data-control-type='field']");
    submitArtifactInteraction(field, field.value).catch(error => setStatus(error.message));
    return;
  }
  const expander = event.target.closest && event.target.closest("[data-expand-message]");
  if (expander) {
    expandMessage(expander.dataset.expandMessage).catch(error => setStatus(error.message));
    return;
  }
  const opener = event.target.closest && event.target.closest("[data-open-artifact]");
  if (opener) {
    openArtifact(opener.dataset.openArtifact).catch(error => setStatus(error.message));
    return;
  }
  const scheduleButton = event.target.closest && event.target.closest("button[data-schedule-action]");
  if (scheduleButton) {
    scheduleAction(scheduleButton).catch(error => setStatus(error.message));
    return;
  }
  const memoryEdit = event.target.closest && event.target.closest("button[data-memory-edit]");
  if (memoryEdit) {
    editMemory(memoryEdit.dataset.memoryEdit).catch(error => setStatus(error.message));
    return;
  }
  const memoryDelete = event.target.closest && event.target.closest("button[data-memory-delete]");
  if (memoryDelete) {
    const memoryId = memoryDelete.dataset.memoryDelete;
    if (!confirm(`Forget memory "${memoryId}"?`)) return;
    api("DELETE", `/memories/${encodeURIComponent(memoryId)}`).then(refresh).catch(error => setStatus(error.message));
    return;
  }
  const toolDelete = event.target.closest && event.target.closest("button[data-tool-delete]");
  if (toolDelete) {
    const toolId = toolDelete.dataset.toolDelete;
    if (!confirm(`Delete tool "${toolId}" from the inventory?`)) return;
    api("DELETE", `/tools/${encodeURIComponent(toolId)}`).then(refresh).catch(error => setStatus(error.message));
    return;
  }
});

document.addEventListener("change", event => {
  const toggle = event.target.closest && event.target.closest("input[data-control-type='toggle']");
  if (!toggle) return;
  submitArtifactInteraction(toggle, toggle.checked).catch(error => {
    toggle.checked = !toggle.checked;
    setStatus(error.message);
  });
});

$("hero-send").addEventListener("click", () => activateWorkspace().catch(error => setStatus(error.message)));
$("deactivate-app").addEventListener("click", () => deactivateApp().catch(error => setStatus(error.message)));
$("chat-send").addEventListener("click", () => sendMessage().catch(error => setStatus(error.message)));
[["chat-input", false]].forEach(([id, fromHero]) => {
  $(id).addEventListener("keydown", event => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") sendMessage().catch(error => setStatus(error.message));
  });
});
$("stop-task").addEventListener("click", () => {
  const taskId = $("stop-task").dataset.taskId;
  const runId = $("stop-task").dataset.runId;
  if (taskId) {
    if (!confirm("Stop the agent's current turn?")) return;
    api("POST", `/tasks/${encodeURIComponent(taskId)}/stop`).then(refresh).catch(error => setStatus(error.message));
    return;
  }
  if (!runId || !confirm("Discard this queued turn?")) return;
  api("POST", `/runs/${encodeURIComponent(runId)}/discard`).then(refresh).catch(error => setStatus(error.message));
});
$("agent-settings-toggle").addEventListener("click", () => {
  if ($("agent-settings-popover").hidden) openAgentSettings();
  else closeHeaderPopovers();
});
$("agent-settings-cancel").addEventListener("click", closeHeaderPopovers);
$("agent-settings-apply").addEventListener("click", () => applyAgentSettings().catch(error => setStatus(error.message)));
$("agent-runtime").addEventListener("change", () => {
  setAgentSettingsControls({ runtime: $("agent-runtime").value });
  renderAgentSettings();
});
$("agent-model").addEventListener("change", () => {
  setAgentSettingsControls({
    runtime: $("agent-runtime").value,
    model: $("agent-model").value,
  });
  renderAgentSettings();
});
$("agent-effort").addEventListener("change", renderAgentSettings);
$("info-toggle").addEventListener("click", () => {
  setHeaderPopover("info", $("info-popover").hidden);
});
document.addEventListener("pointerdown", event => {
  if (!event.target.closest || event.target.closest(".app-frame-actions")) return;
  closeHeaderPopovers();
});
$("artifact-close").addEventListener("click", closeArtifact);
$("artifact-overlay").addEventListener("click", event => {
  if (event.target === $("artifact-overlay")) closeArtifact();
});
$("artifact-delete").addEventListener("click", () => {
  const artifactId = $("artifact-delete").dataset.artifactId;
  if (!artifactId || !confirm(`Delete artifact "${artifactId}"?`)) return;
  api("DELETE", `/artifacts/${encodeURIComponent(artifactId)}`)
    .then(() => { closeArtifact(); return refresh(); })
    .catch(error => setStatus(error.message));
});
document.addEventListener("keydown", event => {
  const field = event.target.closest && event.target.closest("input[data-control-type='field']");
  if (field && event.key === "Enter") {
    event.preventDefault();
    submitArtifactInteraction(field, field.value).catch(error => setStatus(error.message));
    return;
  }
  if (event.key !== "Escape") return;
  if (!$("artifact-overlay").hidden) closeArtifact();
  else closeHeaderPopovers();
});

refresh();
setInterval(refresh, 5000);
