// Host health plus the always-visible top-bar runtime status and usage.

import { api } from "./api.js";
import {
  $, badge, clampPercent, esc, gib, notice, setHtml, RUNTIME_PROVIDERS,
} from "./helpers.js";
import { renderIntegrationAccounts } from "./network.js";

let latestRuntimes = [];
let latestAccounts = [];

export function providerAccounts() {
  return latestAccounts;
}

export function runtimeRecords() {
  return latestRuntimes;
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
  renderUpgradeNotice(health.upgrade);
  $("agent-name").textContent = health.agent_name ? `Host: ${health.agent_name}` : "";
  $("agent-name").hidden = !health.agent_name;
  const runtimes = Array.isArray(health.agent_runtime.runtimes) ? health.agent_runtime.runtimes : [];
  latestRuntimes = runtimes;
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
  renderRuntimeOverview();
  renderIntegrationAccounts();
  // A pending login survives page reloads and provider-card re-renders: every
  // poll re-shows it inside the expanded provider card (showOauth is a no-op
  // while the card is collapsed, and a GET never starts a new login).
  for (const pending of runtimes.filter(runtime => runtime.status === "awaiting_login")) {
    await showOauth(false, pending.type);
  }
}

function renderUpgradeNotice(upgrade) {
  const notice = $("upgrade-notice");
  const checked = typeof upgrade?.latest === "string";
  notice.hidden = !checked;
  if (!checked) return;
  const available = upgrade.available === true;
  const title = available
    ? `Upgrade available: version ${upgrade.latest}`
    : "Your TrustyClaw is at the latest version.";
  const detail = available ? "Use your operator plane to upgrade." : "";
  const label = detail ? `${title}. ${detail}` : title;
  notice.classList.toggle("upgrade-available", available);
  notice.classList.toggle("upgrade-current", !available);
  $("upgrade-popover-title").textContent = title;
  $("upgrade-popover-detail").textContent = detail;
  $("upgrade-popover-detail").hidden = !detail;
  notice.setAttribute("aria-label", label);
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
  renderRuntimeOverview();
  renderIntegrationAccounts();
}

export async function refreshProviderUsage() {
  const response = await api("POST", "/v1/agent-runtime/refresh", {});
  renderProviderAccounts(response);
  await refreshHealth();
}

function renderRuntimeOverview() {
  const container = $("runtime-overview");
  if (!container) return;
  const runtimes = ["codex", "claude_code"].map(runtime => {
    const meta = RUNTIME_PROVIDERS[runtime];
    const record = latestRuntimes.find(entry => entry.type === runtime) || { status: "loading" };
    const account = latestAccounts.find(entry => entry.agent_runtime === runtime) || {};
    const windows = usageWindows(account);
    const running = Array.isArray(record.active_task_ids) ? record.active_task_ids.length : 0;
    const statusText = String(record.status || "loading").replaceAll("_", " ");
    const modelSummary = windows.fableWeekly
      ? `; ${usageSummary(`${windows.fableWeekly.label} weekly`, windows.fableWeekly)}` : "";
    const runningLabel = running ? `; ${running} running` : "";
    // The running count is a corner badge rather than inline text so a long
    // status ("awaiting login") never truncates it away.
    const runningBadge = running
      ? `<span class="runtime-running-badge" aria-hidden="true">${running} running</span>` : "";
    const inner = `
        <span class="runtime-summary-name">
          <span class="runtime-status-dot ${esc(record.status)}" aria-hidden="true"></span>
          <span class="runtime-summary-copy">
            <span>${esc(meta.label)}</span>
            <span class="runtime-state">${esc(statusText)}</span>
          </span>
        </span>
        <span class="runtime-usage">
          ${usageRing("5h", windows.fiveHour)}
          ${usageRing("wk", windows.weekly)}
          ${windows.fableWeekly ? usageRing(windows.fableWeekly.label, windows.fableWeekly) : ""}
        </span>
        ${runningBadge}`;
    // Only a deactivated runtime has somewhere worth navigating to: the
    // Internet Access and Tools tab, to re-enable its managed integration. An
    // active, logging-in, or errored runtime needs no navigation, so it is a
    // static chip, not a button.
    if (record.status === "deactivated") {
      const summaryLabel = `${meta.label}: ${statusText}${runningLabel}; ${usageSummary("5 hour", windows.fiveHour)}; ${usageSummary("weekly", windows.weekly)}${modelSummary}. Open account settings`;
      return `
      <button class="runtime-summary" data-action="open-provider" data-provider="${esc(meta.provider)}" aria-label="${esc(summaryLabel)}">${inner}</button>`;
    }
    const summaryLabel = `${meta.label}: ${statusText}${runningLabel}; ${usageSummary("5 hour", windows.fiveHour)}; ${usageSummary("weekly", windows.weekly)}${modelSummary}`;
    return `
      <div class="runtime-summary is-static" role="group" aria-label="${esc(summaryLabel)}">${inner}</div>`;
  }).join("");
  setHtml(container, `${runtimes}
    <button class="ghost sm icon-button runtime-refresh" data-action="refresh-provider-usage" title="Refresh provider status and usage" aria-label="Refresh provider status and usage">
      <svg width="16" height="16" viewBox="0 0 20 20" aria-hidden="true"><path d="M16.2 6.5A6.8 6.8 0 1 0 17 10M16.2 3.5v3h-3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>`);
}

function usageWindows(account) {
  if (account.agent_runtime === "claude_code") {
    const usage = account.claude_usage || {};
    return {
      fiveHour: {
        usedPercent: usage.current_session_used_percent,
        resetsAt: usage.current_session_resets_at,
      },
      weekly: {
        usedPercent: usage.weekly_used_percent,
        resetsAt: usage.weekly_resets_at,
      },
      // The Fable-specific weekly window; shown only when the usage snapshot
      // carries one.
      fableWeekly: usage.fable_weekly_used_percent === undefined ? null : {
        label: "fable",
        usedPercent: usage.fable_weekly_used_percent,
        resetsAt: usage.fable_weekly_resets_at,
      },
    };
  }
  const limits = account.codex_usage?.rate_limits || {};
  const windows = Object.values(limits).filter(value => value && typeof value === "object");
  // Windows are identified by duration, not by primary/secondary position;
  // Number() tolerates a snapshot serializing durations as strings.
  const fiveHour = windows.find(window => Number(window.window_duration_mins) === 300);
  const weekly = windows.find(window => Number(window.window_duration_mins) === 10080);
  return {
    fiveHour: { usedPercent: fiveHour?.used_percent, resetsAt: fiveHour?.resets_at },
    weekly: { usedPercent: weekly?.used_percent, resetsAt: weekly?.resets_at },
    fableWeekly: null,
  };
}

function usageLabel(value) {
  return value !== undefined && value !== null && Number.isFinite(Number(value))
    ? `${Number(clampPercent(value))}% used`
    : "usage unavailable";
}

function resetCountdown(value, now = Date.now()) {
  if (value === undefined || value === null || value === "") return "";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "";
  const remaining = numeric * 1000 - now;
  if (remaining <= 0) return "due";
  const minutes = Math.max(1, Math.ceil(remaining / 60000));
  if (minutes >= 24 * 60) return `${Math.ceil(minutes / (24 * 60))}d`;
  if (minutes >= 60) return `${Math.ceil(minutes / 60)}h`;
  return `${minutes}m`;
}

function usageSummary(label, window) {
  const countdown = resetCountdown(window.resetsAt);
  const reset = countdown === "due" ? "; reset due" : countdown ? `; resets in ${countdown}` : "";
  return `${label} ${usageLabel(window.usedPercent)}${reset}`;
}

function usageRing(label, window) {
  const value = window.usedPercent;
  const available = value !== undefined && value !== null && Number.isFinite(Number(value));
  const percent = available ? Number(clampPercent(value)) : 0;
  const display = available ? `${Math.round(percent)}%` : "--";
  const countdown = resetCountdown(window.resetsAt);
  const resetDescription = countdown === "due" ? "; reset due" : countdown ? `; resets in ${countdown}` : "";
  const title = available ? `${label}: ${percent}% used${resetDescription}` : `${label}: usage unavailable`;
  const thresholdClass = percent > 90 ? " usage-critical" : percent > 80 ? " usage-warning" : "";
  // One label line whether or not a countdown is known, so the ring block
  // (and with it the top bar) keeps a constant height.
  return `
    <span class="usage-ring${available ? thresholdClass : " unavailable"}">
      <svg viewBox="0 0 36 36" role="img" aria-label="${esc(title)}">
        <circle class="usage-ring-track" cx="18" cy="18" r="15.5" pathLength="100"></circle>
        <circle class="usage-ring-value" cx="18" cy="18" r="15.5" pathLength="100" stroke-dasharray="${percent} 100"></circle>
        <text x="18" y="18">${esc(display)}</text>
      </svg>
      <span class="usage-window">${esc(label)}${countdown ? ` · ${countdown}` : ""}</span>
    </span>`;
}

async function showOauth(start, runtime) {
  const provider = runtime === "claude_code" ? "claude" : "openai";
  const target = document.querySelector(`[data-provider-oauth="${provider}"]`);
  if (!target) return;
  try {
    if (runtime === "claude_code") {
      const login = await api(start ? "POST" : "GET", "/v1/agent-runtime/claude-oauth-login");
      setHtml(target, `<div class="oauth-card">
        <span>Claude Code login: open
        <a href="${esc(login.login_url)}" target="_blank">${esc(login.login_url)}</a>
        <span class="muted">(expires ${esc(login.expires_at)})</span></span>
        <button class="primary sm" data-action="complete-claude-login">Submit code</button></div>`);
      return;
    }
    const login = await api(start ? "POST" : "GET", "/v1/agent-runtime/codex-oauth-login");
    setHtml(target, `<div class="oauth-card">
      <span>Codex login: enter code <b>${esc(login.device_code)}</b> at
      <a href="${esc(login.login_url)}" target="_blank">${esc(login.login_url)}</a>
      <span class="muted">(expires ${esc(login.expires_at)})</span>
      <span class="muted">After approving in your browser, wait ~5 seconds for the status to update.</span></span></div>`);
  } catch (error) { if (start) notice(error.message, "error"); }
}

export async function startLogin(runtime) { await showOauth(true, runtime); }

export async function completeClaudeLogin() {
  const code = prompt("Claude Code login code:");
  if (!code) return;
  try {
    await api("POST", "/v1/agent-runtime/claude-oauth-login/complete", { code });
    notice("Claude Code login submitted.");
    await refreshHealth();
  } catch (error) { notice(error.message, "error"); }
}

export async function rebootHost() {
  if (!confirm("Reboot the host machine?")) return;
  try { await api("POST", "/v1/host-runtime/reboot"); notice("Reboot accepted; the host will be back shortly."); }
  catch (error) { notice(error.message, "error"); }
}
