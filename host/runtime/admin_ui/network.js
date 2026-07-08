// Internet Access and Tools tab: the active network policy (managed
// integrations and manual domain rules), the GitHub write-repository list
// with per-repository audits, and the GitHub credential controls.

import { api } from "./api.js";
import { $, badge, esc, objectValue, setHtml } from "./helpers.js";
import { providerAccounts, refreshHealth, refreshProviderAccounts, renderRuntimeGuidance } from "./health.js";

const GITHUB_REPO_INPUT_RE = /^([a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?)\/([a-z0-9._-]{1,100})$/;
const MANAGED_INTEGRATIONS = {
  openai: { label: "OpenAI", info: "openai" },
  claude: { label: "Claude", info: "claude" },
  github: { label: "GitHub", info: "github" },
  python_packages: { label: "Python packages", info: "python" },
  npm_packages: { label: "npm packages", info: "npm" },
};
const INTEGRATION_INFO = {
  openai: {
    heading: "OpenAI",
    summary: "Lets the Codex runtime reach OpenAI. Requests must authenticate as the pinned operator account, and live web search stays disabled.",
    rows: [
      ["api.openai.com", "POST; account guard; live web search disabled"],
      ["auth.openai.com", "GET, POST"],
      ["chatgpt.com", "GET, POST; account guard; live web search disabled"],
    ],
  },
  claude: {
    heading: "Claude",
    summary: "Lets the Claude Code runtime reach Anthropic. Requests must authenticate as the pinned operator account.",
    rows: [
      ["api.anthropic.com", "GET, POST; account guard"],
      ["platform.claude.com", "GET, POST; only /v1/oauth paths"],
    ],
  },
  github: {
    heading: "GitHub",
    summary: "When enabled, every read is allowed: the agent can read any public repository, and any private repository the token reaches. Writes are the controlled side: a push or mutating API call must target a repository listed below, and repository administration stays denied even there. The credential is installed for git and gh only while GitHub is enabled.",
    rows: [
      ["github.com", "GET, HEAD, and git fetch: any repo. git push and LFS upload: only configured write repos (LFS uploads denied)"],
      ["api.github.com", "any GET/HEAD read; writes only under /repos/<owner>/<repo> of a write repo, and never repo administration (settings, access grants, keys, hooks, forks, transfers, workflows, protections, security toggles, automation signals); GraphQL denied"],
      ["uploads.github.com", "release-asset uploads under /repos/<owner>/<repo>; needs a write repo"],
      ["codeload.github.com", "GET, HEAD; any repo archive"],
      ["raw.githubusercontent.com", "GET, HEAD; any repo path"],
      ["objects.githubusercontent.com", "GET, HEAD; signed download URLs only"],
      ["github-cloud.githubusercontent.com", "GET, HEAD; signed download URLs only"],
      ["release-assets.githubusercontent.com", "GET, HEAD; signed download URLs only"],
    ],
  },
  python: {
    heading: "Python packages",
    summary: "Read-only access to the public PyPI index and package downloads.",
    rows: [
      ["pypi.org", "GET, HEAD; only /simple and /pypi/<package>/json paths"],
      ["files.pythonhosted.org", "GET, HEAD; only /packages paths"],
    ],
  },
  npm: {
    heading: "npm packages",
    summary: "Read-only access to the public npm registry and Node.js downloads.",
    rows: [
      ["nodejs.org", "GET, HEAD; only /dist paths"],
      ["registry.npmjs.org", "GET, HEAD"],
    ],
  },
};

let activeNetworkPolicy = {"managed_network_integrations": {}, "allowed_network_access": {}};
let expandedIntegrations = new Set();
let expandedGithubRepoAudits = new Set();
let customDomainExpanded = false;
let latestGithubAudits = [];
let infoPopoverAnchor = null;

export function activePolicy() {
  return activeNetworkPolicy;
}

function policyMessage(message) {
  $("policy-message").textContent = message || "";
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

export function toggleIntegrationInfo(name, anchor) {
  const panel = $("preset-info-popover");
  if (!INTEGRATION_INFO[name]) return;
  if (!panel.hidden && panel.dataset.integration === name) {
    closeIntegrationInfo();
    return;
  }
  panel.dataset.integration = name;
  panel.innerHTML = renderIntegrationInfo(INTEGRATION_INFO[name]);
  panel.hidden = false;
  infoPopoverAnchor = anchor;
  positionIntegrationInfo();
  for (const button of document.querySelectorAll(".info-button")) {
    button.setAttribute("aria-expanded", String(button.dataset.info === name));
  }
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

function renderIntegrationInfo(info) {
  return `
    <h3>${esc(info.heading)}</h3>
    <p class="muted">This integration enables direct internet access to these domains and paths. ${esc(info.summary)}</p>
    <table>
      ${info.rows.map(([domain, scope]) => `
        <tr>
          <td><strong>${esc(domain)}</strong></td>
          <td>${esc(scope)}</td>
        </tr>`).join("")}
    </table>`;
}

export async function loadPolicy() {
  const response = await api("GET", "/v1/network/policy");
  activeNetworkPolicy = normalizePolicy(response.network_controls);
  renderNetworkControls();
  loadGithubCredential().catch(() => {});
}

function normalizePolicy(policy) {
  const managed = objectValue(policy && policy.managed_network_integrations);
  const rules = objectValue(policy && policy.allowed_network_access);
  return {
    "managed_network_integrations": JSON.parse(JSON.stringify(managed)),
    "allowed_network_access": rules,
  };
}

function clonePolicy(policy) {
  return normalizePolicy(JSON.parse(JSON.stringify(policy || {})));
}

function renderNetworkControls() {
  renderManagedIntegrations();
  renderGithubRepos();
  renderDomainRules();
  renderRuntimeGuidance();
}

// Every edit control mutates a clone of the live policy and publishes it
// immediately: there is no proposal state, each integration and each domain
// rule is managed on its own, and the backend PUT validates and applies the
// whole policy atomically.
async function publishPolicy(mutate, message) {
  const draft = clonePolicy(activeNetworkPolicy);
  mutate(draft);
  try {
    const response = await api("PUT", "/v1/network/policy", draft);
    activeNetworkPolicy = normalizePolicy(response.network_controls);
    renderNetworkControls();
    policyMessage(message);
    loadGithubCredential().catch(() => {});
    // Runtime states change with the policy; reflect that now, not at the next poll.
    refreshHealth().catch(() => {});
    refreshProviderAccounts().catch(() => {});
  } catch (error) { policyMessage(error.message); }
}

function renderManagedIntegrations() {
  closeIntegrationInfo();
  const managed = objectValue(activeNetworkPolicy.managed_network_integrations);
  // Park the expansion node outside the list before the innerHTML swap below
  // would destroy it (it was moved under the GitHub details on the previous
  // render).
  const expansion = $("github-expansion");
  const container = $("managed-integrations");
  if (container.contains(expansion)) container.after(expansion);
  setHtml($("managed-integrations"), Object.entries(MANAGED_INTEGRATIONS).map(([name, meta]) => {
    const enabled = objectValue(managed[name]).enabled === true;
    const expanded = expandedIntegrations.has(name);
    return `
      <section class="integration-row" data-integration="${esc(name)}">
        <div class="integration-summary">
          <button class="ghost sm icon-button integration-chevron" data-action="toggle-integration-expansion" data-integration="${esc(name)}" aria-label="Toggle ${esc(meta.label)} details" aria-expanded="${expanded}">
            <svg width="16" height="16" viewBox="0 0 20 20" aria-hidden="true"><path d="m7.5 4.5 5 5.5-5 5.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <div class="preset-with-info integration-title">
            <h2>${esc(meta.label)}</h2>
            <button class="info-button" data-action="toggle-integration-info" data-info="${esc(meta.info)}" aria-label="${esc(meta.label)} internet access details" aria-haspopup="dialog" aria-expanded="false">i</button>
          </div>
          ${badge(enabled ? "enabled" : "disabled")}
          <span class="integration-actions">
            <button class="sm" data-action="enable-integration" data-integration="${esc(name)}"${enabled ? " disabled" : ""}>Enable</button>
            <button class="ghost sm" data-action="disable-integration" data-integration="${esc(name)}"${enabled ? "" : " disabled"}>Disable</button>
          </span>
        </div>
        <div class="integration-details" data-integration-details="${esc(name)}"${expanded ? "" : " hidden"}>
          ${integrationDetailsHtml(name, enabled)}
        </div>
      </section>`;
  }).join(""));
  renderIntegrationAccounts();
  // The write-repository list and audits render in the GitHub details
  // dropdown: the static #github-expansion node (its input keeps state across
  // re-renders) moves under the freshly rendered card.
  const githubDetails = document.querySelector('.integration-details[data-integration-details="github"]');
  if (githubDetails) githubDetails.append(expansion);
  expansion.hidden = !expandedIntegrations.has("github") || objectValue(managed.github).enabled !== true;
}

function integrationDetailsHtml(name, enabled) {
  if (name === "openai" || name === "claude") {
    return `<div class="integration-account" data-provider="${esc(name)}"></div>`;
  }
  if (name === "github") {
    return `
      <p class="muted">All GitHub reads are allowed for public repositories, and private repositories the token reaches. Pushes and mutating API writes are scoped to the write repositories configured below.</p>
      ${enabled ? "" : `<p class="muted">Enable GitHub to manage write repositories.</p>`}`;
  }
  return `<p class="muted">No additional settings for this integration.</p>`;
}

// Filled in place on every accounts poll so the enclosing integration row
// (its buttons and open info popover) is never re-rendered by the poll.
export function renderIntegrationAccounts() {
  for (const node of document.querySelectorAll(".integration-account")) {
    const provider = node.dataset.provider;
    const account = providerAccounts().find(entry => entry.provider === provider) || {};
    const identity = account.email || account.account_id;
    const summary = identity
      ? `Linked account: <b>${esc(identity)}</b> <span class="muted">&middot; only this account is allowed through the proxy.</span>`
      : `<span class="muted">No account linked yet. The first login links the account it signs in to, and only that account is then allowed through the proxy.</span>`;
    setHtml(node, `
      <p>${summary}</p>
      <button class="ghost sm" data-action="reset-linked-account" data-provider="${esc(provider)}">Reset linked account</button>`);
  }
}

export async function resetLinkedAccount(provider) {
  const label = MANAGED_INTEGRATIONS[provider] ? MANAGED_INTEGRATIONS[provider].label : provider;
  const message = `Reset the linked ${label} account? `
    + `This clears local ${label} auth and fails running tasks that use it. `
    + `The agent cannot reach ${label} until a new login links an account.`;
  if (!confirm(message)) return;
  const runtime = provider === "claude" ? "claude_code" : "codex";
  try {
    await api("POST", "/v1/agent-runtime/reset-linked-account", { "agent_runtime": runtime });
    policyMessage(`${label} linked account reset.`);
    await refreshProviderAccounts();
    await refreshHealth();
  } catch (error) { policyMessage(error.message); }
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
  await publishPolicy(policy => {
    const managed = policy.managed_network_integrations;
    // A disabled integration carries no other state: disabling GitHub also
    // removes its write repositories (the stored credential stays).
    managed[name] = enabled ? { ...objectValue(managed[name]), "enabled": true } : { "enabled": false };
  }, `${MANAGED_INTEGRATIONS[name].label} ${enabled ? "enabled" : "disabled"}.`);
}

function githubRepositories(policy) {
  const github = objectValue(objectValue(policy.managed_network_integrations).github);
  return Array.isArray(github.write_repositories) ? github.write_repositories : [];
}

function githubRequireApproval(policy) {
  return objectValue(objectValue(policy.managed_network_integrations).github).require_dot_github_approval === true;
}

function githubIntegrationObject(enabled, writeRepositories, requireApproval) {
  const value = { "enabled": enabled === true, "write_repositories": writeRepositories };
  if (requireApproval) value.require_dot_github_approval = true;
  return value;
}

function renderGithubRepos() {
  const managed = objectValue(activeNetworkPolicy.managed_network_integrations);
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
  const managed = objectValue(activeNetworkPolicy.managed_network_integrations);
  if (objectValue(managed.github).enabled !== true) {
    policyMessage("Enable the GitHub integration before adding write repositories.");
    return;
  }
  const input = $("github-repo").value.trim().toLowerCase().replace(/\.git$/, "");
  const match = GITHUB_REPO_INPUT_RE.exec(input);
  if (!match) { policyMessage("Enter a GitHub repository as owner/repo."); return; }
  const entry = {"owner": match[1], "repo": match[2]};
  await publishPolicy(policy => {
    const managed = policy.managed_network_integrations;
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
  await publishPolicy(policy => {
    const managed = policy.managed_network_integrations;
    const github = objectValue(managed.github);
    const remaining = githubRepositories(policy).filter(entry =>
      !(objectValue(entry).owner === owner && objectValue(entry).repo === repo));
    managed.github = githubIntegrationObject(github.enabled === true, remaining, githubRequireApproval(policy));
  }, `Write repository ${owner}/${repo} removed.`);
}

function renderGithubApproval() {
  const managed = objectValue(activeNetworkPolicy.managed_network_integrations);
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
  if (enableButton) enableButton.disabled = !enabled || required;
  if (disableButton) disableButton.disabled = !enabled || !required;
  renderPendingPushes();
}

export async function setGithubRequireApproval(requireApproval) {
  const managed = objectValue(activeNetworkPolicy.managed_network_integrations);
  if (objectValue(managed.github).enabled !== true) {
    policyMessage("Enable the GitHub integration before changing .github push approval.");
    return;
  }
  await publishPolicy(policy => {
    const managed = policy.managed_network_integrations;
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
  const pending = pushes.filter(push => push.status === "pending" || push.status === "resolving");
  if (!pending.length) {
    setHtml($("github-pending-pushes"), "");
    return;
  }
  setHtml($("github-pending-pushes"), `
    <div class="field-label"><code>.github</code> pushes awaiting approval</div>
    ${pending.map(push => `
      <div class="manual-domain">
        <div class="mono">${esc(push.owner)}/${esc(push.repo)} — push-${esc(push.id)} (${esc(push.status)})</div>
        <div class="muted">${(push.ref_updates || []).map(update => esc(update.ref)).join(", ")}</div>
        <ul>${(push.changed_paths || []).map(path => `<li class="mono">${esc(path)}</li>`).join("")}</ul>
        <div class="actions">
          <button class="sm" data-action="approve-github-push" data-id="${esc(push.id)}">Approve &amp; push</button>
          <button class="danger sm" data-action="reject-github-push" data-id="${esc(push.id)}">Reject</button>
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
    policyMessage(`push-${id} approved and pushed.`);
  } catch (error) {
    policyMessage(`Approve failed: ${error.message}`);
  }
  renderPendingPushes();
}

export async function rejectGithubPush(id) {
  if (!confirm(`Reject push-${id}? Its objects are discarded.`)) return;
  try {
    await api("POST", `/v1/network-tools/github-pending-pushes/${id}/reject`, {});
    policyMessage(`push-${id} rejected.`);
  } catch (error) {
    policyMessage(`Reject failed: ${error.message}`);
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
  policyMessage("Re-checking repository audits…");
  try {
    const metadata = await api("POST", "/v1/network-tools/github-audit", {});
    renderGithubAudit(metadata.repository_audits);
    policyMessage("Repository audits refreshed.");
  } catch (error) { policyMessage(error.message); }
}

export async function setGithubCredential() {
  const mode = $("github-credential-mode").value;
  let body;
  if (mode === "app") {
    const appId = $("github-app-id").value.trim();
    const installationId = $("github-app-installation-id").value.trim();
    const privateKey = $("github-app-private-key").value.trim();
    if (!appId || !installationId || !privateKey) {
      policyMessage("App id, installation id, and private key are all required for app mode.");
      return;
    }
    body = {"mode": "app", "app_id": appId, "installation_id": installationId, "private_key_pem": privateKey};
  } else {
    const token = $("github-token").value.trim();
    if (!token) { policyMessage("Enter a GitHub token first."); return; }
    body = {"mode": "pat", "token": token};
  }
  try {
    await api("PUT", "/v1/network-tools/github-credential", body);
    $("github-token").value = "";
    $("github-app-private-key").value = "";
    policyMessage("GitHub credential stored.");
  } catch (error) { policyMessage(error.message); }
  await loadGithubCredential();
}

export async function deleteGithubCredential() {
  try {
    await api("DELETE", "/v1/network-tools/github-credential");
    policyMessage("GitHub credential cleared.");
  } catch (error) { policyMessage(error.message); }
  await loadGithubCredential();
}

function renderDomainRules() {
  const rules = objectValue(activeNetworkPolicy.allowed_network_access);
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
  if (!domain || !methods.length) { policyMessage("Domain and at least one HTTP method are required."); return; }
  const rule = {"allow_http_methods": methods};
  if (pathGuards.length) rule.path_guards = pathGuards;
  await publishPolicy(policy => {
    policy.allowed_network_access[domain] = rule;
  }, `Domain rule for ${domain} saved.`);
  $("policy-domain").value = "";
  $("policy-methods").value = "";
  $("policy-path-guards").value = "";
}

export async function removeDomainRule(domain) {
  if (!confirm(`Remove the domain rule for ${domain}?`)) return;
  await publishPolicy(policy => {
    delete policy.allowed_network_access[domain];
  }, `Domain rule for ${domain} removed.`);
}
