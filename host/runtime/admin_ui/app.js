// Entry module: session lifecycle (login, logout), tab switching, the
// 5-second refresh tick, and the one delegated click dispatcher that maps
// data-action buttons to feature handlers. Feature code lives in the sibling
// modules; this file is the only place that wires them together.

import { api, getPassword, setUnauthorizedHandler } from "./api.js";
import { $, notice } from "./helpers.js";
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
let installedApps = [];
const appFrames = new Map();
const staticTabs = ["home", "agent", "processes", "agent-log", "files", "network", "net-log"];

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
  for (const tabName of staticTabs) {
    $(`tab-${tabName}`).classList.toggle("active-tab", tabName === name);
    $(`panel-${tabName}`).hidden = tabName !== name;
  }
  for (const app of installedApps) {
    const selected = name === `app:${app.id}`;
    $(`tab-app-${app.id}`)?.classList.toggle("active-tab", selected);
    const panel = $(`panel-app-${app.id}`);
    if (panel) panel.hidden = !selected;
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
  loadApps().catch(error => notice(error.message));
  renderThreadHistory();
  loadPolicy().catch(() => {});
  tick();
  setInterval(tick, 5000);
}

async function loadApps() {
  const response = await api("GET", "/v1/apps");
  installedApps = response.apps || [];
  renderAppTabs();
}

function renderAppTabs() {
  const container = $("app-tabs");
  container.innerHTML = "";
  document.querySelectorAll(".app-tab-panel").forEach(panel => panel.remove());
  appFrames.clear();
  if (!installedApps.length) {
    container.innerHTML = `<div class="sidebar-empty">No apps installed</div>`;
    return;
  }
  const main = document.querySelector("main");
  for (const app of installedApps) {
    const button = document.createElement("button");
    button.id = `tab-app-${app.id}`;
    button.className = "tab-button app-tab-button";
    button.dataset.action = "show-tab";
    button.dataset.tab = `app:${app.id}`;
    button.innerHTML = `${appIconSvg()}<span></span>`;
    button.querySelector("span").textContent = app.title || app.id;
    container.appendChild(button);

    const panel = document.createElement("div");
    panel.id = `panel-app-${app.id}`;
    panel.className = "tab-panel app-tab-panel";
    panel.hidden = activeTab !== `app:${app.id}`;
    const section = document.createElement("section");
    section.className = "app-frame-section";
    const iframe = document.createElement("iframe");
    iframe.className = "app-frame";
    iframe.title = app.title || app.id;
    iframe.src = app.ui.iframe_src;
    iframe.setAttribute("sandbox", (app.ui.sandbox || ["allow-scripts", "allow-forms"]).join(" "));
    section.appendChild(iframe);
    panel.appendChild(section);
    main.appendChild(panel);
    appFrames.set(app.id, iframe);
  }
}

function appIconSvg() {
  return `<svg width="19" height="19" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h9A1.5 1.5 0 0 1 16 5.5v9a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 4 14.5v-9Z" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M7 8h6M7 11h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
}

window.addEventListener("message", event => {
  const message = event.data;
  if (!message || message.type !== "trustyclaw-app-api") return;
  const app = installedApps.find(candidate => appFrames.get(candidate.id)?.contentWindow === event.source);
  if (!app) return;
  handleAppApiMessage(app, event.source, message).catch(error => {
    event.source.postMessage({
      type: "trustyclaw-app-api-result",
      request_id: message.request_id,
      ok: false,
      error: error.message,
    }, "*");
  });
});

async function handleAppApiMessage(app, source, message) {
  if (!["GET", "POST", "PUT", "DELETE"].includes(message.method) || typeof message.path !== "string" || !message.path.startsWith("/v1/")) {
    throw new Error("invalid app API request");
  }
  const canonical = canonicalAppBridgePath(message.path);
  if (!canonical || !isAppBridgeAllowed(app, canonical.pathname)) {
    throw new Error("app API route is not allowed");
  }
  const body = await api(message.method, canonical.requestPath, message.body, { "X-TrustyClaw-App-Bridge": app.id });
  source.postMessage({
    type: "trustyclaw-app-api-result",
    request_id: String(message.request_id || ""),
    ok: true,
    body,
  }, "*");
}

function canonicalAppBridgePath(path) {
  if (path.includes("\\")) return null;
  let url;
  try {
    url = new URL(path, window.location.origin);
  } catch (_error) {
    return null;
  }
  if (url.origin !== window.location.origin || !url.pathname.startsWith("/v1/")) return null;
  return { pathname: url.pathname, requestPath: url.pathname + url.search };
}

function isAppBridgeAllowed(app, path) {
  const route = app && app.backend && app.backend.api_route;
  return typeof route === "string" && path.startsWith(route);
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
