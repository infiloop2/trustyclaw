// Entry module: session lifecycle (login, logout), tab switching, the
// 5-second refresh tick, and the one delegated click dispatcher that maps
// data-action buttons to feature handlers. Feature code lives in the sibling
// modules; this file is the only place that wires them together.

import { getPassword, setUnauthorizedHandler } from "./api.js";
import { $ } from "./helpers.js";
import {
  completeClaudeLogin, refreshHealth, refreshProviderAccounts,
  refreshProviderUsage, rebootHost, startLogin,
} from "./health.js";
import {
  loadMoreTaskEvents, loadThreads, refreshSelectedThread,
  renderThreadHistory, showTaskEvents, showThread,
} from "./threads.js";
import {
  ensureFilesLoaded, goToFilePath, loadParentDirectory, openAgentPath,
  refreshFiles,
} from "./files.js";
import { refreshAgentProcesses } from "./processes.js";
import { agentLog, netLog, toggleNetDeniedFilter } from "./logs.js";
import {
  addDomainRule, addGithubRepo, approveGithubPush, closeIntegrationInfo, deleteGithubCredential,
  loadPolicy, recheckGithubAudit, rejectGithubPush, removeDomainRule,
  removeGithubRepo, resetLinkedAccount, setGithubCredential, setGithubRequireApproval,
  setIntegrationEnabled, positionIntegrationInfo, refreshPendingGithubPushes, toggleGithubCredentialMode, toggleIntegrationExpansion,
  toggleCustomDomainAccess, toggleGithubRepoAudit, toggleIntegrationInfo,
} from "./network.js";

let activeTab = "home";

function adminCookieAttributes(maxAge) {
  return `; path=/; max-age=${maxAge}; samesite=strict${location.protocol === "https:" ? "; secure" : ""}`;
}

function login() {
  const value = $("password").value.trim();
  if (!value) return;
  document.cookie = "trustyclaw_admin=" + encodeURIComponent(value) + adminCookieAttributes(2592000);
  $("password").value = "";
  start();
}

function logout() {
  document.cookie = "trustyclaw_admin=" + adminCookieAttributes(0);
  location.reload();
}

function showLogin() {
  $("login").hidden = false;
  $("app").hidden = true;
  $("logout-button").hidden = true;
  $("agent-name").hidden = true;
}

function showTab(name) {
  activeTab = name;
  for (const tabName of ["home", "agent", "processes", "agent-log", "files", "network", "net-log"]) {
    $(`tab-${tabName}`).classList.toggle("active-tab", tabName === name);
    $(`panel-${tabName}`).hidden = tabName !== name;
  }
  refreshVisibleTab(name).catch(() => {});
}

async function refreshVisibleTab(name) {
  if (name === "agent-log") {
    await agentLog.showFirstPage();
  } else if (name === "net-log") {
    await netLog.showFirstPage();
  } else if (name === "processes") {
    await refreshAgentProcesses();
  } else if (name === "files") {
    await ensureFilesLoaded();
  }
}

async function tick() {
  await refreshOrSkip(refreshHealth);
  await refreshOrSkip(refreshProviderAccounts);
  await refreshOrSkip(loadThreads);
  await refreshOrSkip(refreshSelectedThread);
  if (activeTab === "agent-log" && agentLog.page === 1) await refreshOrSkip(() => agentLog.showFirstPage());
  if (activeTab === "net-log" && netLog.page === 1) await refreshOrSkip(() => netLog.showFirstPage());
  if (activeTab === "network") await refreshOrSkip(refreshPendingGithubPushes);
  if (activeTab === "processes") await refreshOrSkip(refreshAgentProcesses);
  if (activeTab === "files") await refreshOrSkip(refreshFiles);
}

async function refreshOrSkip(work) {
  try {
    await work();
  } catch (_error) {
    // Keep one failed dashboard section from preventing later sections, such
    // as the audit logs, from fetching their own backend state.
  }
}

function start() {
  if (!getPassword()) { showLogin(); return; }
  $("login").hidden = true;
  $("app").hidden = false;
  $("logout-button").hidden = false;
  renderThreadHistory();
  loadPolicy().catch(() => {});
  tick();
  setInterval(tick, 5000);
}

document.addEventListener("click", event => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  if (!target.closest(".info-button, #preset-info-popover")) closeIntegrationInfo();
  const button = target.closest("button[data-action]");
  if (!button) return;
  const { action } = button.dataset;
  const taskId = button.dataset.taskId;
  const threadId = button.dataset.threadId;
  const runtime = button.dataset.runtime;
  const path = button.dataset.path;
  const fileType = button.dataset.fileType;
  const actions = {
    "login": () => login(),
    "logout": () => logout(),
    "show-tab": () => showTab(button.dataset.tab),
    "start-login": () => startLogin(runtime),
    "reset-linked-account": () => resetLinkedAccount(button.dataset.provider),
    "complete-claude-login": () => completeClaudeLogin(),
    "refresh-provider-usage": () => refreshProviderUsage(),
    "reboot-host": () => rebootHost(),
    "show-thread": () => showThread(threadId, runtime),
    "show-task-events": () => showTaskEvents(taskId),
    "load-more-task-events": () => loadMoreTaskEvents(taskId),
    "file-up": () => loadParentDirectory(),
    "file-go": () => goToFilePath(),
    "open-file-path": () => openAgentPath(path, fileType),
    "load-policy": () => loadPolicy(),
    "toggle-integration-info": () => toggleIntegrationInfo(button.dataset.info, button),
    "toggle-integration-expansion": () => toggleIntegrationExpansion(button.dataset.integration),
    "toggle-custom-domain-access": () => toggleCustomDomainAccess(),
    "toggle-github-repo-audit": () => toggleGithubRepoAudit(button.dataset.repoKey),
    "enable-integration": () => setIntegrationEnabled(button.dataset.integration, true),
    "disable-integration": () => setIntegrationEnabled(button.dataset.integration, false),
    "add-github-repo": () => addGithubRepo(),
    "remove-github-repo": () => removeGithubRepo(button.dataset.owner, button.dataset.repo),
    "enable-github-require-approval": () => setGithubRequireApproval(true),
    "disable-github-require-approval": () => setGithubRequireApproval(false),
    "add-domain-rule": () => addDomainRule(),
    "remove-domain-rule": () => removeDomainRule(button.dataset.domain),
    "set-github-credential": () => setGithubCredential(),
    "recheck-github-audit": () => recheckGithubAudit(),
    "delete-github-credential": () => deleteGithubCredential(),
    "toggle-net-denied": () => toggleNetDeniedFilter(),
    "net-page": () => netLog.showPage(button.dataset.page).catch(() => {}),
    "agent-page": () => agentLog.showPage(button.dataset.page).catch(() => {}),
    "approve-github-push": () => approveGithubPush(button.dataset.id),
    "reject-github-push": () => rejectGithubPush(button.dataset.id),
  };
  const handler = actions[action];
  if (handler) handler();
});

setUnauthorizedHandler(showLogin);
document.addEventListener("keydown", event => { if (event.key === "Escape") closeIntegrationInfo(); });
window.addEventListener("resize", positionIntegrationInfo);
document.addEventListener("scroll", positionIntegrationInfo, true);
$("github-credential-mode").addEventListener("change", toggleGithubCredentialMode);
$("password").addEventListener("keydown", event => { if (event.key === "Enter") login(); });
$("file-path").addEventListener("keydown", event => { if (event.key === "Enter") goToFilePath(); });
start();
