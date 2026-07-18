// Bundled tools UI: one integration row per tool in the network tab, styled
// exactly like the managed integrations (GitHub, OpenAI, ...): a collapsed
// summary with status and enable/disable, and a chevron dropdown holding the
// OAuth connection, a Configuration section, and an Approvals section, so
// pending write actions are shown per tool rather than in one unified list.
// Direct actions return immediately; approval-gated actions queue an operator
// decision in that tool's Approvals section. Tool state is fetched from
// /v1/tools and rendered on tab entry and after actions only, never on the
// poll timer, so half-typed config survives; a tool's approvals load when its
// row is expanded and refresh on the poll while it stays open.

import { api } from "./api.js";
import { $, badge, esc, formatUnixTime, informationIcon, inlineCode, inlineMessage, notice, replaceIntegrationRows, setHtml } from "./helpers.js";
import { closeIntegrationInfo, toggleInfoPopover } from "./network.js";

let tools = [];
// tool_id -> approvals array, for tools whose row is expanded.
const toolApprovalsByTool = new Map();
// Tool rows are collapsed by default, like the managed integration rows.
const expandedTools = new Set();

function toolsMessage(toolId, message, isError) {
  const node = document.querySelector(`[data-tool-message="${toolId}"]`);
  inlineMessage(node, message, isError);
}

export async function refreshTools() {
  const response = await api("GET", "/v1/tools");
  tools = Array.isArray(response.tools) ? response.tools : [];
  renderTools();
}

function renderTools() {
  // Re-rendering replaces the info buttons, so drop any open popover.
  closeIntegrationInfo();
  $("tools-empty").hidden = tools.length > 0;
  if (!tools.length) {
    replaceIntegrationRows($("tools"), "[data-tool-row]", "");
    return;
  }
  const sortedTools = [...tools]
    .sort((left, right) => left.display_name.localeCompare(right.display_name, undefined, { sensitivity: "base" }));
  replaceIntegrationRows($("tools"), "[data-tool-row]", sortedTools.map(renderToolRow).join(""));
  // Re-rendering the rows empties each expanded row's approvals table, so
  // repaint them from the cached approvals; the poll and actions refresh the
  // data.
  for (const toolId of expandedTools) renderToolApprovalsTable(toolId);
}

function renderToolRow(tool) {
  const expanded = expandedTools.has(tool.tool_id);
  const connected = tool.connection_status && tool.connection_status.connected === true;
  const chips = [badge(tool.enabled ? "enabled" : "disabled")];
  if (tool.connection === "oauth" && (tool.enabled || connected)) {
    const account = (tool.connection_status && tool.connection_status.account) || {};
    chips.push(connected
      ? `<span class="status active">connected: <span class="chip-label">${esc(account.label || "")}</span></span>`
      : `<span class="status">not connected</span>`);
  }
  return `
    <section class="integration-row${expanded ? " expanded" : ""}" data-tool-row="${esc(tool.tool_id)}">
      <div class="integration-summary">
        <button class="ghost sm icon-button integration-chevron" data-action="toggle-tool-expansion" data-tool="${esc(tool.tool_id)}" aria-label="Toggle ${esc(tool.display_name)} details" aria-expanded="${expanded}">
          <svg width="16" height="16" viewBox="0 0 20 20" aria-hidden="true"><path d="m7.5 4.5 5 5.5-5 5.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
        <div class="integration-title">
          <div class="preset-with-info">
            <h2>${esc(tool.display_name)}</h2>
            <button class="info-button" data-action="toggle-tool-info" data-tool="${esc(tool.tool_id)}" data-info="tool:${esc(tool.tool_id)}" aria-label="${esc(tool.display_name)} overview and protections" aria-haspopup="dialog" aria-expanded="false">${informationIcon()}</button>
          </div>
          <div class="integration-subtitle">${esc(tool.description)}</div>
        </div>
        <span class="status-chips">${chips.join(" ")}</span>
        <span class="integration-actions">
          <span class="seg">
            <button data-action="enable-tool" data-tool="${esc(tool.tool_id)}"${tool.enabled ? " disabled" : ""}>Enable</button>
            <button data-action="disable-tool" data-tool="${esc(tool.tool_id)}"${tool.enabled ? "" : " disabled"}>Disable</button>
          </span>
        </span>
      </div>
      <p class="inline-message integration-row-message" data-tool-message="${esc(tool.tool_id)}" role="status" aria-live="polite"></p>
      <div class="integration-details" data-tool-details="${esc(tool.tool_id)}"${expanded ? "" : " hidden"}>
        <p class="muted">${esc(tool.description)}</p>
        ${tool.connection === "oauth" && (tool.enabled || connected) ? `
        <div class="detail-card">
          <div class="detail-card-head"><h3>Connection</h3></div>
          ${renderToolConnection(tool, connected)}
        </div>` : ""}
        ${tool.config.length ? `
        <div class="detail-card">
          <div class="detail-card-head"><h3>Configuration</h3></div>
          <div class="tool-config">${tool.config.map(entry => renderToolConfigRow(tool, entry)).join("")}</div>
        </div>` : ""}
        <div class="detail-card">
          <div class="detail-card-head"><h3>Approvals</h3></div>
          <div class="tool-approvals" data-tool-approvals="${esc(tool.tool_id)}">
            <div class="table-scroll"><table class="tool-approvals-table"></table></div>
          </div>
        </div>
      </div>
    </section>`;
}

// The OAuth connection line mirrors the provider linked-account line in the
// managed integration dropdowns. Disconnect stays available whenever an
// account is connected, even if the tool was later disabled or its config
// cleared, so the operator always has a path to revoke and delete stored
// tokens (the backend allows it too). Connect requires the tool to be enabled.
function renderToolConnection(tool, connected) {
  const account = (tool.connection_status && tool.connection_status.account) || {};
  if (connected) {
    return `
      <div class="integration-account">
        <p class="connection-summary">Connected account: <span class="connection-identity">${esc(account.label || "")}</span></p>
        <button class="ghost sm" data-action="disconnect-tool" data-tool="${esc(tool.tool_id)}">Disconnect</button>
      </div>`;
  }
  return `
    <div class="integration-account">
      <p class="connection-summary">No account connected yet. Connect signs in on the provider's site and stores the tokens on the host.</p>
      <button class="primary sm" data-action="connect-tool" data-tool="${esc(tool.tool_id)}">Connect</button>
    </div>`;
}

// Expand/collapse without re-rendering, so half-typed config values in other
// rows survive the toggle (only refreshTools rebuilds the rows). Expanding
// loads that tool's approvals; they refresh on the poll while the row is open.
export function toggleToolExpansion(toolId) {
  if (expandedTools.has(toolId)) expandedTools.delete(toolId);
  else expandedTools.add(toolId);
  const expanded = expandedTools.has(toolId);
  const row = document.querySelector(`.integration-row[data-tool-row="${cssEscape(toolId)}"]`);
  if (row) row.classList.toggle("expanded", expanded);
  const details = document.querySelector(`.integration-details[data-tool-details="${cssEscape(toolId)}"]`);
  if (details) details.hidden = !expanded;
  const chevron = document.querySelector(`.integration-chevron[data-action="toggle-tool-expansion"][data-tool="${cssEscape(toolId)}"]`);
  if (chevron) chevron.setAttribute("aria-expanded", String(expanded));
  if (expanded) loadToolApprovals(toolId).catch(error => toolsMessage(toolId, error.message, true));
}


function renderToolConfigRow(tool, entry) {
  const inputId = `tool-config-${tool.tool_id}-${entry.key}`;
  return `
    <div class="tool-config-row">
      <label class="field" for="${esc(inputId)}">
        <span class="config-key mono">${esc(entry.key)} ${entry.set ? `<span class="status active">set</span>` : `<span class="status">not set</span>`}</span>
        <span class="muted config-note">${esc(entry.description)}</span>
      </label>
      <div class="config-input-row">
        <input id="${esc(inputId)}" type="password"
               placeholder="${entry.set ? "configured (enter to replace, blank to clear)" : "not configured"}" spellcheck="false">
        <button class="sm" data-action="save-tool-config" data-tool="${esc(tool.tool_id)}" data-key="${esc(entry.key)}">Save</button>
      </div>
    </div>`;
}

// Tool details open in the same floating popover as the managed integration
// info buttons, anchored to the clicked button.
export function toggleToolInfo(toolId, anchor) {
  const tool = tools.find(item => item.tool_id === toolId);
  if (!tool) return;
  toggleInfoPopover(`tool:${toolId}`, anchor, renderToolInfo(tool));
}

function renderToolInfo(tool) {
  return `
    <h3>${esc(tool.display_name)}</h3>
    <h4>Protections</h4>
    <ul>${(tool.protections || []).map(protection => `<li>${inlineCode(protection)}</li>`).join("")}</ul>
    <button class="popover-guide-link" data-action="open-connection-guide" data-guide="tool:${esc(tool.tool_id)}">View integration guide</button>`;
}

export async function setToolEnabled(toolId, enabled) {
  const label = tools.find(tool => tool.tool_id === toolId)?.display_name || toolId;
  try {
    toolsMessage(toolId, "");
    await api("POST", `/v1/tools/${encodeURIComponent(toolId)}/${enabled ? "enable" : "disable"}`, {});
    await refreshTools();
    toolsMessage(toolId, `${label} ${enabled ? "enabled" : "disabled"}.`);
  } catch (error) { toolsMessage(toolId, error.message, true); }
}

export async function saveToolConfig(toolId, key) {
  const input = $(`tool-config-${toolId}-${key}`);
  const value = input.value.trim();
  try {
    toolsMessage(toolId, "");
    await api("PUT", `/v1/tools/${encodeURIComponent(toolId)}/config`, { key, value });
    input.value = "";
    await refreshTools();
    toolsMessage(toolId, `${key} ${value ? "saved" : "cleared"}.`);
  } catch (error) { toolsMessage(toolId, error.message, true); }
}

function oauthRedirectUri() {
  return location.origin + "/oauth/callback";
}

export async function connectTool(toolId) {
  try {
    toolsMessage(toolId, "");
    const response = await api("POST", `/v1/tools/${encodeURIComponent(toolId)}/oauth_connect/start`,
      { redirect_uri: oauthRedirectUri() });
    sessionStorage.setItem("trustyclaw_tool_connect", toolId);
    location.assign(response.authorization_url);
  } catch (error) { toolsMessage(toolId, error.message, true); }
}

export async function disconnectTool(toolId) {
  if (!confirm("Disconnect this account? Stored tokens are revoked and deleted.")) return;
  try {
    toolsMessage(toolId, "");
    await api("POST", `/v1/tools/${encodeURIComponent(toolId)}/oauth_connect/disconnect`, {});
    await refreshTools();
    toolsMessage(toolId, "Account disconnected.");
  } catch (error) { toolsMessage(toolId, error.message, true); }
}

// Finish a tool OAuth connect after the provider redirected back to
// /oauth/callback?code=...&state=... — the tool id was stashed before leaving.
// The caller (app.js start) has already switched to the network tab.
export async function completeToolConnect() {
  const params = new URLSearchParams(location.search);
  const toolId = sessionStorage.getItem("trustyclaw_tool_connect");
  sessionStorage.removeItem("trustyclaw_tool_connect");
  history.replaceState(null, "", "/");
  if (!toolId) { notice("Tool connect callback had no pending tool."); return; }
  if (!params.get("code")) {
    try { await refreshTools(); } catch (_error) { /* render the callback error if the row already exists */ }
    toolsMessage(toolId, `Connect cancelled: ${params.get("error") || "no authorization code returned"}.`, true);
    return;
  }
  let message = "";
  let isError = false;
  try {
    const result = await api("POST", `/v1/tools/${encodeURIComponent(toolId)}/oauth_connect/complete`, {
      code: params.get("code"),
      state: params.get("state") || "",
      redirect_uri: oauthRedirectUri(),
    });
    const label = result.account && result.account.label;
    message = `Connected ${toolId}${label ? ` as ${label}` : ""}.`;
  } catch (error) {
    message = error.message;
    isError = true;
  }
  try { await refreshTools(); } catch (_error) { /* show the callback result if the row already exists */ }
  toolsMessage(toolId, message, isError);
}

// Refresh every expanded tool row's approvals, called on tab entry and the
// poll tick.
export async function refreshExpandedToolApprovals() {
  await Promise.all([...expandedTools].map(toolId => loadToolApprovals(toolId).catch(() => {})));
}

async function loadToolApprovals(toolId) {
  const response = await api("GET", `/v1/tools/${encodeURIComponent(toolId)}/approvals`);
  toolApprovalsByTool.set(toolId, Array.isArray(response.approvals) ? response.approvals : []);
  renderToolApprovalsTable(toolId);
}

function renderToolApprovalsTable(toolId) {
  wireApprovalPayloadLazyRender();
  const section = document.querySelector(`.tool-approvals[data-tool-approvals="${cssEscape(toolId)}"]`);
  const table = section && section.querySelector("table.tool-approvals-table");
  if (!table) return;
  const approvals = toolApprovalsByTool.get(toolId) || [];
  table.classList.toggle("has-rows", approvals.length > 0);
  // Payloads can each be up to 64 KiB and there can be up to the pending cap of
  // them, so stringifying every payload into the DOM on every refresh would make
  // the table unusable exactly when a runaway queue needs clearing. Render the
  // <pre> empty and fill it lazily from the in-memory approval when its
  // <details> is expanded (see wireApprovalPayloadLazyRender).
  setHtml(table, approvals.length
    ? `<tr><th>time</th><th>proposed action</th><th>status</th><th></th></tr>` + approvals.map(approval => `
      <tr>
        <td class="muted time">${esc(formatUnixTime(approval.created_at))}</td>
        <td>
          <div>${esc(approval.summary)}</div>
          <details data-approval-id="${esc(approval.approval_id)}" data-tool="${esc(toolId)}"><summary class="muted">exact payload</summary><pre class="metadata"></pre></details>
        </td>
        <td>${badge(approval.status)}</td>
        <td>${approval.status === "pending" ? `<span class="approval-decisions">
          <button class="sm" data-action="decide-approval" data-tool="${esc(toolId)}" data-approval-id="${esc(approval.approval_id)}" data-decision="approve">Approve</button>
          <button class="danger ghost sm" data-action="decide-approval" data-tool="${esc(toolId)}" data-approval-id="${esc(approval.approval_id)}" data-decision="deny">Deny</button></span>` : ""}
        </td>
      </tr>`).join("")
    : `<tr><td class="empty-state">No approvals for this tool yet.</td></tr>`);
}

// document.querySelector needs a safe attribute-selector value; tool_ids are
// [a-z0-9_] so this only has to survive that set, but guard defensively.
function cssEscape(value) {
  return (window.CSS && CSS.escape) ? CSS.escape(value) : String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}

let approvalPayloadLazyWired = false;
function wireApprovalPayloadLazyRender() {
  if (approvalPayloadLazyWired) return;
  approvalPayloadLazyWired = true;
  // "toggle" does not bubble, so listen in the capture phase on the document.
  // The approvals list is summary-only; fetch the (up to 64 KiB) payload only
  // when a row is expanded, so the 5s poll never transfers every payload.
  document.addEventListener("toggle", async event => {
    const details = event.target;
    if (!(details instanceof HTMLDetailsElement) || !details.open) return;
    const approvalId = details.dataset.approvalId;
    const toolId = details.dataset.tool;
    if (!approvalId || !toolId) return;
    const pre = details.querySelector("pre.metadata");
    if (!pre || pre.dataset.filled === "1") return;
    pre.dataset.filled = "1";
    try {
      const response = await api("GET", `/v1/tools/${encodeURIComponent(toolId)}/approvals/${encodeURIComponent(approvalId)}`);
      pre.textContent = JSON.stringify(response.approval.payload, null, 2);
    } catch (error) {
      pre.textContent = `(could not load payload: ${error.message})`;
      pre.dataset.filled = "";
    }
  }, true);
}

export async function decideToolApproval(toolId, approvalId, decision) {
  if (decision === "approve" && !confirm("Approve this action? It runs immediately, exactly as recorded.")) return;
  try {
    toolsMessage(toolId, "");
    const response = await api("POST", `/v1/tools/${encodeURIComponent(toolId)}/approvals/${encodeURIComponent(approvalId)}/${decision}`, {});
    const result = response.result;
    if (result && result.status === "failed") toolsMessage(toolId, `Approved action failed: ${result.error}`, true);
    else toolsMessage(toolId, decision === "approve" ? "Approved and executed." : "Denied.");
  } catch (error) { toolsMessage(toolId, error.message, true); }
  try { await loadToolApprovals(toolId); } catch (_error) { /* keep the row feedback visible */ }
}
