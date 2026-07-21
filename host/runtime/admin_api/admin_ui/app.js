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
import { agentLog, netLog, toolLog, toggleNetDeniedFilter } from "./logs.js";
import {
  addDomainRule, addGithubRepo, approveGithubPush, closeIntegrationInfo, deleteGithubCredential,
  loadPolicy, openProvider, recheckGithubAudit, rejectGithubPush, removeDomainRule,
  removeGithubRepo, resetLinkedAccount, connectBedrockCredentials, setClaudeWebSearch, setGithubCredential, setGithubRequireApproval,
  setIntegrationEnabled, positionIntegrationInfo, refreshPendingGithubPushes, toggleGithubCredentialMode, toggleIntegrationExpansion,
  toggleCustomDomainAccess, toggleGithubRepoAudit, toggleIntegrationInfo,
} from "./network.js";
import {
  completeToolConnect, connectTool, decideToolApproval, disconnectTool,
  refreshExpandedToolApprovals, refreshTools, saveToolConfig, setToolEnabled,
  toggleToolExpansion, toggleToolInfo,
} from "./tools.js";
import {
  copyCallbackUri, dismissCallbackCopyFeedback, openConnectionGuide, refreshConnectionGuide,
} from "./connection_guide.js";

let activeTab = "home";
let installedApps = [];
const appFrames = new Map();
const staticTabs = ["home", "agent", "processes", "agent-log", "files", "network", "connection-guide", "net-log", "tool-log"];
const HERO_APP_ID = "agent_chat";
const HERO_CTA = "Begin chat";
const MOBILE_NAV_QUERY = "(max-width: 860px)";
let mobileNavOpen = false;

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

function setMobileNavOpen(open, restoreFocus = false) {
  const mobile = window.matchMedia(MOBILE_NAV_QUERY).matches;
  mobileNavOpen = mobile && open;
  const sidebar = $("sidebar");
  const toggle = $("mobile-nav-toggle");
  sidebar.classList.toggle("mobile-open", mobileNavOpen);
  sidebar.inert = mobile && !mobileNavOpen;
  document.querySelector(".topbar").inert = mobileNavOpen;
  document.querySelector("main").inert = mobileNavOpen;
  $("nav-backdrop").hidden = !mobileNavOpen;
  toggle.setAttribute("aria-expanded", String(mobileNavOpen));
  toggle.setAttribute("aria-label", mobileNavOpen ? "Close navigation" : "Open navigation");
  document.body.classList.toggle("nav-open", mobileNavOpen);
  if (mobileNavOpen) {
    $("mobile-nav-close").focus();
  } else if (restoreFocus && mobile) {
    toggle.focus();
  }
}

function toggleMobileNav() {
  setMobileNavOpen(!mobileNavOpen, mobileNavOpen);
}

function showLogin() {
  setMobileNavOpen(false);
  document.body.classList.remove("connection-guide-open");
  document.body.classList.remove("app-tab-open");
  $("login").hidden = false;
  $("app").hidden = true;
  $("logout-button").hidden = true;
  $("agent-name").hidden = true;
  $("runtime-overview").hidden = true;
  $("upgrade-notice").hidden = true;
  $("mobile-nav-toggle").hidden = true;
}

function showTab(name) {
  const closeDrawer = mobileNavOpen;
  activeTab = name;
  const connectionGuideOpen = name === "connection-guide";
  const appTabOpen = name.startsWith("app:");
  if (connectionGuideOpen || appTabOpen) window.scrollTo(0, 0);
  document.body.classList.toggle("connection-guide-open", connectionGuideOpen);
  document.body.classList.toggle("app-tab-open", appTabOpen);
  for (const tabName of staticTabs) {
    $(`tab-${tabName}`).classList.toggle("active-tab", tabName === name);
    $(`panel-${tabName}`).hidden = tabName !== name;
  }
  for (const app of installedApps) {
    const selected = name === `app:${app.id}`;
    $(`tab-app-${app.id}`)?.classList.toggle("active-tab", selected);
    const panel = $(`panel-app-${app.id}`);
    if (panel) panel.hidden = !selected;
    if (selected) loadAppFrame(app);
  }
  setMobileNavOpen(false, closeDrawer);
  refreshVisibleTab(name).catch(() => {});
}

async function refreshVisibleTab(name) {
  if (name === "agent-log") {
    await agentLog.showFirstPage();
  } else if (name === "net-log") {
    await netLog.showFirstPage();
  } else if (name === "tool-log") {
    await toolLog.showFirstPage();
  } else if (name === "processes") {
    await refreshAgentProcesses();
  } else if (name === "files") {
    await ensureFilesLoaded();
  } else if (name === "network") {
    // Tool rows hold config inputs, so they refresh on tab entry and after
    // actions only, never on the 5-second tick (that would wipe half-typed
    // values). Expanded approvals carry no inputs and also refresh on the
    // tick below.
    await refreshTools();
    await refreshExpandedToolApprovals();
  } else if (name === "connection-guide") {
    await refreshConnectionGuide();
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
  if (activeTab === "network") await refreshOrSkip(refreshExpandedToolApprovals);
  if (activeTab === "tool-log" && toolLog.page === 1) await refreshOrSkip(() => toolLog.showFirstPage());
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
  $("mobile-nav-toggle").hidden = false;
  $("runtime-overview").hidden = false;
  setMobileNavOpen(false);
  loadApps().catch(error => notice(error.message, "error"));
  renderThreadHistory();
  loadPolicy().catch(() => {});
  // The provider redirects a tool OAuth connect back to /oauth/callback; land
  // on the network tab and finish the token exchange the operator started.
  if (location.pathname === "/oauth/callback") {
    showTab("network");
    completeToolConnect().catch(error => notice(error.message, "error"));
  }
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
  $("hero-app-tab").innerHTML = "";
  document.querySelectorAll(".app-tab-panel").forEach(panel => panel.remove());
  appFrames.clear();
  // Agent Chat is hardwired as the host's main interface: the home hero
  // navigator carries its CTA, and its nav entry sits directly below Home
  // instead of inside Apps (Beta). Every other installed app belongs to that
  // beta section. Shell presentation only: app manifests and /v1/apps carry
  // neither hero nor maturity fields.
  const heroApp = installedApps.find(app => app.id === HERO_APP_ID) || null;
  renderHomeHero(heroApp);
  const listedApps = installedApps.filter(app => app !== heroApp);
  if (!listedApps.length) {
    container.innerHTML = `<div class="sidebar-empty">No apps installed</div>`;
  }
  const main = document.querySelector("main");
  for (const app of installedApps) {
    const button = document.createElement("button");
    button.id = `tab-app-${app.id}`;
    button.className = app === heroApp ? "tab-button hero-app-tab" : "tab-button";
    button.dataset.action = "show-tab";
    button.dataset.tab = `app:${app.id}`;
    if (app === heroApp) {
      button.innerHTML = `${chatIconSvg()}<span></span>`;
      button.querySelector("span").textContent = app.title || app.id;
      $("hero-app-tab").appendChild(button);
    } else {
      button.innerHTML = `${appIconSvg()}<span></span>`;
      button.querySelector("span").textContent = app.title || app.id;
      container.appendChild(button);
    }

    const panel = document.createElement("div");
    panel.id = `panel-app-${app.id}`;
    panel.className = "tab-panel app-tab-panel";
    panel.hidden = activeTab !== `app:${app.id}`;
    const section = document.createElement("section");
    section.className = "app-frame-section";
    panel.appendChild(section);
    main.appendChild(panel);
    if (activeTab === `app:${app.id}`) loadAppFrame(app);
  }
}

function loadAppFrame(app) {
  if (appFrames.has(app.id)) return;
  const section = $(`panel-app-${app.id}`)?.querySelector(".app-frame-section");
  if (!section) return;
  const iframe = document.createElement("iframe");
  iframe.className = "app-frame";
  iframe.title = app.title || app.id;
  iframe.setAttribute("sandbox", app.ui.sandbox.join(" "));
  iframe.src = app.ui.iframe_src;
  section.appendChild(iframe);
  appFrames.set(app.id, iframe);
}

function renderHomeHero(heroApp) {
  const hero = $("home-hero");
  hero.hidden = !heroApp;
  hero.innerHTML = "";
  if (!heroApp) return;
  const card = document.createElement("section");
  card.className = "home-hero-card";
  card.innerHTML = `
    <div class="home-hero-copy">
      <span class="home-hero-icon">${chatIconSvg()}</span>
      <h1></h1>
    </div>
    <button class="home-hero-cta" data-action="show-tab"></button>`;
  card.querySelector("h1").textContent = heroApp.title || heroApp.id;
  const cta = card.querySelector(".home-hero-cta");
  cta.dataset.tab = `app:${heroApp.id}`;
  cta.textContent = HERO_CTA;
  hero.appendChild(card);
}

function appIconSvg() {
  return `<svg width="19" height="19" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h9A1.5 1.5 0 0 1 16 5.5v9a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 4 14.5v-9Z" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M7 8h6M7 11h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
}

function chatIconSvg() {
  return `<svg width="19" height="19" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 4.5h12A1.5 1.5 0 0 1 17.5 6v6a1.5 1.5 0 0 1-1.5 1.5H9.4L6 16.5v-3H4A1.5 1.5 0 0 1 2.5 12V6A1.5 1.5 0 0 1 4 4.5Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M6.5 8h7M6.5 10.6h4.6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
}

window.addEventListener("message", event => {
  const message = event.data;
  if (!message || !["trustyclaw-app-api", "trustyclaw-app-open-file"].includes(message.type)) return;
  const app = installedApps.find(candidate => appFrames.get(candidate.id)?.contentWindow === event.source);
  if (!app) return;
  if (message.type === "trustyclaw-app-open-file") {
    const path = typeof message.path === "string" ? message.path : "";
    if (!path.startsWith("/") || path.split("/").includes("..")) return;
    showTab("files");
    openAgentPath(path, "file").catch(error => notice(error.message, true));
    return;
  }
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
  // Friendly pre-check only: the admin API enforces the bridge scope
  // server-side (a bridge-tagged request outside the app's own API is 403).
  const route = app && app.backend && app.backend.api_route;
  if (
    !["GET", "POST", "PUT", "DELETE"].includes(message.method) ||
    typeof message.path !== "string" ||
    typeof route !== "string" ||
    !message.path.startsWith(route)
  ) {
    throw new Error("app API route is not allowed");
  }
  const body = await api(message.method, message.path, message.body, { "X-TrustyClaw-App-Bridge": app.id });
  source.postMessage({
    type: "trustyclaw-app-api-result",
    request_id: String(message.request_id || ""),
    ok: true,
    body,
  }, "*");
}

document.addEventListener("click", event => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  if (!target.closest(".info-button, #preset-info-popover")) closeIntegrationInfo();
  if (!target.closest(".guide-copy-button")) dismissCallbackCopyFeedback();
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
    "toggle-mobile-nav": () => toggleMobileNav(),
    "close-mobile-nav": () => setMobileNavOpen(false, true),
    "show-tab": () => showTab(button.dataset.tab),
    "open-provider": () => { showTab("network"); openProvider(button.dataset.provider); },
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
    "enable-claude-web-search": () => setClaudeWebSearch(true),
    "disable-claude-web-search": () => setClaudeWebSearch(false),
    "connect-bedrock-credentials": () => connectBedrockCredentials(button.dataset.integration),
    "add-domain-rule": () => addDomainRule(),
    "remove-domain-rule": () => removeDomainRule(button.dataset.domain),
    "set-github-credential": () => setGithubCredential(),
    "recheck-github-audit": () => recheckGithubAudit(),
    "delete-github-credential": () => deleteGithubCredential(),
    "toggle-net-denied": () => toggleNetDeniedFilter(),
    "net-page": () => netLog.showPage(button.dataset.page).catch(() => {}),
    "agent-page": () => agentLog.showPage(button.dataset.page).catch(() => {}),
    "tool-page": () => toolLog.showPage(button.dataset.page).catch(() => {}),
    "approve-github-push": () => approveGithubPush(button.dataset.id),
    "reject-github-push": () => rejectGithubPush(button.dataset.id),
    "enable-tool": () => setToolEnabled(button.dataset.tool, true),
    "disable-tool": () => setToolEnabled(button.dataset.tool, false),
    "save-tool-config": () => saveToolConfig(button.dataset.tool, button.dataset.key),
    "connect-tool": () => connectTool(button.dataset.tool),
    "disconnect-tool": () => disconnectTool(button.dataset.tool),
    "toggle-tool-info": () => toggleToolInfo(button.dataset.tool, button),
    "toggle-tool-expansion": () => toggleToolExpansion(button.dataset.tool),
    "decide-approval": () => decideToolApproval(button.dataset.tool, button.dataset.approvalId, button.dataset.decision),
    "open-connection-guide": () => {
      closeIntegrationInfo();
      openConnectionGuide(button.dataset.guide);
      showTab("connection-guide");
    },
    "jump-connection-guide": () => openConnectionGuide(button.dataset.guide),
    "copy-callback-uri": () => copyCallbackUri(button),
  };
  const handler = actions[action];
  if (handler) handler();
});

setUnauthorizedHandler(showLogin);
document.addEventListener("keydown", event => {
  if (event.key !== "Escape") return;
  closeIntegrationInfo();
  if (mobileNavOpen) setMobileNavOpen(false, true);
});
window.addEventListener("resize", () => {
  positionIntegrationInfo();
  setMobileNavOpen(mobileNavOpen);
});
document.addEventListener("scroll", positionIntegrationInfo, true);
$("github-credential-mode").addEventListener("change", toggleGithubCredentialMode);
$("connection-guide-select").addEventListener("change", event => openConnectionGuide(event.target.value));
$("password").addEventListener("keydown", event => { if (event.key === "Enter") login(); });
$("file-path").addEventListener("keydown", event => { if (event.key === "Enter") goToFilePath(); });
start();
