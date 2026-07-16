const pending = new Map();
let nextRequestId = 1;
let snapshot = null;
let sessionOptions = {};
let draftSettings = null;
let openArtifactId = null;
let openArtifactUpdatedAt = null;
const fullMessages = new Map();

const $ = id => document.getElementById(id);
const esc = value => {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
};
const escAttr = value => esc(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const runtimeLabel = runtime => runtime === "claude_code" ? "Claude Code" : runtime === "codex" ? "Codex" : runtime;
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

// Escape first, then apply the tiny inline markup: `code`, **bold**, *italic*.
function mdLite(value) {
  let html = esc(value);
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  return html.replace(/\n/g, "<br>");
}

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
  parent.postMessage({ type: "trustyclaw-app-api", request_id: requestId, method, path: "/v1/apps/mission_pursuit/api" + path, body }, "*");
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

async function refresh() {
  try {
    if (!Object.keys(sessionOptions).length) {
      const response = await api("GET", "/session-options");
      if (!response.session_options || typeof response.session_options !== "object") {
        throw new Error("Mission Pursuit returned invalid agent settings");
      }
      sessionOptions = response.session_options;
      draftSettings = defaultAgentSettings();
    }
    snapshot = await api("GET", "/workspace");
    if (snapshot.workspace.agent_runtime && $("agent-settings-popover").hidden) {
      draftSettings = workspaceAgentSettings();
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
  const started = Boolean(snapshot.workspace.agent_runtime) || snapshot.messages.length > 0;
  $("hero").hidden = started;
  $("workspace").hidden = !started;
  renderAgentSettings();
  if (!started) return;
  renderGoal();
  renderFeed();
  renderBusy();
  renderSchedules();
  renderArtifacts();
  renderTools();
  renderMemories();
}

function defaultAgentSettings() {
  const runtime = Object.keys(sessionOptions)[0];
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
  const runtimes = Object.keys(sessionOptions);
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
    ? `<span class="goal-kicker">Goal</span> ${esc(goal)}` +
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
  // Snapshot messages are truncated server-side to keep the payload small;
  // fetch the full body once on demand.
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
    `<svg width="12" height="12" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 13.5 10 4l6 9.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>` +
    `${esc(message.content)}</span></div>`;
}

function renderBusy() {
  const busy = snapshot.busy || [];
  const bar = $("busy-bar");
  bar.hidden = busy.length === 0;
  if (!busy.length) return;
  // The active run is the primary control target; queued runs come after.
  const current = busy.find(run => run.status === "active") || busy[0];
  const kindText = current.kind === "schedule" ? "Running a scheduled turn"
    : current.kind === "continuation" ? "Reading artifacts"
    : "Agent is working";
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

function renderArtifacts() {
  const rows = snapshot.artifacts || [];
  if (!rows.length) {
    $("artifacts").innerHTML = `<div class="empty-state">No artifacts yet. They appear as your agent builds them.</div>`;
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
        ${tool.note ? `<span class="rail-item-sub rail-item-note">${esc(tool.note)}</span>` : ""}
      </div>
      <span class="rail-item-actions">
        <button class="danger sm" data-tool-delete="${esc(tool.tool_id)}">Delete</button>
      </span>
    </div>`).join("");
}

function renderMemories() {
  const rows = snapshot.memories || [];
  if (!rows.length) {
    $("memories").innerHTML = `<div class="empty-state">Nothing remembered yet. The agent stores durable facts here; a nightly dream cycle tidies them.</div>`;
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
  if (listed.updated_at === openArtifactUpdatedAt) return;
  // Rebuilding the body replaces the control nodes, which would drop a
  // half-typed field value and its focus. Defer while the operator is editing
  // a control inside the overlay; the next poll picks up the change once they
  // blur or submit.
  const active = document.activeElement;
  const body = $("artifact-body");
  if (active && body && body.contains(active) && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) {
    return;
  }
  await openArtifact(openArtifactId);
}

function renderBlock(block, artifactId) {
  if (!block || typeof block !== "object") return "";
  if (block.type === "heading") {
    const level = block.level === 1 ? "b-h1" : block.level === 3 ? "b-h3" : "b-h2";
    return `<div class="b-heading ${level}">${esc(block.text)}</div>`;
  }
  if (block.type === "text") {
    return String(block.text).split(/\n{2,}/).map(paragraph => `<p class="b-text">${mdLite(paragraph)}</p>`).join("");
  }
  if (block.type === "callout") {
    const tone = ["success", "warning", "danger"].includes(block.tone) ? block.tone : "info";
    return `<aside class="b-callout ${tone}">
      ${block.title ? `<div class="callout-title">${esc(block.title)}</div>` : ""}
      <div class="callout-text">${mdLite(block.text)}</div>
    </aside>`;
  }
  if (block.type === "metrics") {
    return `<div class="b-metrics">${(block.items || []).map(item => `
      <div class="metric-tile">
        <span class="metric-label">${esc(item.label)}</span>
        <span class="metric-value">${esc(item.value)}</span>
        ${item.delta ? `<span class="metric-delta ${String(item.delta).startsWith("-") ? "down" : "up"}">${esc(item.delta)}</span>` : ""}
      </div>`).join("")}</div>`;
  }
  if (block.type === "cards") {
    return `<div class="b-cards">${(block.items || []).map(item => {
      const tone = ["info", "success", "warning", "danger"].includes(item.tone) ? item.tone : "neutral";
      return `<article class="artifact-card ${tone}">
        <div class="card-head"><span class="card-title">${esc(item.title)}</span>${item.badge ? `<span class="card-badge">${esc(item.badge)}</span>` : ""}</div>
        ${item.text ? `<div class="card-text">${mdLite(item.text)}</div>` : ""}
      </article>`;
    }).join("")}</div>`;
  }
  if (block.type === "details") {
    return `<dl class="b-details">${(block.items || []).map(item => `
      <div class="detail-row"><dt>${esc(item.label)}</dt><dd>${mdLite(item.value)}</dd></div>`).join("")}</dl>`;
  }
  if (block.type === "list") {
    const tag = block.style === "number" ? "ol" : "ul";
    return `<${tag} class="b-list">${(block.items || []).map(item => `<li>${mdLite(item)}</li>`).join("")}</${tag}>`;
  }
  if (block.type === "table") {
    const head = (block.columns || []).map(column => `<th>${esc(column)}</th>`).join("");
    const body = (block.rows || []).map(row => `<tr>${row.map(cell => `<td>${esc(cell)}</td>`).join("")}</tr>`).join("");
    return `<div class="b-table-wrap"><table class="b-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }
  if (block.type === "checklist") {
    return `<ul class="b-checklist">${(block.items || []).map(item => `
      <li class="${item.done ? "done" : ""}"><span class="check-mark" aria-hidden="true">${item.done ? "✓" : ""}</span>${esc(item.text)}</li>`).join("")}</ul>`;
  }
  if (block.type === "progress") {
    const value = Math.max(0, Math.min(100, Number(block.value) || 0));
    return `<div class="b-progress">
      ${block.label ? `<span class="progress-label">${esc(block.label)}</span>` : ""}
      <span class="progress-track"><span class="progress-fill" style="width:${value}%"></span></span>
      <span class="progress-value">${esc(Math.round(value))}%</span>
    </div>`;
  }
  if (block.type === "timeline") {
    return `<ol class="b-timeline">${(block.items || []).map(item => `
      <li class="${escAttr(item.status)}">
        <span class="timeline-marker" aria-hidden="true"></span>
        <div class="timeline-content">
          <div class="timeline-head"><span class="timeline-title">${esc(item.title)}</span>${item.time ? `<span class="timeline-time">${esc(item.time)}</span>` : ""}</div>
          ${item.text ? `<div class="timeline-text">${mdLite(item.text)}</div>` : ""}
        </div>
      </li>`).join("")}</ol>`;
  }
  if (block.type === "kanban") {
    return `<div class="b-kanban">${(block.columns || []).map(column => `
      <section class="kanban-column">
        <div class="kanban-title">${esc(column.title)}<span>${(column.items || []).length}</span></div>
        <div class="kanban-items">${(column.items || []).map(item => `<div class="kanban-item">${mdLite(item)}</div>`).join("") || `<div class="kanban-empty">Empty</div>`}</div>
      </section>`).join("")}</div>`;
  }
  if (block.type === "chart") return renderChart(block);
  if (block.type === "code") {
    return `<div class="b-code">${block.language ? `<span class="code-lang">${esc(block.language)}</span>` : ""}<pre>${esc(block.text)}</pre></div>`;
  }
  if (block.type === "button") {
    const tone = ["neutral", "danger"].includes(block.tone) ? block.tone : "primary";
    return `<div class="b-control b-button-control">
      <button type="button" class="b-control-button ${tone}" data-artifact-interaction
        data-artifact-id="${escAttr(artifactId)}" data-control-id="${escAttr(block.control_id)}"
        data-control-type="button">${esc(block.label)}</button>
    </div>`;
  }
  if (block.type === "toggle") {
    return `<label class="b-control b-toggle-control">
      <span class="control-label">${esc(block.label)}</span>
      <input type="checkbox" data-artifact-interaction data-artifact-id="${escAttr(artifactId)}"
        data-control-id="${escAttr(block.control_id)}" data-control-type="toggle" ${block.value ? "checked" : ""}>
      <span class="toggle-track" aria-hidden="true"><span></span></span>
    </label>`;
  }
  if (block.type === "field") {
    return `<label class="b-control b-field-control">
      <span class="control-label">${esc(block.label)}</span>
      <span class="field-input-row">
        <input type="text" maxlength="1000" value="${escAttr(block.value)}" placeholder="${escAttr(block.placeholder || "")}"
          data-artifact-id="${escAttr(artifactId)}" data-control-id="${escAttr(block.control_id)}" data-control-type="field">
        <button type="button" class="field-submit-button" data-field-submit aria-label="Submit field" title="Submit field">
          <svg width="16" height="16" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 6l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
      </span>
    </label>`;
  }
  if (block.type === "divider") return `<hr class="b-divider">`;
  return "";
}

// Single-series chart per the repo's chart conventions: thin marks with
// rounded data ends, a 2px gap between bars, recessive gridlines, labels in
// text tokens, and a hover tooltip. Bars are zero-based; lines pad min..max.
const CHART_SERIES_COLOR = "#8b7cf6";

function renderChart(block) {
  const points = block.points || [];
  const W = 560, H = 220, padLeft = 44, padRight = 12, padTop = 14, padBottom = 26;
  const plotW = W - padLeft - padRight, plotH = H - padTop - padBottom;
  const values = points.map(point => Number(point.value));
  let min = Math.min(...values), max = Math.max(...values);
  if (block.kind === "bar") { min = Math.min(0, min); max = Math.max(0, max); }
  else { const pad = (max - min) * 0.1 || Math.abs(max) * 0.1 || 1; min -= pad; max += pad; }
  if (min === max) { min -= 1; max += 1; }
  const yFor = value => padTop + plotH - ((value - min) / (max - min)) * plotH;
  const ticks = [min, (min + max) / 2, max];
  const grid = ticks.map(tick => {
    const y = yFor(tick);
    return `<line x1="${padLeft}" y1="${y}" x2="${W - padRight}" y2="${y}" class="chart-grid"/>` +
      `<text x="${padLeft - 6}" y="${y + 3}" class="chart-tick" text-anchor="end">${esc(shortNumber(tick))}</text>`;
  }).join("");
  const labelEvery = Math.ceil(points.length / 7);
  const xLabels = points.map((point, index) => {
    if (index % labelEvery !== 0 && index !== points.length - 1) return "";
    const x = padLeft + (points.length === 1 ? plotW / 2 : (index / (points.length - 1)) * plotW);
    const text = String(point.label).length > 9 ? String(point.label).slice(0, 8) + "…" : String(point.label);
    return `<text x="${x}" y="${H - 8}" class="chart-tick" text-anchor="middle">${esc(text)}</text>`;
  }).join("");
  let marks = "";
  if (block.kind === "bar") {
    const slot = plotW / points.length;
    const barW = Math.max(3, Math.min(28, slot - 2));
    const zeroY = yFor(0);
    marks = points.map((point, index) => {
      const x = padLeft + slot * index + (slot - barW) / 2;
      const y = yFor(Number(point.value));
      const top = Math.min(y, zeroY), height = Math.max(2, Math.abs(zeroY - y));
      const radius = Math.min(4, barW / 2, height);
      return `<path class="chart-mark" data-index="${index}" d="${roundedBarPath(x, top, barW, height, radius, Number(point.value) >= 0)}" fill="${CHART_SERIES_COLOR}"/>`;
    }).join("");
    // Bar x labels use slot centers instead of the line positions.
    marks += points.map((point, index) => {
      if (index % labelEvery !== 0 && index !== points.length - 1) return "";
      const x = padLeft + slot * index + slot / 2;
      const text = String(point.label).length > 9 ? String(point.label).slice(0, 8) + "…" : String(point.label);
      return `<text x="${x}" y="${H - 8}" class="chart-tick" text-anchor="middle">${esc(text)}</text>`;
    }).join("");
  } else {
    const xFor = index => padLeft + (points.length === 1 ? plotW / 2 : (index / (points.length - 1)) * plotW);
    const path = values.map((value, index) => `${index ? "L" : "M"}${xFor(index).toFixed(1)},${yFor(value).toFixed(1)}`).join(" ");
    marks = `<path d="${path}" fill="none" stroke="${CHART_SERIES_COLOR}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>` +
      values.map((value, index) =>
        `<circle class="chart-mark chart-dot" data-index="${index}" cx="${xFor(index).toFixed(1)}" cy="${yFor(value).toFixed(1)}" r="4" fill="${CHART_SERIES_COLOR}"/>`).join("");
  }
  const baseline = `<line x1="${padLeft}" y1="${padTop + plotH}" x2="${W - padRight}" y2="${padTop + plotH}" class="chart-axis"/>`;
  const data = escAttr(JSON.stringify(points.map(point => ({ label: String(point.label), value: Number(point.value) }))));
  return `<figure class="b-chart" data-points="${data}">
    ${block.label ? `<figcaption class="chart-label">${esc(block.label)}</figcaption>` : ""}
    <div class="chart-frame">
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${escAttr(block.label || "chart")}">
        ${grid}${baseline}${marks}${block.kind === "line" ? xLabels : ""}
      </svg>
      <div class="chart-tooltip" hidden></div>
    </div>
  </figure>`;
}

function roundedBarPath(x, y, width, height, radius, positive) {
  if (positive) {
    return `M${x},${y + height} L${x},${y + radius} Q${x},${y} ${x + radius},${y} L${x + width - radius},${y} Q${x + width},${y} ${x + width},${y + radius} L${x + width},${y + height} Z`;
  }
  return `M${x},${y} L${x + width},${y} L${x + width},${y + height - radius} Q${x + width},${y + height} ${x + width - radius},${y + height} L${x + radius},${y + height} Q${x},${y + height} ${x},${y + height - radius} Z`;
}

function shortNumber(value) {
  const abs = Math.abs(value);
  if (abs >= 1e9) return (value / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (abs >= 1e6) return (value / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (abs >= 1e3) return (value / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
  return abs >= 100 || Number.isInteger(value) ? String(Math.round(value)) : value.toFixed(1);
}

function attachChartTooltips(root) {
  root.querySelectorAll(".b-chart").forEach(figure => {
    const points = JSON.parse(figure.dataset.points || "[]");
    const frame = figure.querySelector(".chart-frame");
    const tooltip = figure.querySelector(".chart-tooltip");
    const svg = figure.querySelector("svg");
    frame.addEventListener("mousemove", event => {
      const marks = [...svg.querySelectorAll(".chart-mark")];
      if (!marks.length) return;
      const frameRect = frame.getBoundingClientRect();
      let best = null, bestDistance = Infinity;
      marks.forEach(mark => {
        const rect = mark.getBoundingClientRect();
        const distance = Math.abs(event.clientX - (rect.left + rect.width / 2));
        if (distance < bestDistance) { bestDistance = distance; best = mark; }
      });
      const index = Number(best.dataset.index);
      const point = points[index];
      if (!point) return;
      const rect = best.getBoundingClientRect();
      tooltip.hidden = false;
      tooltip.textContent = `${point.label}: ${point.value}`;
      tooltip.style.left = `${rect.left + rect.width / 2 - frameRect.left}px`;
      tooltip.style.top = `${rect.top - frameRect.top - 8}px`;
    });
    frame.addEventListener("mouseleave", () => { tooltip.hidden = true; });
  });
}

// --------------------------------------------------------------------------
// Actions

async function sendMessage(fromHero) {
  const input = fromHero ? $("hero-input") : $("chat-input");
  const content = input.value.trim();
  if (!content) return;
  const body = { content };
  if (!snapshot || !snapshot.workspace.agent_runtime) {
    if (!draftSettings) throw new Error("Agent settings are not loaded yet");
    body.agent_runtime = draftSettings.runtime;
    body.model = draftSettings.model;
    body.effort = draftSettings.effort;
  }
  await api("POST", "/messages", body);
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

$("hero-send").addEventListener("click", () => sendMessage(true).catch(error => setStatus(error.message)));
$("chat-send").addEventListener("click", () => sendMessage(false).catch(error => setStatus(error.message)));
[["hero-input", true], ["chat-input", false]].forEach(([id, fromHero]) => {
  $(id).addEventListener("keydown", event => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") sendMessage(fromHero).catch(error => setStatus(error.message));
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
