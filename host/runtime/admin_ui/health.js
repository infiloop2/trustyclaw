// Home tab: host health tiles, runtime cards, provider accounts and usage,
// OAuth logins, runtime guidance, and the reboot control.

import { api } from "./api.js";
import {
  $, badge, clampPercent, esc, formatDateTime, formatUnixTime, gib, notice,
  objectValue, setHtml, RUNTIME_PROVIDERS,
} from "./helpers.js";
import { activePolicy, renderIntegrationAccounts } from "./network.js";

let latestRuntimes = [];
let latestAccounts = [];
let runtimeType = "codex";

export function providerAccounts() {
  return latestAccounts;
}

function statTile(label, valueHtml, extraClass = "") {
  return `<div class="stat-tile${extraClass ? " " + extraClass : ""}"><div class="stat-label">${esc(label)}</div><div class="stat-value">${valueHtml}</div></div>`;
}

function meterTile(label, valueHtml, percent) {
  const clamped = Math.max(0, Math.min(100, percent));
  return `
    <div class="stat-tile">
      <div class="stat-label">${esc(label)}</div>
      <div class="stat-value stat-meter-value">${valueHtml}</div>
      <progress class="meter${clamped >= 80 ? " hot" : ""}" max="100" value="${clamped.toFixed(1)}"></progress>
    </div>`;
}

function usageTile(label, used, total, totalLabel) {
  const unit = totalLabel || "GiB";
  const value = `<span class="metric-main">${esc(gib(used))} ${esc(unit)}</span><span class="metric-total">of ${esc(gib(total))} ${esc(unit)}</span>`;
  return meterTile(label, value, total > 0 ? (used / total) * 100 : 0);
}

function memorySwapTile(memory, swap) {
  const memoryUsed = Number(memory?.used_bytes) || 0;
  const memoryTotal = Number(memory?.total_bytes) || 0;
  const swapUsed = Number(swap?.used_bytes) || 0;
  const swapTotal = Number(swap?.allocated_bytes) || 0;
  const combinedTotal = memoryTotal + swapTotal;
  const usedPercent = combinedTotal > 0 ? ((memoryUsed + swapUsed) / combinedTotal) * 100 : 0;
  const clamped = clampPercent(usedPercent);
  return `
    <div class="stat-tile memory-swap-tile">
      <div class="stat-label">Memory</div>
      <div class="memory-swap-values">
        <div>
          <span class="metric-main">${esc(gib(memoryUsed))} GiB</span>
          <span class="metric-total">memory of ${esc(gib(memoryTotal))} GiB</span>
        </div>
        <div>
          <span class="metric-main">${esc(gib(swapUsed))} GiB</span>
          <span class="metric-total">swap of ${esc(gib(swapTotal))} GiB</span>
        </div>
      </div>
      <progress class="meter${Number(clamped) >= 80 ? " hot" : ""}" max="100" value="${esc(clamped)}"></progress>
    </div>`;
}

function filesystemMountTile(label, mount) {
  if (!mount || typeof mount !== "object") {
    return statTile(label, `<span class="muted">not mounted</span>`);
  }
  return usageTile(label, mount.used_bytes, mount.total_bytes);
}

export async function refreshHealth() {
  const health = await api("GET", "/v1/health");
  $("agent-name").textContent = health.agent_name ? `Agent: ${health.agent_name}` : "";
  $("agent-name").hidden = !health.agent_name;
  const runtimes = Array.isArray(health.agent_runtime.runtimes) ? health.agent_runtime.runtimes : [];
  latestRuntimes = runtimes;
  runtimeType = runtimes.find(r => r.status === "active")?.type || runtimes[0]?.type || "codex";
  const host = health.host_runtime;
  const mounts = host.filesystem?.mounts || {};
  setHtml($("health"), `
    <div class="stat-grid stat-statuses">
      ${statTile("Overall", badge(health.status))}
      ${statTile("Network controls", badge(health.network_controls.status))}
      ${statTile("Version", renderVersion(health.version), "stat-wide")}
    </div>
    <div class="stat-grid stat-meters">
      ${meterTile("CPU", `<span class="metric-main">${esc(host.cpu.usage_percent)}%</span>`, Number(host.cpu.usage_percent) || 0)}
      ${memorySwapTile(host.memory, host.swap)}
      ${filesystemMountTile("Admin volume", mounts.admin)}
      ${filesystemMountTile("Agent volume", mounts.agent)}
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

export async function refreshProviderAccounts() {
  const response = await api("GET", "/v1/agent-runtime/account");
  renderProviderAccounts(response);
}

function renderProviderAccounts(response) {
  latestAccounts = Array.isArray(response.accounts) ? response.accounts : [];
  renderIntegrationAccounts();
  if (!latestAccounts.length) {
    setHtml($("provider-accounts"), `<tr><td class="empty-state">No provider accounts.</td></tr>`);
    return;
  }
  setHtml($("provider-accounts"), `
    <tr><th>runtime</th><th>account</th><th>plan</th><th>usage</th></tr>
    ${latestAccounts.map(renderProviderAccountRow).join("")}`);
}

export async function refreshProviderUsage() {
  const response = await api("POST", "/v1/agent-runtime/refresh", {});
  renderProviderAccounts(response);
  await refreshHealth();
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
    // Login can also start from error: a changed account or malformed local
    // credentials are recovered by logging in again.
    const canStart = statuses[runtime] === "awaiting_login" || statuses[runtime] === "error";
    button.hidden = !canStart;
    button.disabled = !canStart;
  }
}

function providerEnabled(policy, provider) {
  const managed = objectValue(policy ? policy.managed_network_integrations : null);
  return objectValue(managed[provider]).enabled === true;
}

export function renderRuntimeGuidance(runtimes = latestRuntimes) {
  const messages = [];
  for (const runtime of runtimes) {
    const meta = RUNTIME_PROVIDERS[runtime.type];
    if (!meta) continue;
    if (runtime.status === "deactivated" && !providerEnabled(activePolicy(), meta.provider)) {
      messages.push(runtimeGuidance(`${esc(meta.label)} is deactivated because ${esc(meta.providerLabel)} provider access is disabled in the active network policy. Enable the ${esc(meta.providerLabel)} integration to activate this runtime.`));
    } else if (runtime.status === "error") {
      messages.push(runtimeGuidance(`${esc(meta.label)} error: ${esc(runtime.error_message || "the last status check failed")}. Start a new login to recover, or reset the linked ${esc(meta.providerLabel)} account under Internet Access and Tools to unlink it first.`));
    }
  }
  setHtml($("runtime-guidance"), messages.length ? `<div class="runtime-guidance-list">${messages.join("")}</div>` : "");
}

function runtimeGuidance(html) {
  return `
    <div class="runtime-guidance">
      <p>${html}</p>
      <div class="actions">
        <button class="ghost sm" data-action="show-tab" data-tab="network">Open Internet Access and Tools</button>
      </div>
    </div>`;
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
      <span class="muted">(expires ${esc(login.expires_at)})</span>
      <span class="muted">After approving in your browser, wait ~5 seconds for the status to update.</span></span></div>`);
  } catch (error) { if (start) notice(error.message); }
}

export async function startLogin(runtime) { await showOauth(true, runtime); }

export async function completeClaudeLogin() {
  const code = prompt("Claude Code login code:");
  if (!code) return;
  try {
    await api("POST", "/v1/agent-runtime/claude-oauth-login/complete", { code });
    notice("Claude Code login submitted.");
    await refreshHealth();
  } catch (error) { notice(error.message); }
}

export async function rebootHost() {
  if (!confirm("Reboot the host machine?")) return;
  try { await api("POST", "/v1/host-runtime/reboot"); notice("Reboot accepted; the host will be back shortly."); }
  catch (error) { notice(error.message); }
}
