"use strict";
const $ = id => document.getElementById(id);
let eventsSince = null, netSince = null;
const events = [], netEvents = [];
let runtimeType = "codex";
let threads = [], threadTasks = [];
let selectedThreadId = null, selectedThreadRuntime = null;
let activeTab = "home";
let activeNetworkPolicy = {"managed_ai_provider_network_access": {}, "allowed_network_access": {}};
let proposedNetworkPolicy = {"managed_ai_provider_network_access": {}, "allowed_network_access": {}};
let latestRuntimes = [];
let selectedTask = null;
let selectedTaskEvents = [];
let selectedTaskEventsSince = null;
let selectedTaskEventsHasMore = false;
let currentFilePath = "/";
let fileEntries = [];
const FILE_LIST_ENTRY_LIMIT = 1000;
const TASK_EVENT_PAGE_BATCH = 10;
const POLICY_PRESETS = {
  openai: { managed: { openai: true }, rules: {} },
  claude: { managed: { claude: true }, rules: {} },
  github: {
    managed: {},
    rules: {
      "github.com": {"allow_http_methods": ["GET", "POST"]},
      "api.github.com": {"allow_http_methods": ["GET", "POST", "PATCH", "PUT", "DELETE"]},
      "codeload.github.com": {"allow_http_methods": ["GET", "HEAD"]},
      "objects.githubusercontent.com": {"allow_http_methods": ["GET", "HEAD"]},
      "raw.githubusercontent.com": {"allow_http_methods": ["GET", "HEAD"]},
      "release-assets.githubusercontent.com": {"allow_http_methods": ["GET", "HEAD"]},
    },
  },
  python: {
    managed: {},
    rules: {
      "pypi.org": {
        "allow_http_methods": ["GET", "HEAD"],
        "path_guards": ["^/simple(?:/.*)?$", "^/pypi/[^/]+/json$"],
      },
      "files.pythonhosted.org": {
        "allow_http_methods": ["GET", "HEAD"],
        "path_guards": ["^/packages(?:/.*)?$"],
      },
    },
  },
  npm: {
    managed: {},
    rules: {
      "nodejs.org": {
        "allow_http_methods": ["GET", "HEAD"],
        "path_guards": ["^/dist(?:/.*)?$"],
      },
      "registry.npmjs.org": {"allow_http_methods": ["GET", "HEAD"]},
    },
  },
};
const POLICY_PRESET_BUTTONS = {
  openai: { label: "OpenAI", proposed: "OpenAI in proposal", partial: "OpenAI partial proposal" },
  claude: { label: "Claude", proposed: "Claude in proposal", partial: "Claude partial proposal" },
  github: { label: "GitHub", proposed: "GitHub in proposal", partial: "GitHub partial proposal" },
  python: { label: "Python packages", proposed: "Python packages in proposal", partial: "Python packages partial proposal" },
  npm: { label: "npm packages", proposed: "npm packages in proposal", partial: "npm packages partial proposal" },
};
const RUNTIME_PROVIDERS = {
  codex: { label: "Codex", provider: "openai", providerLabel: "OpenAI" },
  claude_code: { label: "Claude Code", provider: "claude", providerLabel: "Claude" },
};
const PRESET_INFO = {
  openai: {
    heading: "OpenAI expands internally",
    rows: [
      ["api.openai.com", "POST; account guard; live web search disabled"],
      ["auth.openai.com", "GET, POST"],
      ["chatgpt.com", "GET, POST; account guard; live web search disabled"],
    ],
  },
  claude: {
    heading: "Claude expands internally",
    rows: [
      ["api.anthropic.com", "GET, POST; account guard"],
      ["platform.claude.com", "GET, POST; only /v1/oauth paths"],
    ],
  },
  github: {
    heading: "GitHub expands",
    rows: [
      ["github.com", "GET, POST"],
      ["api.github.com", "GET, POST, PATCH, PUT, DELETE"],
      ["codeload.github.com", "GET, HEAD"],
      ["objects.githubusercontent.com", "GET, HEAD"],
      ["raw.githubusercontent.com", "GET, HEAD"],
      ["release-assets.githubusercontent.com", "GET, HEAD"],
    ],
  },
  python: {
    heading: "Python packages expands",
    rows: [
      ["pypi.org", "GET, HEAD; only /simple and /pypi/<package>/json paths"],
      ["files.pythonhosted.org", "GET, HEAD; only /packages paths"],
    ],
  },
  npm: {
    heading: "npm packages expands",
    rows: [
      ["nodejs.org", "GET, HEAD; only /dist paths"],
      ["registry.npmjs.org", "GET, HEAD"],
    ],
  },
};

function getPassword() {
  const match = document.cookie.match(/(?:^|; )trustyclaw_admin=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : null;
}

function login() {
  const value = $("password").value.trim();
  if (!value) return;
  document.cookie = "trustyclaw_admin=" + encodeURIComponent(value) + "; path=/; max-age=2592000; samesite=strict";
  $("password").value = "";
  start();
}

function logout() {
  document.cookie = "trustyclaw_admin=; path=/; max-age=0";
  location.reload();
}

async function api(method, path, body) {
  const headers = { "Authorization": "Bearer " + getPassword() };
  if (method !== "GET") headers["Idempotency-Key"] = crypto.randomUUID();
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json();
  if (response.status === 401) { showLogin(); throw new Error("unauthorized"); }
  if (!response.ok) throw new Error(data.error ? data.error.message : response.statusText);
  return data;
}

function notice(message) {
  $("notice").textContent = message || "";
  if (message) setTimeout(() => { $("notice").textContent = ""; }, 8000);
}

function policyMessage(message) {
  $("policy-message").textContent = message || "";
}

function badge(value) { return `<span class="status ${value}">${value}</span>`; }
function esc(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

// Skip innerHTML swaps when nothing changed: the UI polls every 5 seconds and
// unconditional re-renders would break in-flight taps, hover, and selection.
function setHtml(el, html) {
  if (el.__lastHtml === html) return;
  el.__lastHtml = html;
  el.innerHTML = html;
}

function showLogin() {
  $("login").hidden = false;
  $("app").hidden = true;
  $("logout-button").hidden = true;
  $("agent-name").hidden = true;
}

function showTab(name) {
  activeTab = name;
  for (const tabName of ["home", "agent", "agent-log", "files", "network", "net-log"]) {
    $(`tab-${tabName}`).classList.toggle("active-tab", tabName === name);
    $(`panel-${tabName}`).hidden = tabName !== name;
  }
  if (name === "files" && !fileEntries.length) loadAgentFiles(currentFilePath);
}

function togglePresetInfo(preset, button) {
  const panel = $("preset-info-popover");
  const isOpen = !panel.hidden && panel.dataset.preset === preset;
  panel.hidden = isOpen;
  panel.dataset.preset = isOpen ? "" : preset;
  panel.innerHTML = isOpen ? "" : renderPresetInfo(PRESET_INFO[preset]);
  if (!isOpen) {
    const rect = button.getBoundingClientRect();
    panel.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - 400))}px`;
    panel.style.top = `${rect.bottom + 8}px`;
  }
  for (const button of document.querySelectorAll(".info-button")) {
    button.setAttribute("aria-expanded", String(!isOpen && button.dataset.info === preset));
  }
}

function renderPresetInfo(info) {
  return `
    <h3>${esc(info.heading)}</h3>
    <table>
      ${info.rows.map(([domain, scope]) => `
        <tr>
          <td><strong>${esc(domain)}</strong></td>
          <td>${esc(scope)}</td>
        </tr>`).join("")}
    </table>`;
}

function gib(bytes) { return (bytes / 1073741824).toFixed(1); }

function statTile(label, valueHtml, extraClass = "") {
  return `<div class="stat-tile${extraClass ? " " + extraClass : ""}"><div class="stat-label">${esc(label)}</div><div class="stat-value">${valueHtml}</div></div>`;
}

function meterTile(label, valueHtml, percent) {
  const clamped = Math.max(0, Math.min(100, percent));
  return `
    <div class="stat-tile">
      <div class="stat-label">${esc(label)}</div>
      <div class="stat-value">${valueHtml}</div>
      <div class="meter${clamped >= 80 ? " hot" : ""}"><i style="width:${clamped.toFixed(1)}%"></i></div>
    </div>`;
}

function usageTile(label, used, total, totalLabel) {
  const value = `${esc(gib(used))} <span class="unit">/ ${esc(gib(total))} ${esc(totalLabel || "GiB")}</span>`;
  return meterTile(label, value, total > 0 ? (used / total) * 100 : 0);
}

async function refreshHealth() {
  const health = await api("GET", "/v1/health");
  $("agent-name").textContent = health.agent_name;
  $("agent-name").hidden = false;
  const runtimes = Array.isArray(health.agent_runtime.runtimes) ? health.agent_runtime.runtimes : [];
  latestRuntimes = runtimes;
  runtimeType = runtimes.find(r => r.status === "active")?.type || runtimes[0]?.type || "codex";
  const host = health.host_runtime;
  setHtml($("health"), `
    <div class="stat-grid stat-statuses">
      ${statTile("Overall", badge(health.status))}
      ${statTile("Network controls", badge(health.network_controls.status))}
      ${statTile("Version", renderVersion(health.version), "stat-wide")}
    </div>
    <div class="stat-grid stat-meters">
      ${meterTile("CPU", `${esc(host.cpu.usage_percent)}<span class="unit">%</span>`, Number(host.cpu.usage_percent) || 0)}
      ${usageTile("Memory", host.memory.used_bytes, host.memory.total_bytes)}
      ${usageTile("Filesystem", host.filesystem.used_bytes, host.filesystem.total_bytes)}
      ${usageTile("Swap", host.swap.used_bytes, host.swap.allocated_bytes)}
    </div>`);
  setHtml($("runtime"), `<div class="runtime-grid">` +
    runtimes.map(runtime => `
      <div class="runtime-card">
        <div>
          <div class="name">${esc(RUNTIME_PROVIDERS[runtime.type]?.label || runtime.type)}</div>
          <div class="sub">${esc(runtime.type)}${(runtime.active_task_ids || []).length
            ? ` &middot; ${(runtime.active_task_ids || []).length} running` : ""}</div>
        </div>
        ${badge(runtime.status)}
      </div>`).join("") + `</div>`);
  updateLoginButtons(runtimes);
  renderRuntimeGuidance(runtimes);
  const pending = runtimes.find(runtime => runtime.status === "awaiting_login");
  if (pending) await showOauth(false, pending.type);
  else setHtml($("oauth"), "");
}

function renderVersion(version) {
  if (!version || typeof version !== "object") return `<span class="muted">not reported</span>`;
  const status = typeof version.status === "string" && version.status ? version.status : "unknown";
  const runtime = typeof version.runtime === "string" && version.runtime ? version.runtime : "unknown";
  const state = typeof version.state === "string" && version.state ? version.state : "unknown";
  return `${badge(status)} <span class="muted">runtime</span> ${esc(runtime)} <span class="muted">state</span> ${esc(state)}`;
}

async function refreshProviderAccounts() {
  const response = await api("GET", "/v1/agent-runtime/account");
  const accounts = Array.isArray(response.accounts) ? response.accounts : [];
  if (!accounts.length) {
    setHtml($("provider-accounts"), `<tr><td class="empty-state">No provider accounts.</td></tr>`);
    return;
  }
  setHtml($("provider-accounts"), `
    <tr><th>runtime</th><th>account</th><th>plan</th><th>usage</th></tr>
    ${accounts.map(renderProviderAccountRow).join("")}`);
}

function renderProviderAccountRow(account) {
  const identity = [];
  if (account.account_id) identity.push(`<div>${esc(account.account_id)}</div>`);
  if (account.email) identity.push(`<div class="muted">${esc(account.email)}</div>`);
  const plan = account.plan_type ? esc(account.plan_type) : `<span class="muted">not reported</span>`;
  const usage = account.agent_runtime === "claude_code"
    ? renderClaudeUsage(account.claude_usage)
    : renderCodexUsage(account.codex_usage);
  return `
    <tr>
      <td>${esc(account.agent_runtime)}<br>${badge(account.status)}</td>
      <td>${identity.length ? identity.join("") : `<span class="muted">not available</span>`}</td>
      <td>${plan}</td>
      <td>${usage}</td>
    </tr>`;
}

function renderCodexUsage(codexUsage) {
  if (codexUsage === undefined || codexUsage == null) return `<span class="muted">not reported</span>`;
  if (codexUsage && typeof codexUsage === "object" && codexUsage.rate_limits) {
    return renderRateLimits(codexUsage);
  }
  return renderMetadata(codexUsage);
}

function renderClaudeUsage(claudeUsage) {
  if (claudeUsage === undefined || claudeUsage == null) return `<span class="muted">not reported</span>`;
  if (!claudeUsage || typeof claudeUsage !== "object") return renderMetadata(claudeUsage);
  const rows = [];
  if (claudeUsage.current_session_used_percent !== undefined) {
    rows.push(`<div>current session: ${esc(`${claudeUsage.current_session_used_percent}%`)}</div>`);
  }
  if (claudeUsage.weekly_used_percent !== undefined) {
    rows.push(`<div>weekly: ${esc(`${claudeUsage.weekly_used_percent}%`)}</div>`);
  }
  if (claudeUsage.weekly_resets_at_text) {
    rows.push(`<div class="muted">resets ${esc(claudeUsage.weekly_resets_at_text)}</div>`);
  }
  if (claudeUsage.last_checked_at) {
    rows.push(`<div class="muted">checked ${esc(formatDateTime(claudeUsage.last_checked_at))}</div>`);
  }
  return rows.length ? rows.join("") : renderMetadata(claudeUsage);
}

function renderRateLimits(usage) {
  const snapshot = usage.rate_limits;
  const rows = [];
  if (snapshot && typeof snapshot === "object") {
    const windows = [];
    if (snapshot.primary) windows.push(["primary", snapshot.primary]);
    if (snapshot.secondary) windows.push(["secondary", snapshot.secondary]);
    const renderedWindows = windows.map(([name, window]) => renderRateLimitWindow(name, window)).filter(Boolean).join("");
    const credits = snapshot.credits ? renderCredits(snapshot.credits) : "";
    if (renderedWindows || credits) rows.push(`<div>${renderedWindows}${credits}</div>`);
  }
  if (usage.last_checked_at) rows.push(`<div class="muted">checked ${esc(formatDateTime(usage.last_checked_at))}</div>`);
  const extra = { ...usage };
  delete extra.rate_limits;
  delete extra.last_checked_at;
  return rows.join("") + (Object.keys(extra).length ? `<pre class="metadata">${esc(JSON.stringify(extra, null, 2))}</pre>` : "");
}

function renderRateLimitWindow(name, window) {
  if (!window || typeof window !== "object") return "";
  const duration = window.window_duration_mins;
  const label = duration === 300 ? "5 hour" : duration === 10080 ? "weekly" : `${name} (${duration ?? "unknown"} min)`;
  const used = window.used_percent === undefined ? "not reported" : `${window.used_percent}%`;
  const resets = window.resets_at ? `<br><span class="muted">resets ${esc(formatUnixTime(window.resets_at))}</span>` : "";
  return `<div>${esc(label)}: ${esc(used)}${resets}</div>`;
}

function renderCredits(credits) {
  if (!credits || typeof credits !== "object") return "";
  if (credits.unlimited === true) return `<div>credits: unlimited</div>`;
  if (credits.has_credits === false) return `<div>credits: none</div>`;
  if (credits.balance !== undefined) return `<div>credits: ${esc(credits.balance)}</div>`;
  return "";
}

function formatUnixTime(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return formatDateTime(numeric * 1000);
}

function formatDateTime(value) {
  const date = typeof value === "number" ? new Date(value) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

function renderMetadata(value) {
  if (value == null) return `<span class="muted">not reported</span>`;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return esc(value);
  return `<pre class="metadata">${esc(JSON.stringify(value, null, 2))}</pre>`;
}

function updateLoginButtons(runtimes) {
  const statuses = Object.fromEntries(runtimes.map(runtime => [runtime.type, runtime.status]));
  for (const [runtime, buttonId] of [
    ["codex", "start-codex-login"],
    ["claude_code", "start-claude-login"],
  ]) {
    const button = $(buttonId);
    const canStart = statuses[runtime] === "awaiting_login";
    button.hidden = !canStart;
    button.disabled = !canStart;
  }
}

function providerEnabled(policy, provider) {
  const managed = objectValue(policy ? policy.managed_ai_provider_network_access : null);
  return managed[provider] === true;
}

function renderRuntimeGuidance(runtimes = latestRuntimes) {
  const messages = [];
  for (const runtime of runtimes) {
    const meta = RUNTIME_PROVIDERS[runtime.type];
    if (!meta || runtime.status !== "deactivated") continue;
    const activeEnabled = providerEnabled(activeNetworkPolicy, meta.provider);
    const proposedEnabled = providerEnabled(proposedNetworkPolicy, meta.provider);
    if (!activeEnabled && proposedEnabled) {
      messages.push(`
        <div class="runtime-guidance">
          <p>${esc(meta.label)} is deactivated because ${esc(meta.providerLabel)} provider access is disabled in the active network policy. The proposal enables it; replace the active policy to activate this runtime.</p>
          <div class="actions">
            <button class="ghost sm" data-action="show-tab" data-tab="network">Open Network settings</button>
          </div>
        </div>`);
    } else if (!activeEnabled) {
      messages.push(`
        <div class="runtime-guidance">
          <p>${esc(meta.label)} is deactivated because ${esc(meta.providerLabel)} provider access is disabled in the active network policy. Add ${esc(meta.providerLabel)} to the proposed policy before starting login.</p>
          <div class="actions">
            <button class="ghost sm" data-action="show-tab" data-tab="network">Open Network settings</button>
          </div>
        </div>`);
    }
  }
  setHtml($("runtime-guidance"), messages.length ? `<div class="runtime-guidance-list">${messages.join("")}</div>` : "");
}

async function showOauth(start, runtime) {
  runtime = runtime || runtimeType;
  try {
    if (runtime === "claude_code") {
      const login = await api(start ? "POST" : "GET", "/v1/agent-runtime/claude-oauth-login");
      setHtml($("oauth"), `<div class="oauth-card">
        <span>Claude Code login: open
        <a href="${esc(login.login_url)}" target="_blank">${esc(login.login_url)}</a>
        <span class="muted">(expires ${esc(login.expires_at)})</span></span>
        <button class="primary sm" data-action="complete-claude-login">Submit code</button></div>`);
      return;
    }
    const login = await api(start ? "POST" : "GET", "/v1/agent-runtime/codex-oauth-login");
    setHtml($("oauth"), `<div class="oauth-card">
      <span>Codex login: enter code <b>${esc(login.device_code)}</b> at
      <a href="${esc(login.login_url)}" target="_blank">${esc(login.login_url)}</a>
      <span class="muted">(expires ${esc(login.expires_at)})</span></span></div>`);
  } catch (error) { if (start) notice(error.message); }
}

async function startLogin(runtime) { await showOauth(true, runtime); }

async function completeClaudeLogin() {
  const code = prompt("Claude Code login code:");
  if (!code) return;
  try {
    await api("POST", "/v1/agent-runtime/claude-oauth-login/complete", { code });
    notice("Claude Code login submitted.");
    await refreshHealth();
  } catch (error) { notice(error.message); }
}

async function rebootHost() {
  if (!confirm("Reboot the host machine?")) return;
  try { await api("POST", "/v1/host-runtime/reboot"); notice("Reboot accepted; the host will be back shortly."); }
  catch (error) { notice(error.message); }
}

async function createTask() {
  const message = $("new-task").value.trim();
  const threadId = $("new-task-thread").value.trim();
  const agentRuntime = $("new-task-runtime").value;
  if (!message || !threadId) return;
  try {
    await api("POST", "/v1/tasks", { input_message: message, thread_id: threadId, agent_runtime: agentRuntime });
    $("new-task").value = "";
    selectedThreadId = threadId;
    selectedThreadRuntime = agentRuntime;
    updateComposer();
    await refreshSelectedThread();
    await loadThreads();
  }
  catch (error) { notice(error.message); }
}

async function steerTask(taskId) {
  const message = prompt("Steering message for " + taskId + ":");
  if (!message) return;
  try { await api("POST", `/v1/tasks/${taskId}/steer`, { steer_message: message }); notice("Steering accepted."); }
  catch (error) { notice(error.message); }
}

async function cancelTask(taskId) {
  try { await api("POST", `/v1/tasks/${taskId}/cancel`); await refreshSelectedThread(); }
  catch (error) { notice(error.message); }
}

async function killTask(taskId) {
  if (!confirm("Kill running task " + taskId + "? Its runtime process is terminated and the task is cancelled.")) return;
  try { await api("POST", `/v1/tasks/${taskId}/kill`); await refreshSelectedThread(); }
  catch (error) { notice(error.message); }
}

async function loadThreads() {
  const listed = await api("GET", "/v1/threads");
  threads = listed.threads || [];
  renderThreads();
}

function runtimeLabel(runtime) {
  return RUNTIME_PROVIDERS[runtime]?.label || runtime;
}

function renderThreads() {
  if (!threads.length) {
    setHtml($("threads"), `<div class="empty-state">No threads yet. Start one on the right.</div>`);
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

function newThread() {
  selectedThreadId = null;
  selectedThreadRuntime = null;
  threadTasks = [];
  $("task-events-detail").innerHTML = "";
  $("new-task-thread").value = "";
  updateComposer();
  renderThreadHistory();
  renderThreads();
  $("new-task-thread").focus();
}

function updateComposer() {
  const hasThread = selectedThreadId !== null;
  $("thread-field").hidden = hasThread;
  $("runtime-field").hidden = hasThread;
  $("composer-target").textContent = hasThread
    ? `New task on ${selectedThreadId} (${runtimeLabel(selectedThreadRuntime)})`
    : "New thread";
  $("new-task").placeholder = hasThread
    ? "Describe what the agent should do next…"
    : "Describe the first task in this thread…";
  if (hasThread) {
    $("new-task-thread").value = selectedThreadId;
    $("new-task-runtime").value = selectedThreadRuntime;
  }
}

async function showThread(threadId, agentRuntime) {
  if (threadId !== selectedThreadId) $("task-events-detail").innerHTML = "";
  selectedThreadId = threadId;
  selectedThreadRuntime = agentRuntime;
  updateComposer();
  renderThreads();
  await refreshSelectedThread();
}

async function refreshSelectedThread() {
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

function renderThreadHistory() {
  if (selectedThreadId === null) {
    setHtml($("thread-detail"), `<div class="empty-state thread-empty">Select a thread on the left to see its tasks, or start a new one below.</div>`);
    return;
  }
  const ordered = threadTasks.slice().sort((a, b) =>
    a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : taskNumber(a.task_id) - taskNumber(b.task_id));
  setHtml($("thread-detail"), `
    <div class="thread-head">
      <span class="thread-title">${esc(selectedThreadId)}</span>
      <span class="muted">${esc(runtimeLabel(selectedThreadRuntime))}</span>
    </div>
    ${ordered.length ? ordered.map(renderTaskCard).join("")
      : `<div class="empty-state thread-empty">No retained tasks for this thread yet.</div>`}`);
}

function renderTaskCard(task) {
  const actions = [
    task.status === "running"
      ? `<button class="sm" data-action="steer-task" data-task-id="${esc(task.task_id)}">Steer</button>
         <button class="danger sm" data-action="kill-task" data-task-id="${esc(task.task_id)}">Kill</button>`
      : "",
    task.status === "queued"
      ? `<button class="ghost sm" data-action="cancel-task" data-task-id="${esc(task.task_id)}">Cancel</button>`
      : "",
    `<button class="ghost sm" data-action="show-task-events" data-task-id="${esc(task.task_id)}">Events</button>`,
  ].join("");
  const pendingNote = task.status === "running" ? "Working&hellip;"
    : task.status === "queued" ? "Queued; waiting for the runtime." : "";
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
      ${pendingNote ? `<div class="msg pending muted">${pendingNote}</div>` : ""}
    </div>`;
}

async function loadTaskEventBatch(taskId) {
  for (let page = 0; page < TASK_EVENT_PAGE_BATCH; page += 1) {
    const response = await api(
      "GET",
      `/v1/tasks/${encodeURIComponent(taskId)}/events` +
        (selectedTaskEventsSince === null ? "" : "?since=" + selectedTaskEventsSince)
    );
    if (!response.events.length) return false;
    for (const event of response.events) {
      selectedTaskEvents.push(event);
      selectedTaskEventsSince = event.seq;
    }
  }
  return true;
}

async function showTaskEvents(taskId) {
  selectedTask = threadTasks.find(item => item.task_id === taskId) || await api("GET", `/v1/tasks/${encodeURIComponent(taskId)}`);
  selectedTaskEvents = [];
  selectedTaskEventsSince = null;
  selectedTaskEventsHasMore = await loadTaskEventBatch(taskId);
  renderTaskEventsDetail();
}

async function loadMoreTaskEvents(taskId) {
  if (!selectedTask || selectedTask.task_id !== taskId) {
    await showTaskEvents(taskId);
    return;
  }
  selectedTaskEventsHasMore = await loadTaskEventBatch(taskId);
  renderTaskEventsDetail();
}

function renderTaskEventsDetail() {
  const task = selectedTask;
  const taskEvents = selectedTaskEvents;
  $("task-events-detail").innerHTML = `
    <h2>${esc(task.task_id)} events</h2>
    <div class="table-scroll"><table>
      <tr><th>status</th><td>${badge(task.status)}</td></tr>
      <tr><th>runtime</th><td>${esc(task.agent_runtime)}</td></tr>
      <tr><th>thread</th><td class="mono">${esc(task.thread_id)}</td></tr>
      <tr><th>created</th><td class="muted time">${esc(formatDateTime(task.created_at))}</td></tr>
      <tr><th>updated</th><td class="muted time">${esc(formatDateTime(task.updated_at))}</td></tr>
      <tr><th>input</th><td><pre>${esc(task.input_message)}</pre></td></tr>
      ${task.output_message ? `<tr><th>output</th><td><pre>${esc(task.output_message)}</pre></td></tr>` : ""}
      ${task.error_message ? `<tr><th>error</th><td><pre>${esc(task.error_message)}</pre></td></tr>` : ""}
    </table></div>
    <h2>Events</h2>
    <div class="table-scroll"><table>
      <tr><th>seq</th><th>time</th><th>type</th><th>source</th><th>payload</th></tr>
      ${taskEvents.length ? taskEvents.map(event => `
        <tr>
          <td>${esc(event.seq)}</td>
          <td class="muted time">${esc(formatDateTime(event.timestamp))}</td>
          <td>${esc(event.event_type)}</td>
          <td>${esc(event.payload.source || "")}</td>
          <td><pre>${esc(event.payload.message || event.payload.error_message || JSON.stringify(event.payload))}</pre></td>
        </tr>`).join("") : `<tr><td colspan="5" class="muted">No retained events for this task.</td></tr>`}
    </table></div>
    ${selectedTaskEventsHasMore ? `<div class="actions"><button data-action="load-more-task-events" data-task-id="${esc(task.task_id)}">Load more events</button></div>` : ""}`;
}

async function refreshEvents() {
  const response = await api("GET", "/v1/events" + (eventsSince === null ? "" : "?since=" + eventsSince));
  for (const event of response.events) { events.push(event); eventsSince = event.seq; }
  while (events.length > 50) events.shift();
  setHtml($("events"), `<tr><th>time</th><th>type</th><th>task</th><th>payload</th></tr>` +
    events.slice().reverse().map(event => `
      <tr>
        <td class="muted time">${esc(formatDateTime(event.timestamp))}</td>
        <td class="mono">${esc(event.event_type)}</td>
        <td class="mono">${esc(event.task_id || "")}</td>
        <td><pre>${esc(event.payload.message || event.payload.error_message || "")}</pre></td>
      </tr>`).join(""));
}

async function refreshNetEvents() {
  const response = await api("GET", "/v1/network/events" + (netSince === null ? "" : "?since=" + netSince));
  for (const event of response.events) { netEvents.push(event); netSince = event.seq; }
  while (netEvents.length > 50) netEvents.shift();
  setHtml($("net-events"), `<tr><th>time</th><th>request</th><th>decision</th></tr>` +
    netEvents.slice().reverse().map(event => `
      <tr>
        <td class="muted time">${esc(formatDateTime(event.timestamp))}</td>
        <td class="mono">${esc(event.method)} ${esc(event.protocol)}://${esc(event.host)}${esc(event.path)}</td>
        <td>${badge(event.decision)}</td>
      </tr>`).join(""));
}

function fileMessage(message) {
  $("file-message").textContent = message || "";
}

function parentPath(path) {
  const normalized = path && path !== "/" ? path.replace(/\/+$/, "") : "/";
  if (normalized === "/") return "/";
  const index = normalized.lastIndexOf("/");
  return index <= 0 ? "/" : normalized.slice(0, index);
}

async function loadAgentFiles(path = currentFilePath) {
  try {
    fileMessage("");
    const response = await api("GET", `/v1/agent-files?path=${encodeURIComponent(path || "/")}`);
    currentFilePath = response.path || "/";
    fileEntries = Array.isArray(response.entries) ? response.entries : [];
    $("file-path").value = currentFilePath;
    renderFileList(response);
  } catch (error) {
    fileMessage(error.message);
  }
}

async function readAgentFile(path) {
  try {
    fileMessage("");
    const response = await api("GET", `/v1/agent-files/read?path=${encodeURIComponent(path)}`);
    renderFileContent(response);
  } catch (error) {
    fileMessage(error.message);
  }
}

async function openAgentPath(path, type) {
  if (type === "directory") {
    await loadAgentFiles(path);
    return;
  }
  await readAgentFile(path);
}

function renderFileList(listing = {}) {
  if (listing.truncated) {
    fileMessage(`Showing first ${FILE_LIST_ENTRY_LIMIT} entries.`);
  }
  const table = $("file-list");
  table.textContent = "";
  const header = document.createElement("tr");
  for (const label of ["name", "type", "size"]) {
    const cell = document.createElement("th");
    cell.textContent = label;
    header.appendChild(cell);
  }
  table.appendChild(header);
  if (currentFilePath !== "/") {
    table.appendChild(fileRow("..", parentPath(currentFilePath), "directory", null));
  }
  if (!fileEntries.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.className = "empty-state";
    cell.textContent = "Empty directory.";
    row.appendChild(cell);
    table.appendChild(row);
    return;
  }
  for (const entry of fileEntries) {
    table.appendChild(fileRow(entry.name, entry.path, entry.type, entry.size_bytes));
  }
}

function fileRow(name, path, type, sizeBytes) {
  const row = document.createElement("tr");
  const nameCell = document.createElement("td");
  const button = document.createElement("button");
  button.className = "file-entry";
  button.dataset.action = "open-file-path";
  button.dataset.path = path == null ? "" : String(path);
  button.dataset.fileType = type == null ? "" : String(type);
  button.textContent = name == null ? "" : String(name);
  nameCell.appendChild(button);
  row.appendChild(nameCell);

  const typeCell = document.createElement("td");
  typeCell.textContent = type == null ? "" : String(type);
  row.appendChild(typeCell);

  const sizeCell = document.createElement("td");
  sizeCell.className = "muted";
  sizeCell.textContent = sizeBytes == null ? "" : String(sizeBytes);
  row.appendChild(sizeCell);
  return row;
}

function renderFileContent(file) {
  const truncated = file.truncated ? " (truncated)" : "";
  $("file-viewer-title").textContent = `${file.path || ""}${truncated}`;
  $("file-content").textContent = file.content || "";
}

function goToFilePath() {
  loadAgentFiles($("file-path").value.trim() || "/");
}

async function loadPolicy() {
  const response = await api("GET", "/v1/network/policy");
  activeNetworkPolicy = normalizePolicy(response.network_controls);
  proposedNetworkPolicy = clonePolicy(activeNetworkPolicy);
  renderPolicyControls();
}

function normalizePolicy(policy) {
  const managed = objectValue(policy && policy.managed_ai_provider_network_access);
  const rules = objectValue(policy && policy.allowed_network_access);
  return {
    "managed_ai_provider_network_access": managed,
    "allowed_network_access": rules,
  };
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function clonePolicy(policy) {
  return normalizePolicy(JSON.parse(JSON.stringify(policy || {})));
}

function policiesEqual(left, right) {
  return JSON.stringify(normalizePolicy(left)) === JSON.stringify(normalizePolicy(right));
}

function renderPolicyControls(options = {}) {
  const updateEditor = options.updateEditor !== false;
  renderActivePolicy();
  renderPolicyPresets();
  renderProposalStatus();
  renderRuntimeGuidance();
  if (updateEditor) $("policy").value = JSON.stringify(proposedNetworkPolicy, null, 2);
}

function renderActivePolicy() {
  $("active-policy").value = JSON.stringify(activeNetworkPolicy, null, 2);
}

function renderProposalStatus() {
  policyMessage("");
  if (policiesEqual(activeNetworkPolicy, proposedNetworkPolicy)) {
    $("policy-status").textContent = "Proposal matches current policy. Nothing changes until Replace active policy with proposal runs.";
    return;
  }
  $("policy-status").textContent = "Proposal has unapplied changes. The active proxy policy is unchanged until you replace it.";
}

function resetProposedPolicy() {
  proposedNetworkPolicy = clonePolicy(activeNetworkPolicy);
  clearWebsiteRuleForm();
  renderPolicyControls();
}

function sameStringArray(left, right) {
  left = left || [];
  right = right || [];
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function rulesEqual(left, right) {
  return sameStringArray(left && left.allow_http_methods, right && right.allow_http_methods) &&
    sameStringArray(left && left.path_guards, right && right.path_guards);
}

function wildcardCoversDomain(pattern, domain) {
  return pattern.startsWith("*.") && domain.endsWith(pattern.slice(1)) && domain !== pattern.slice(2);
}

function hasWildcardOverlap(rules, domain) {
  return Object.keys(rules).some(pattern => wildcardCoversDomain(pattern, domain));
}

function policyPresetState(name) {
  const preset = POLICY_PRESETS[name];
  if (!preset) return "missing";
  const managed = proposedNetworkPolicy.managed_ai_provider_network_access || {};
  const managedEntries = Object.entries(preset.managed || {}).filter(([_provider, enabled]) => enabled);
  if (managedEntries.length) {
    return managedEntries.every(([provider]) => managed[provider] === true) ? "active" : "missing";
  }
  const rules = proposedNetworkPolicy.allowed_network_access || {};
  const domains = Object.keys(preset.rules || {});
  const existing = domains.filter(domain => Object.prototype.hasOwnProperty.call(rules, domain));
  if (existing.length === domains.length && domains.every(domain => rulesEqual(rules[domain], preset.rules[domain]))) {
    return "active";
  }
  const wildcardCovered = domains.filter(domain =>
    !Object.prototype.hasOwnProperty.call(rules, domain) && hasWildcardOverlap(rules, domain)
  );
  if (!existing.length && !wildcardCovered.length) return "missing";
  return "partial";
}

function renderPolicyPresets() {
  for (const [name, copy] of Object.entries(POLICY_PRESET_BUTTONS)) {
    const button = $(`policy-preset-${name}`);
    const state = policyPresetState(name);
    button.classList.remove("preset-active", "preset-partial");
    button.disabled = state === "partial";
    if (state === "active") {
      button.classList.add("preset-active");
      button.textContent = `Remove ${copy.label}`;
      button.title = "Remove this preset from the proposed policy.";
    } else if (state === "partial") {
      button.classList.add("preset-partial");
      button.textContent = copy.partial;
      button.title = "One or more preset domains already exist or are covered by a wildcard in the proposal. Edit website rules manually to avoid overwriting custom policy.";
    } else {
      button.textContent = `Add ${copy.label}`;
      button.title = "Add this preset to the proposed policy. It will not take effect until you replace the active policy.";
    }
  }
}

function cloneRule(rule) {
  const cloned = {"allow_http_methods": [...(rule.allow_http_methods || [])]};
  if (rule.path_guards && rule.path_guards.length) cloned.path_guards = [...rule.path_guards];
  return cloned;
}

function applyPolicyPreset(name) {
  const preset = POLICY_PRESETS[name];
  if (!preset) return;
  const state = policyPresetState(name);
  if (state === "active") {
    removePolicyPreset(preset);
    renderPolicyControls();
    return;
  }
  if (state !== "missing") {
    policyMessage("Preset overlaps the proposed policy. Edit the website rules manually.");
    return;
  }
  proposedNetworkPolicy.managed_ai_provider_network_access = objectValue(proposedNetworkPolicy.managed_ai_provider_network_access);
  for (const [provider, enabled] of Object.entries(preset.managed || {})) {
    if (enabled) proposedNetworkPolicy.managed_ai_provider_network_access[provider] = true;
  }
  proposedNetworkPolicy.allowed_network_access = objectValue(proposedNetworkPolicy.allowed_network_access);
  for (const [domain, rule] of Object.entries(preset.rules || {})) {
    proposedNetworkPolicy.allowed_network_access[domain] = cloneRule(rule);
  }
  renderPolicyControls();
}

function removePolicyPreset(preset) {
  const managed = objectValue(proposedNetworkPolicy.managed_ai_provider_network_access);
  for (const [provider, enabled] of Object.entries(preset.managed || {})) {
    if (enabled) delete managed[provider];
  }
  proposedNetworkPolicy.managed_ai_provider_network_access = managed;

  const rules = objectValue(proposedNetworkPolicy.allowed_network_access);
  for (const [domain, rule] of Object.entries(preset.rules || {})) {
    if (rulesEqual(rules[domain], rule)) delete rules[domain];
  }
  proposedNetworkPolicy.allowed_network_access = rules;
}

function clearWebsiteRuleForm() {
  $("policy-domain").value = "";
  $("policy-methods").value = "";
  $("policy-path-guards").value = "";
}

function saveWebsiteRule() {
  const domain = $("policy-domain").value.trim().toLowerCase();
  const methods = $("policy-methods").value.split(",").map(value => value.trim().toUpperCase()).filter(Boolean);
  const pathGuards = $("policy-path-guards").value.split("\n").map(value => value.trim()).filter(Boolean);
  if (!domain || !methods.length) { policyMessage("Website and at least one HTTP method are required."); return; }
  proposedNetworkPolicy.allowed_network_access = proposedNetworkPolicy.allowed_network_access || {};
  const rule = {"allow_http_methods": methods};
  if (pathGuards.length) rule.path_guards = pathGuards;
  proposedNetworkPolicy.allowed_network_access[domain] = rule;
  clearWebsiteRuleForm();
  renderPolicyControls();
}

function loadPolicyFromJsonEditor() {
  try {
    proposedNetworkPolicy = normalizePolicy(JSON.parse($("policy").value));
    renderPolicyPresets();
    renderProposalStatus();
    renderRuntimeGuidance();
  } catch (_error) {
    // Keep raw JSON editing permissive until Save validates the exact payload.
  }
}

async function savePolicy() {
  if (!confirm("Replace the active network policy with the proposed policy?")) return;
  try {
    const response = await api("PUT", "/v1/network/policy", JSON.parse($("policy").value));
    activeNetworkPolicy = normalizePolicy(response.network_controls || proposedNetworkPolicy);
    proposedNetworkPolicy = clonePolicy(activeNetworkPolicy);
    renderPolicyControls();
    policyMessage("Active network policy replaced.");
    // Runtime states change with the policy; reflect that now, not at the next poll.
    refreshHealth().catch(() => {});
    refreshProviderAccounts().catch(() => {});
  } catch (error) { policyMessage(error.message); }
}

async function tick() {
  try {
    await refreshHealth();
    await refreshProviderAccounts();
    await loadThreads();
    await refreshSelectedThread();
    await refreshEvents();
    await refreshNetEvents();
    if (activeTab === "files") await loadAgentFiles(currentFilePath);
  } catch (error) { /* shown via login panel or notice */ }
}

function start() {
  if (!getPassword()) { showLogin(); return; }
  $("login").hidden = true;
  $("app").hidden = false;
  $("logout-button").hidden = false;
  updateComposer();
  renderThreadHistory();
  loadPolicy().catch(() => {});
  tick();
  setInterval(tick, 5000);
}

document.addEventListener("click", event => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const button = target.closest("button[data-action]");
  if (!button) return;
  const { action } = button.dataset;
  const taskId = button.dataset.taskId;
  const threadId = button.dataset.threadId;
  const runtime = button.dataset.runtime;
  const preset = button.dataset.preset || button.dataset.info;
  const path = button.dataset.path;
  const fileType = button.dataset.fileType;
  const actions = {
    "login": () => login(),
    "logout": () => logout(),
    "show-tab": () => showTab(button.dataset.tab),
    "start-login": () => startLogin(runtime),
    "complete-claude-login": () => completeClaudeLogin(),
    "reboot-host": () => rebootHost(),
    "create-task": () => createTask(),
    "steer-task": () => steerTask(taskId),
    "kill-task": () => killTask(taskId),
    "cancel-task": () => cancelTask(taskId),
    "new-thread": () => newThread(),
    "show-thread": () => showThread(threadId, runtime),
    "show-task-events": () => showTaskEvents(taskId),
    "load-more-task-events": () => loadMoreTaskEvents(taskId),
    "file-up": () => loadAgentFiles(parentPath(currentFilePath)),
    "file-go": () => goToFilePath(),
    "file-refresh": () => loadAgentFiles(currentFilePath),
    "open-file-path": () => openAgentPath(path, fileType),
    "load-policy": () => loadPolicy(),
    "reset-proposed-policy": () => resetProposedPolicy(),
    "apply-policy-preset": () => applyPolicyPreset(preset),
    "toggle-preset-info": () => togglePresetInfo(preset, button),
    "save-website-rule": () => saveWebsiteRule(),
    "save-policy": () => savePolicy(),
  };
  const handler = actions[action];
  if (handler) handler();
});

$("policy").addEventListener("input", loadPolicyFromJsonEditor);
$("password").addEventListener("keydown", event => { if (event.key === "Enter") login(); });
$("file-path").addEventListener("keydown", event => { if (event.key === "Enter") goToFilePath(); });
start();
