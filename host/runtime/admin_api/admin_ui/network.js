// Internet Access and Tools tab: the active network policy (managed
// integrations and manual domain rules), the GitHub write-repository list
// with per-repository audits, and the GitHub credential controls.

import { api } from "./api.js";
import { $, badge, bedrockUsage, esc, formatTokenCount, informationIcon, inlineCode, inlineMessage, objectValue, providerRuntime, replaceIntegrationRows, runtimeLabel, RUNTIME_PROVIDERS, setHtml } from "./helpers.js";
import { providerAccounts, refreshHealth, refreshProviderAccounts, runtimeRecords } from "./health.js";
import { CUSTOM_DOMAIN_GUIDE, MANAGED_INTEGRATIONS, integrationInfo } from "./integration_catalog.js";

const GITHUB_REPO_INPUT_RE = /^([a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?)\/([a-z0-9._-]{1,100})$/;
// The AI Inference group in render order.
const INFERENCE_INTEGRATIONS = ["openai", "claude", "bedrock"];
const BEDROCK_INTEGRATION = "bedrock";
// Must match SUPPORTED_REGIONS in host/network_integrations/bedrock/manifest.py.
const BEDROCK_REGIONS = ["us-east-1", "us-east-2", "us-west-2"];

let activeNetworkPolicy = {"network_integrations": {}};
let expandedIntegrations = new Set();
let expandedGithubRepoAudits = new Set();
let customDomainExpanded = false;
let latestGithubAudits = [];
let infoPopoverAnchor = null;
let bedrockCredentialMetadata = { connected: false };

export function setBedrockCredentialMetadata(value) {
  bedrockCredentialMetadata = value && typeof value === "object" ? value : { connected: false };
}

export function activePolicy() {
  return activeNetworkPolicy;
}

function policyMessage(integration, message, isError) {
  const node = document.querySelector(`[data-integration-message="${integration}"]`);
  inlineMessage(node, message, isError);
}

export function closeIntegrationInfo() {
  const panel = $("preset-info-popover");
  panel.hidden = true;
  panel.dataset.integration = "";
  panel.innerHTML = "";
  panel.style.left = "";
  panel.style.top = "";
  infoPopoverAnchor = null;
  for (const button of document.querySelectorAll(".info-button")) {
    button.setAttribute("aria-expanded", "false");
  }
}

// One floating popover serves every info button on the tab: managed
// integrations pass their catalog summary, tool rows pass
// html built from the tool manifest (tools.js). The key identifies the open
// popover so a second click on the same button closes it.
export function toggleInfoPopover(key, anchor, html) {
  const panel = $("preset-info-popover");
  if (!panel.hidden && panel.dataset.integration === key) {
    closeIntegrationInfo();
    return;
  }
  panel.dataset.integration = key;
  panel.innerHTML = html;
  panel.hidden = false;
  infoPopoverAnchor = anchor;
  positionIntegrationInfo();
  for (const button of document.querySelectorAll(".info-button")) {
    button.setAttribute("aria-expanded", String(button.dataset.info === key));
  }
}

export function toggleIntegrationInfo(name, anchor) {
  const info = name === CUSTOM_DOMAIN_GUIDE.id ? CUSTOM_DOMAIN_GUIDE : integrationInfo(name);
  if (!info) return;
  toggleInfoPopover(name, anchor, renderIntegrationInfo(name, info));
}

export function positionIntegrationInfo() {
  const panel = $("preset-info-popover");
  if (panel.hidden || !infoPopoverAnchor) return;
  const anchorRect = infoPopoverAnchor.getBoundingClientRect();
  const margin = 12;
  const panelWidth = Math.min(448, window.innerWidth - margin * 2);
  panel.style.width = `${panelWidth}px`;
  panel.style.left = "0px";
  panel.style.top = "0px";
  const panelRect = panel.getBoundingClientRect();
  const rightLeft = anchorRect.right + margin;
  const leftLeft = anchorRect.left - panelRect.width - margin;
  const preferredLeft = rightLeft + panelRect.width <= window.innerWidth - margin
    ? rightLeft
    : leftLeft;
  const left = Math.min(
    Math.max(margin, preferredLeft),
    Math.max(margin, window.innerWidth - panelRect.width - margin),
  );
  const preferredTop = anchorRect.top - 6;
  const top = Math.min(
    Math.max(margin, preferredTop),
    Math.max(margin, window.innerHeight - panelRect.height - margin),
  );
  panel.style.left = `${left}px`;
  panel.style.top = `${top}px`;
}

function renderIntegrationInfo(name, info) {
  return `
    <h3>${esc(info.label)}</h3>
    <h4>Protections</h4>
    <ul>${info.protections.map(protection => `<li>${inlineCode(protection)}</li>`).join("")}</ul>
    <button class="popover-guide-link" data-action="open-connection-guide" data-guide="${esc(name)}">View integration guide</button>`;
}

export async function loadPolicy() {
  const response = await api("GET", "/v1/network/policy");
  activeNetworkPolicy = normalizePolicy(response.network_controls);
  renderNetworkControls();
  loadGithubCredential().catch(() => {});
}

function normalizePolicy(policy) {
  const integrations = objectValue(policy && policy.network_integrations);
  return {"network_integrations": JSON.parse(JSON.stringify(integrations))};
}

function customDomains(policy) {
  return objectValue(objectValue(objectValue(policy.network_integrations).custom).domains);
}

function clonePolicy(policy) {
  return normalizePolicy(JSON.parse(JSON.stringify(policy || {})));
}

function renderNetworkControls() {
  renderManagedIntegrations();
  renderGithubRepos();
  renderDomainRules();
}

// Every edit control mutates a clone of the live policy and publishes it
// immediately: there is no proposal state, each integration and each domain
// rule is managed on its own, and the backend PUT validates and applies the
// whole policy atomically.
async function publishPolicy(integration, mutate, message) {
  const draft = clonePolicy(activeNetworkPolicy);
  mutate(draft);
  policyMessage(integration, "");
  try {
    const response = await api("PUT", "/v1/network/policy", draft);
    activeNetworkPolicy = normalizePolicy(response.network_controls);
    renderNetworkControls();
    policyMessage(integration, message);
    loadGithubCredential().catch(() => {});
    // Runtime states change with the policy; reflect that now, not at the next poll.
    refreshHealth().catch(() => {});
    refreshProviderAccounts().catch(() => {});
  } catch (error) { policyMessage(integration, error.message, true); }
}

function renderManagedIntegrations() {
  closeIntegrationInfo();
  const managed = objectValue(activeNetworkPolicy.network_integrations);
  // Park the expansion node outside the list before the innerHTML swap below
  // would destroy it (it was moved under the GitHub details on the previous
  // render).
  const expansion = $("github-expansion");
  const toolContainer = $("tools");
  if (expansion.closest(".integration-row")) toolContainer.after(expansion);
  const integrations = Object.entries(MANAGED_INTEGRATIONS)
    .sort(([, left], [, right]) => left.label.localeCompare(right.label, undefined, { sensitivity: "base" }));
  const renderRows = entries => entries.map(([name, meta]) => {
    const enabled = objectValue(managed[name]).enabled === true;
    const expanded = expandedIntegrations.has(name);
    return `
      <section class="integration-row" data-integration="${esc(name)}">
        <div class="integration-summary">
          <button class="ghost sm icon-button integration-chevron" data-action="toggle-integration-expansion" data-integration="${esc(name)}" aria-label="Toggle ${esc(meta.label)} details" aria-expanded="${expanded}">
            <svg width="16" height="16" viewBox="0 0 20 20" aria-hidden="true"><path d="m7.5 4.5 5 5.5-5 5.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <div class="integration-title">
            <div class="preset-with-info">
              <h2>${esc(meta.label)}</h2>
              <button class="info-button" data-action="toggle-integration-info" data-info="${esc(name)}" aria-label="${esc(meta.label)} overview and protections" aria-haspopup="dialog" aria-expanded="false">${informationIcon()}</button>
            </div>
            <div class="integration-subtitle">${esc(meta.summary)}</div>
          </div>
          <span class="status-chips">
            ${badge(enabled ? "enabled" : "disabled")}
            ${INFERENCE_INTEGRATIONS.includes(name) ? `<span data-provider-status="${esc(name)}"></span>` : ""}
          </span>
          <span class="integration-actions">
            <span class="seg">
              <button data-action="enable-integration" data-integration="${esc(name)}"${enabled ? " disabled" : ""}>Enable</button>
              <button data-action="disable-integration" data-integration="${esc(name)}"${enabled ? "" : " disabled"}>Disable</button>
            </span>
          </span>
        </div>
        <p class="inline-message integration-row-message" data-integration-message="${esc(name)}" role="status" aria-live="polite"></p>
        <div class="integration-details" data-integration-details="${esc(name)}"${expanded ? "" : " hidden"}>
          ${integrationDetailsHtml(name, enabled)}
        </div>
      </section>`;
  }).join("");
  const byName = new Map(integrations);
  const inference = INFERENCE_INTEGRATIONS.map(name => [name, byName.get(name)]);
  const managedTools = integrations.filter(([name]) => !INFERENCE_INTEGRATIONS.includes(name));
  setHtml($("ai-inference-integrations"), renderRows(inference));
  replaceIntegrationRows(toolContainer, "[data-integration]", renderRows(managedTools));
  renderIntegrationAccounts();
  // The write-repository list and audits render in the GitHub details
  // dropdown: the static #github-expansion node (its input keeps state across
  // re-renders) moves under the freshly rendered card.
  const githubDetails = document.querySelector('.integration-details[data-integration-details="github"]');
  if (githubDetails) githubDetails.append(expansion);
  expansion.hidden = !expandedIntegrations.has("github") || objectValue(managed.github).enabled !== true;
}

function integrationDetailsHtml(name, enabled) {
  if (name === BEDROCK_INTEGRATION) {
    const region = bedrockCredentialMetadata.region || "us-east-1";
    const accountCard = `
      <div class="detail-card">
        <div class="detail-card-head"><h3>AWS Bedrock connection</h3></div>
        <div class="integration-account" data-provider="${esc(name)}"></div>
        <div class="bedrock-credential-form">
          <input id="bedrock-access-key-id-${esc(name)}" type="text" placeholder="Access key id (AKIA...)" autocomplete="off" spellcheck="false">
          <input id="bedrock-secret-access-key-${esc(name)}" type="password" placeholder="Secret access key" autocomplete="off">
          <label class="bedrock-region-field" for="bedrock-region-${esc(name)}">
            <span>Region</span>
            <select id="bedrock-region-${esc(name)}">
              ${BEDROCK_REGIONS.map(value => `<option value="${esc(value)}"${value === region ? " selected" : ""}>${esc(value)}</option>`).join("")}
            </select>
          </label>
          <button class="primary sm" data-action="connect-bedrock-credentials" data-integration="${esc(name)}">Connect</button>
        </div>
      </div>`;
    return accountCard;
  }
  if (name === "openai" || name === "claude") {
    const accountCard = `
      <div class="detail-card">
        <div class="detail-card-head"><h3>Account</h3></div>
        <div class="integration-account" data-provider="${esc(name)}"></div>
        <div class="provider-oauth" data-provider-oauth="${esc(name)}"></div>
      </div>`;
    if (name === "claude" && enabled) {
      const webSearch = objectValue(objectValue(activeNetworkPolicy.network_integrations).claude).web_search === true;
      return `${accountCard}
      <div class="detail-card">
        <div class="detail-card-head">
          <h3>Web search</h3>
          <span class="seg">
            <button data-action="enable-claude-web-search"${webSearch ? " disabled" : ""}>Enable</button>
            <button data-action="disable-claude-web-search"${webSearch ? "" : " disabled"}>Disable</button>
          </span>
        </div>
        <p class="muted">Anthropic's server-side web search runs off-box: the query and surrounding context go to Anthropic and its search partners.</p>
        <span class="muted">${webSearch
          ? "Enabled — Claude Code can run server-side web searches."
          : "Disabled — the network proxy blocks web search."}</span>
      </div>`;
    }
    return accountCard;
  }
  if (name === "github") {
    return `
      <p class="muted">All GitHub reads are allowed for public repositories, and private repositories the token reaches. Pushes and mutating API writes are scoped to the write repositories configured below.</p>
      ${enabled ? "" : `<p class="muted">Enable GitHub to manage write repositories.</p>`}`;
  }
  return `<p class="muted">No additional settings for this integration.</p>`;
}

// Filled in place on every accounts poll so the enclosing integration row
// (its buttons and open info popover) is never re-rendered by the poll. Only
// nodes carrying data-provider belong to the OpenAI/Claude rows; the tool
// rows reuse the .integration-account layout for their own connection line
// and must not be overwritten here.
export function renderIntegrationAccounts() {
  for (const node of document.querySelectorAll("[data-provider-status]")) {
    const provider = node.dataset.providerStatus;
    const enabled = objectValue(objectValue(activeNetworkPolicy.network_integrations)[provider]).enabled === true;
    if (!enabled) {
      setHtml(node, "");
      continue;
    }
    const account = providerAccounts().find(entry => entry.provider === provider) || {};
    const runtime = providerRuntime(provider);
    const record = runtimeRecords().find(entry => entry.type === runtime) || { status: account.status || "loading" };
    const identity = provider === BEDROCK_INTEGRATION
      ? (account.arn || account.account_id)
      : (account.email || account.account_id);
    setHtml(node, record.status === "active" && identity
      ? `<span class="status active">connected: <span class="chip-label">${esc(identity)}</span></span>`
      : record.status === "awaiting_login"
        ? `<span class="status awaiting_login">${provider === BEDROCK_INTEGRATION ? "credentials required" : "login required"}</span>`
        : badge(record.status || "not-connected"));
  }
  for (const node of document.querySelectorAll(".integration-account[data-provider]")) {
    const provider = node.dataset.provider;
    const account = providerAccounts().find(entry => entry.provider === provider) || {};
    const runtime = providerRuntime(provider);
    const runtimeLabel = RUNTIME_PROVIDERS[runtime].label;
    const record = runtimeRecords().find(entry => entry.type === runtime) || { status: account.status || "loading" };
    const enabled = objectValue(objectValue(activeNetworkPolicy.network_integrations)[provider]).enabled === true;
    const identity = provider === BEDROCK_INTEGRATION
      ? (account.arn || account.account_id)
      : (account.email || account.account_id);
    const linked = provider === BEDROCK_INTEGRATION ? bedrockCredentialMetadata.connected : Boolean(identity);
    const summary = !enabled && !linked && provider !== BEDROCK_INTEGRATION
      ? ""
      : record.status === "active" && identity
        ? `Connected account: <span class="connection-identity">${esc(identity)}</span> &middot; only this account is allowed through the proxy.`
        : identity
          ? `Linked account: <span class="connection-identity">${esc(identity)}</span> &middot; ${provider === BEDROCK_INTEGRATION ? "enable Bedrock to activate Hermes." : "sign in again to reconnect it."}`
          : provider === BEDROCK_INTEGRATION && bedrockCredentialMetadata.connected
            ? `AWS credential stored: <span class="connection-identity">${esc(bedrockCredentialMetadata.access_key_id || "linked")}</span>. Enable Bedrock to activate Hermes.`
            : provider === BEDROCK_INTEGRATION
              ? "No AWS credential stored yet. Connect a dedicated IAM access key; ensure it has at least these permissions: bedrock:InvokeModel and bedrock:InvokeModelWithResponseStream (required IAM policy). The operator key never enters an agent process."
            : "No account linked yet. The first login links the account it signs in to, and only that account is then allowed through the proxy.";
    const guidance = record.status === "error"
      ? `<p class="provider-error">${esc(record.error_message || "The last runtime check failed.")}</p>`
      : !enabled
        ? provider === BEDROCK_INTEGRATION
          ? bedrockCredentialMetadata.connected
            ? `<p class="muted">The validated credential remains stored. Enable AWS Bedrock to make Hermes available.</p>`
            : ""
          : `<p class="muted">Enable ${esc(MANAGED_INTEGRATIONS[provider].label)} access before starting a login.</p>`
        : "";
    const canLogin = provider !== BEDROCK_INTEGRATION && enabled && (record.status === "awaiting_login" || record.status === "error");
    const billing = provider === BEDROCK_INTEGRATION ? bedrockBillingMetadata(account) : "";
    setHtml(node, `
      ${summary ? `<p class="connection-summary">${summary}</p>` : ""}
      ${billing}
      ${guidance}
      <span class="provider-account-actions">
        ${canLogin ? `<button class="sm" data-action="start-login" data-runtime="${esc(runtime)}">Start ${esc(runtimeLabel)} login</button>` : ""}
        ${(provider === BEDROCK_INTEGRATION ? bedrockCredentialMetadata.connected : identity) ? `<button class="ghost sm" data-action="reset-linked-account" data-provider="${esc(provider)}">${provider === BEDROCK_INTEGRATION ? "Disconnect AWS" : "Disconnect"}</button>` : ""}
      </span>`);
    const oauth = document.querySelector(`[data-provider-oauth="${provider}"]`);
    if (oauth && record.status === "active") setHtml(oauth, "");
  }
}

function bedrockBillingMetadata(account) {
  const box = bedrockUsageBox(account);
  return box ? `<div class="bedrock-usage-boxes">${box}</div>` : "";
}

function bedrockUsageBox(account) {
  const usage = bedrockUsage(account);
  if (!usage) return "";
  const tokenParts = [
    `${formatTokenCount(usage.inputTokens)} in`,
    `${formatTokenCount(usage.outputTokens)} out`,
  ];
  if (usage.cacheReadTokens || usage.cacheWriteTokens) {
    tokenParts.push(`${formatTokenCount(usage.cacheReadTokens + usage.cacheWriteTokens)} cached`);
  }
  const unmetered = usage.requests - usage.meteredRequests;
  const caveatHtml = unmetered > 0
    ? `<span class="bedrock-usage-caveat">${esc(`${unmetered} of ${usage.requests} requests unmetered`)}</span>`
    : "";
  return `
    <span class="bedrock-usage-box" role="group" aria-label="${esc(`Month-to-date estimate ${usage.cost}; ${tokenParts.join(", ")} tokens; ${usage.requests} requests`)}">
      <span class="bedrock-usage-cost">MTD est. <strong>${esc(usage.cost)}</strong></span>
      <span class="bedrock-usage-tokens">${esc(tokenParts.join(" · "))} · ${esc(String(usage.requests))} req</span>
      ${caveatHtml}
    </span>`;
}

export async function resetLinkedAccount(provider) {
  const label = MANAGED_INTEGRATIONS[provider] ? MANAGED_INTEGRATIONS[provider].label : provider;
  const sharedBedrock = provider === BEDROCK_INTEGRATION;
  const message = sharedBedrock
    ? "Disconnect the AWS Bedrock account? This fails running Hermes tasks and clears its credential. Hermes cannot reach Bedrock until credentials are connected again."
    : `Disconnect the linked ${label} account? This clears local ${label} auth and fails running tasks that use it. The agent cannot reach ${label} until a new login links an account.`;
  if (!confirm(message)) return;
  const runtime = providerRuntime(provider);
  try {
    if (sharedBedrock) {
      await api("DELETE", "/v1/agent-runtime/bedrock-credentials");
    } else {
      await api("POST", "/v1/agent-runtime/reset-linked-account", { "agent_runtime": runtime });
    }
    policyMessage(provider, sharedBedrock ? "AWS Bedrock account disconnected." : `${label} account disconnected.`);
    await refreshProviderAccounts();
    await refreshHealth();
  } catch (error) { policyMessage(provider, error.message, true); }
}

export function toggleIntegrationExpansion(name) {
  if (!MANAGED_INTEGRATIONS[name]) return;
  if (expandedIntegrations.has(name)) {
    expandedIntegrations.delete(name);
  } else {
    expandedIntegrations.add(name);
  }
  renderManagedIntegrations();
}

export function openProvider(name) {
  if (!MANAGED_INTEGRATIONS[name]) return;
  expandedIntegrations.add(name);
  renderManagedIntegrations();
}

export function toggleCustomDomainAccess() {
  customDomainExpanded = !customDomainExpanded;
  renderDomainRules();
}

export async function setIntegrationEnabled(name, enabled) {
  const dropsRepositories = !enabled && name === "github" && githubRepositories(activeNetworkPolicy).length > 0;
  const prompt = dropsRepositories
    ? `Disable the ${MANAGED_INTEGRATIONS[name].label} integration and remove its write repositories?`
    : `Disable the ${MANAGED_INTEGRATIONS[name].label} integration for the agent right now?`;
  if (!enabled && !confirm(prompt)) return;
  if (name === "github" && enabled) expandedIntegrations.add("github");
  await publishPolicy(name, policy => {
    const managed = policy.network_integrations;
    // A disabled integration carries no other state: disabling GitHub also
    // removes its write repositories (the stored credential stays).
    const value = enabled ? { ...objectValue(managed[name]), "enabled": true } : { "enabled": false };
    // GitHub workflow changes are an arbitrary-code boundary. Start every
    // newly enabled GitHub integration with the approval gate on; the
    // operator can still turn it off explicitly after reviewing the risk.
    if (enabled && name === "github") value.require_dot_github_approval = true;
    managed[name] = value;
  }, `${MANAGED_INTEGRATIONS[name].label} ${enabled ? "enabled" : "disabled"}.`);
}

export async function connectBedrockCredentials(name) {
  if (name !== BEDROCK_INTEGRATION) return;
  const accessKeyId = ($(`bedrock-access-key-id-${name}`)?.value || "").trim();
  const secretAccessKey = ($(`bedrock-secret-access-key-${name}`)?.value || "").trim();
  const region = ($(`bedrock-region-${name}`)?.value || "").trim();
  if (!accessKeyId || !secretAccessKey || !BEDROCK_REGIONS.includes(region)) {
    policyMessage(name, "Enter the access key id, secret access key, and region.", true);
    return;
  }
  try {
    await api("POST", "/v1/agent-runtime/bedrock-credentials", {
      "access_key_id": accessKeyId,
      "secret_access_key": secretAccessKey,
      "region": region,
    });
    const secretInput = $(`bedrock-secret-access-key-${name}`);
    if (secretInput) secretInput.value = "";
    policyMessage(name, "AWS credential accepted.", false);
    await refreshProviderAccounts();
    await refreshHealth();
  } catch (error) { policyMessage(name, error.message, true); }
}

export async function setClaudeWebSearch(webSearch) {
  await publishPolicy("claude", policy => {
    const managed = policy.network_integrations;
    const value = { ...objectValue(managed.claude), "enabled": true };
    if (webSearch) value.web_search = true; else delete value.web_search;
    managed.claude = value;
  }, `Claude web search ${webSearch ? "enabled" : "disabled"}.`);
}

function githubRepositories(policy) {
  const github = objectValue(objectValue(policy.network_integrations).github);
  return Array.isArray(github.write_repositories) ? github.write_repositories : [];
}

function githubRequireApproval(policy) {
  return objectValue(objectValue(policy.network_integrations).github).require_dot_github_approval === true;
}

function githubIntegrationObject(enabled, writeRepositories, requireApproval) {
  const value = { "enabled": enabled === true, "write_repositories": writeRepositories };
  if (requireApproval) value.require_dot_github_approval = true;
  return value;
}

function renderGithubRepos() {
  const managed = objectValue(activeNetworkPolicy.network_integrations);
  const enabled = objectValue(managed.github).enabled === true;
  $("github-repo").disabled = !enabled;
  const addButton = document.querySelector('[data-action="add-github-repo"]');
  if (addButton) addButton.disabled = !enabled;
  renderGithubApproval();
  const repositories = githubRepositories(activeNetworkPolicy);
  if (!repositories.length) {
    setHtml($("github-repos"), `<p class="muted">No write repositories configured. The agent can read any public repository, and any private repository the token reaches; add a repository here to also allow push and API writes to it.</p>`);
    return;
  }
  const audits = new Map(latestGithubAudits.map(audit => [`${audit.owner}/${audit.repo}`, audit]));
  setHtml($("github-repos"), repositories.map(repo => {
    const key = `${repo.owner}/${repo.repo}`;
    const name = `${esc(repo.owner)}/${esc(repo.repo)}`;
    const summary = repoAuditSummary(audits.get(key));
    const expanded = expandedGithubRepoAudits.has(key);
    return `
      <div class="repo-entry">
        <div class="repo-head">
          <button class="ghost sm icon-button repo-audit-toggle integration-chevron" data-action="toggle-github-repo-audit" data-repo-key="${esc(key)}" aria-label="Toggle repository audit details for ${name}" aria-expanded="${expanded}">
            <svg width="16" height="16" viewBox="0 0 20 20" aria-hidden="true"><path d="m7.5 4.5 5 5.5-5 5.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <span class="mono">${name}</span>
          ${statusBadge(summary.kind, summary.label)}
          <button class="ghost sm repo-remove" data-action="remove-github-repo" data-owner="${esc(repo.owner)}" data-repo="${esc(repo.repo)}">Remove</button>
        </div>
        ${expanded ? `<div class="repo-audit-details">${repoAuditDetailsHtml(audits.get(key))}</div>` : ""}
      </div>`;
  }).join(""));
}

function statusBadge(kind, label) {
  return `<span class="status ${esc(kind)}">${esc(label)}</span>`;
}

function repoAuditSummary(audit) {
  if (!audit) return { kind: "warning", label: "1 warning" };
  const warnings = Array.isArray(audit.warnings) ? audit.warnings : [];
  if (warnings.length) {
    return {
      kind: warnings.some(warning => warning.severity === "critical") ? "critical" : "warning",
      label: `${warnings.length} warning${warnings.length === 1 ? "" : "s"}`,
    };
  }
  if (audit.error || !audit.audited_at) return { kind: "warning", label: "1 warning" };
  return { kind: "ok", label: "no warnings" };
}

function repoAuditDetailsHtml(audit) {
  if (!audit) return `<div class="audit-banner warning">Repository audit status is unavailable; TrustyClaw has not verified this write target yet.</div>`;
  const warnings = Array.isArray(audit.warnings) ? audit.warnings : [];
  if (warnings.length) return warnings.map(warning => `
    <div class="audit-banner ${warning.severity === "critical" ? "critical" : "warning"}">${esc(warning.message)}</div>`).join("");
  if (audit.error) return `<div class="audit-banner warning">audit failed — ${esc(audit.error)}</div>`;
  if (!audit.audited_at) return `<div class="audit-banner warning">Repository audit has not run yet; TrustyClaw has not verified this write target.</div>`;
  return `<div class="audit-banner ok">no warnings</div>`;
}

export function toggleGithubRepoAudit(key) {
  if (expandedGithubRepoAudits.has(key)) {
    expandedGithubRepoAudits.delete(key);
  } else {
    expandedGithubRepoAudits.add(key);
  }
  renderGithubRepos();
}

export async function addGithubRepo() {
  const managed = objectValue(activeNetworkPolicy.network_integrations);
  if (objectValue(managed.github).enabled !== true) {
    policyMessage("github", "Enable the GitHub integration before adding write repositories.", true);
    return;
  }
  const input = $("github-repo").value.trim().toLowerCase().replace(/\.git$/, "");
  const match = GITHUB_REPO_INPUT_RE.exec(input);
  if (!match) { policyMessage("github", "Enter a GitHub repository as owner/repo.", true); return; }
  const entry = {"owner": match[1], "repo": match[2]};
  await publishPolicy("github", policy => {
    const managed = policy.network_integrations;
    const github = objectValue(managed.github);
    const remaining = githubRepositories(policy).filter(repo =>
      !(objectValue(repo).owner === entry.owner && objectValue(repo).repo === entry.repo));
    remaining.push(entry);
    managed.github = githubIntegrationObject(github.enabled === true, remaining, githubRequireApproval(policy));
  }, `Write repository ${entry.owner}/${entry.repo} saved.`);
  $("github-repo").value = "";
}

export async function removeGithubRepo(owner, repo) {
  if (!confirm(`Remove ${owner}/${repo} from the GitHub write repositories?`)) return;
  await publishPolicy("github", policy => {
    const managed = policy.network_integrations;
    const github = objectValue(managed.github);
    const remaining = githubRepositories(policy).filter(entry =>
      !(objectValue(entry).owner === owner && objectValue(entry).repo === repo));
    managed.github = githubIntegrationObject(github.enabled === true, remaining, githubRequireApproval(policy));
  }, `Write repository ${owner}/${repo} removed.`);
}

function renderGithubApproval() {
  const managed = objectValue(activeNetworkPolicy.network_integrations);
  const enabled = objectValue(managed.github).enabled === true;
  const required = githubRequireApproval(activeNetworkPolicy);
  const status = $("github-require-approval-status");
  if (status) {
    status.textContent = !enabled
      ? "Enable the GitHub integration first."
      : required
        ? "Enabled — .github pushes are held for approval."
        : "Disabled — .github pushes reach GitHub directly.";
  }
  const enableButton = document.querySelector('[data-action="enable-github-require-approval"]');
  const disableButton = document.querySelector('[data-action="disable-github-require-approval"]');
  if (enableButton) {
    enableButton.disabled = !enabled || required;
    enableButton.textContent = enabled && required ? "Enabled" : "Enable";
  }
  if (disableButton) {
    disableButton.disabled = !enabled || !required;
    disableButton.textContent = enabled && !required ? "Disabled" : "Disable";
  }
  renderPendingPushes();
}

export async function setGithubRequireApproval(requireApproval) {
  const managed = objectValue(activeNetworkPolicy.network_integrations);
  if (objectValue(managed.github).enabled !== true) {
    policyMessage("github", "Enable the GitHub integration before changing .github push approval.", true);
    return;
  }
  await publishPolicy("github", policy => {
    const managed = policy.network_integrations;
    const github = objectValue(managed.github);
    managed.github = githubIntegrationObject(github.enabled === true, githubRepositories(policy), requireApproval);
  }, `.github push approval ${requireApproval ? "enabled" : "disabled"}.`);
}

async function renderPendingPushes() {
  let pushes = [];
  try {
    pushes = (await api("GET", "/v1/network-tools/github-pending-pushes")).pending_pushes || [];
  } catch (_error) {
    setHtml($("github-pending-pushes"), "");
    return;
  }
  const pending = pushes.filter(push => push.status === "pending");
  if (!pending.length) {
    setHtml($("github-pending-pushes"), "");
    return;
  }
  setHtml($("github-pending-pushes"), `
    <div class="field-label"><code>.github</code> pushes awaiting approval</div>
    ${pending.map(push => `
      <div class="pending-push">
        <div class="pending-push-head">
          <span class="mono">${esc(push.owner)}/${esc(push.repo)}</span>
          <span class="muted mono">push-${esc(push.id)}</span>
          ${badge(push.status)}
          <span class="muted mono">${(push.ref_updates || []).map(update => esc(update.ref)).join(", ")}</span>
        </div>
        <ul class="pending-push-paths">${(push.changed_paths || []).map(path => `<li class="mono">${esc(path)}</li>`).join("")}</ul>
        <div class="actions">
          <button class="sm" data-action="approve-github-push" data-id="${esc(push.id)}">Approve &amp; push</button>
          <button class="danger ghost sm" data-action="reject-github-push" data-id="${esc(push.id)}">Reject</button>
        </div>
      </div>`).join("")}`);
}

export async function refreshPendingGithubPushes() {
  await renderPendingPushes();
}

export async function approveGithubPush(id) {
  if (!confirm(`Approve push-${id} and push its .github changes to GitHub?`)) return;
  try {
    await api("POST", `/v1/network-tools/github-pending-pushes/${id}/approve`, {});
    policyMessage("github", `push-${id} approved and pushed.`);
  } catch (error) {
    policyMessage("github", `Approve failed: ${error.message}`, true);
  }
  renderPendingPushes();
}

export async function rejectGithubPush(id) {
  if (!confirm(`Reject push-${id}? Its objects are discarded.`)) return;
  try {
    await api("POST", `/v1/network-tools/github-pending-pushes/${id}/reject`, {});
    policyMessage("github", `push-${id} rejected.`);
  } catch (error) {
    policyMessage("github", `Reject failed: ${error.message}`, true);
  }
  renderPendingPushes();
}

export function toggleGithubCredentialMode() {
  const app = $("github-credential-mode").value === "app";
  $("github-token").hidden = app;
  $("github-app-fields").hidden = !app;
}

async function loadGithubCredential() {
  const status = $("github-credential-status");
  try {
    const metadata = await api("GET", "/v1/network-tools/github-credential");
    renderGithubAudit(metadata.repository_audits);
    $("github-credential-clear").hidden = metadata.configured !== true;
    $("github-credential-form-label").textContent = metadata.configured ? "Replace credential" : "Set a new credential";
    const validation = metadata.validation && metadata.validation.status ? ` (validation: ${metadata.validation.status})` : "";
    if (!metadata.configured) {
      status.textContent = "No credential configured. Public repository reads work without one.";
    } else if (metadata.mode === "app") {
      const expires = metadata.app_token_expires_at ? `; token expires ${metadata.app_token_expires_at}` : "";
      status.textContent = `Configured: GitHub App ${metadata.app_id || ""}, installation ${metadata.installation_id || ""}${expires}${validation}.`;
    } else {
      const updated = metadata.updated_at ? ` (updated ${metadata.updated_at})` : "";
      status.textContent = `Configured: fine-grained token (PAT)${updated}${validation}.`;
    }
  } catch (error) {
    status.textContent = error.message;
  }
}

function renderGithubAudit(audits) {
  latestGithubAudits = Array.isArray(audits) ? audits : [];
  renderGithubRepos();
}

export async function recheckGithubAudit() {
  policyMessage("github", "Re-checking repository audits…");
  try {
    const metadata = await api("POST", "/v1/network-tools/github-audit", {});
    renderGithubAudit(metadata.repository_audits);
    policyMessage("github", "Repository audits refreshed.");
  } catch (error) { policyMessage("github", error.message, true); }
}

export async function setGithubCredential() {
  const mode = $("github-credential-mode").value;
  let body;
  if (mode === "app") {
    const appId = $("github-app-id").value.trim();
    const installationId = $("github-app-installation-id").value.trim();
    const privateKey = $("github-app-private-key").value.trim();
    if (!appId || !installationId || !privateKey) {
      policyMessage("github", "App id, installation id, and private key are all required for app mode.", true);
      return;
    }
    body = {"mode": "app", "app_id": appId, "installation_id": installationId, "private_key_pem": privateKey};
  } else {
    const token = $("github-token").value.trim();
    if (!token) { policyMessage("github", "Enter a GitHub token first.", true); return; }
    body = {"mode": "pat", "token": token};
  }
  try {
    await api("PUT", "/v1/network-tools/github-credential", body);
    $("github-token").value = "";
    $("github-app-private-key").value = "";
    policyMessage("github", "GitHub credential stored.");
  } catch (error) { policyMessage("github", error.message, true); }
  await loadGithubCredential();
}

export async function deleteGithubCredential() {
  try {
    await api("DELETE", "/v1/network-tools/github-credential");
    policyMessage("github", "GitHub credential cleared.");
  } catch (error) { policyMessage("github", error.message, true); }
  await loadGithubCredential();
}

function renderDomainRules() {
  const rules = customDomains(activeNetworkPolicy);
  const domains = Object.keys(rules).sort();
  const count = domains.length;
  $("domain-rule-count").textContent = `${count} domain${count === 1 ? "" : "s"} enabled`;
  $("domain-rule-count").className = `status ${count > 0 ? "enabled" : "disabled"}`;
  $("custom-domain-details").hidden = !customDomainExpanded;
  $("custom-domain-toggle").setAttribute("aria-expanded", String(customDomainExpanded));
  if (!domains.length) {
    setHtml($("domain-rules"), `<p class="muted">No custom domains configured.</p>`);
    return;
  }
  setHtml($("domain-rules"), `<table>
    <tr><th>domain</th><th>methods</th><th>path guards</th><th></th></tr>
    ${domains.map(domain => {
      const rule = objectValue(rules[domain]);
      const methods = (rule.allow_http_methods || []).join(", ");
      const guards = (rule.path_guards || []).join("\n");
      return `
      <tr>
        <td class="mono">${esc(domain)}</td>
        <td>${esc(methods)}</td>
        <td class="mono">${guards ? `<pre>${esc(guards)}</pre>` : `<span class="muted">any path</span>`}</td>
        <td><button class="ghost sm" data-action="remove-domain-rule" data-domain="${esc(domain)}">Remove</button></td>
      </tr>`;
    }).join("")}
  </table>`);
}

export async function addDomainRule() {
  const domain = $("policy-domain").value.trim().toLowerCase();
  const methods = $("policy-methods").value.split(",").map(value => value.trim().toUpperCase()).filter(Boolean);
  const pathGuards = $("policy-path-guards").value.split("\n").map(value => value.trim()).filter(Boolean);
  if (!domain || !methods.length) { policyMessage("custom_domain", "Domain and at least one HTTP method are required.", true); return; }
  const rule = {"allow_http_methods": methods};
  if (pathGuards.length) rule.path_guards = pathGuards;
  await publishPolicy("custom_domain", policy => {
    const domains = customDomains(policy);
    domains[domain] = rule;
    policy.network_integrations.custom = {"domains": domains};
  }, `Domain rule for ${domain} saved.`);
  $("policy-domain").value = "";
  $("policy-methods").value = "";
  $("policy-path-guards").value = "";
}

export async function removeDomainRule(domain) {
  if (!confirm(`Remove the domain rule for ${domain}?`)) return;
  await publishPolicy("custom_domain", policy => {
    const domains = customDomains(policy);
    delete domains[domain];
    if (Object.keys(domains).length) {
      policy.network_integrations.custom = {"domains": domains};
    } else {
      delete policy.network_integrations.custom;
    }
  }, `Domain rule for ${domain} removed.`);
}
