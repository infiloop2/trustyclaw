// The three audit logs (agent events, network events, tool events), one tab
// each. All share one model:
// page 1 is a live tail refreshed while it is visible, deeper pages are
// stable before-cursor snapshots fetched on demand (new inserts never shift
// a snapshot, because its cursor pins it).

import { api } from "./api.js";
import {
  $, badge, esc, eventPayloadText, formatDateTime, formatNetworkReason,
  networkEventTarget, setHtml,
} from "./helpers.js";

const EVENT_PAGE_SIZE = 100;
const EVENT_PAGER_WINDOW = 10;

let netEventFilter = "all";

function createPagedLog(config) {
  const log = {
    pages: [],
    page: 1,
    hasMore: true,
    async fetchPage(before) {
      const params = new URLSearchParams(config.query ? config.query() : {});
      if (before !== null) params.set("before", String(before));
      const suffix = params.toString();
      const response = await api("GET", config.endpoint + (suffix ? `?${suffix}` : ""));
      return Array.isArray(response.events) ? response.events : [];
    },
    async showFirstPage() {
      if (config.pauseWhileOpen && document.querySelector(config.pauseWhileOpen)) return;
      const events = await log.fetchPage(null);
      log.pages = [events];
      log.page = 1;
      log.hasMore = events.length === EVENT_PAGE_SIZE;
      log.render();
    },
    async showPage(requested) {
      const target = Math.max(Number(requested) || 1, 1);
      if (target === 1) return log.showFirstPage();
      while (!log.pages[target - 1] && log.hasMore) {
        const previous = log.pages[log.pages.length - 1] || [];
        if (!previous.length) break;
        const events = await log.fetchPage(previous[previous.length - 1].seq);
        if (!events.length) {
          log.hasMore = false;
          break;
        }
        log.pages.push(events);
        log.hasMore = events.length === EVENT_PAGE_SIZE;
      }
      log.page = Math.min(target, Math.max(log.pages.length, 1));
      log.render();
    },
    render() {
      const events = log.pages[log.page - 1] || [];
      const loadedPages = Math.max(log.pages.length, 1);
      const pageTotal = log.hasMore ? `${loadedPages}+` : String(loadedPages);
      const live = log.page === 1 ? " · live" : "";
      $(config.summaryId).textContent = events.length || log.page > 1
        ? `Page ${log.page} of ${pageTotal}${config.summarySuffix ? config.summarySuffix() : ""}${live}`
        : config.emptySummary();
      setHtml($(config.tableId), config.header + (events.length
        ? events.map(config.row).join("")
        : `<tr><td colspan="${config.columns}" class="empty-state">${config.emptyState()}</td></tr>`));
      renderLogPager(config.pagerId, config.pageAction, log);
    },
  };
  return log;
}

// Numbered buttons stay within a sliding window of EVENT_PAGER_WINDOW pages
// around the current one; "1 …" jumps back and Next keeps extending past the
// window (the total is unknown under cursor pagination).
function renderLogPager(elementId, action, log) {
  const loadedPages = Math.max(log.pages.length, 1);
  const lastButtonPage = log.hasMore ? loadedPages + 1 : loadedPages;
  const button = page =>
    `<button class="ghost sm${page === log.page ? " active-page" : ""}" data-action="${action}" data-page="${page}"${page === log.page ? " disabled" : ""}>${page}</button>`;
  const end = Math.min(Math.max(log.page + Math.floor(EVENT_PAGER_WINDOW / 2), EVENT_PAGER_WINDOW), lastButtonPage);
  const start = Math.max(1, end - EVENT_PAGER_WINDOW + 1);
  const pageButtons = [];
  if (start > 1) pageButtons.push(button(1), `<span class="muted pager-gap">…</span>`);
  for (let page = start; page <= end; page += 1) pageButtons.push(button(page));
  const nextDisabled = !log.hasMore && log.page >= loadedPages;
  setHtml($(elementId), `
    <button class="ghost sm" data-action="${action}" data-page="${log.page - 1}"${log.page <= 1 ? " disabled" : ""}>Previous</button>
    ${pageButtons.join("")}
    <button class="ghost sm" data-action="${action}" data-page="${log.page + 1}"${nextDisabled ? " disabled" : ""}>Next</button>
  `);
}

export const agentLog = createPagedLog({
  endpoint: "/v1/events",
  summaryId: "agent-page-summary",
  tableId: "events",
  pagerId: "agent-event-pager",
  pageAction: "agent-page",
  columns: 4,
  header: `<tr><th>time</th><th>type</th><th>task</th><th>payload</th></tr>`,
  emptySummary: () => "No events",
  emptyState: () => "No agent audit events yet.",
  row: event => `
    <tr>
      <td class="muted time">${esc(formatDateTime(event.timestamp))}</td>
      <td class="mono">${esc(event.event_type)}</td>
      <td class="mono">${esc(event.task_id || "")}</td>
      <td><pre>${esc(eventPayloadText(event))}</pre></td>
    </tr>`,
});

export const netLog = createPagedLog({
  endpoint: "/v1/network/events",
  query: () => (netEventFilter === "denied" ? { decision: "denied" } : {}),
  summaryId: "net-page-summary",
  tableId: "net-events",
  pagerId: "net-event-pager",
  pageAction: "net-page",
  columns: 5,
  header: `<tr><th>time</th><th>method</th><th>target</th><th>decision</th><th>reason</th></tr>`,
  summarySuffix: () => (netEventFilter === "denied" ? " · denied only" : ""),
  emptySummary: () => `No ${netEventFilter === "denied" ? "denied " : ""}events`,
  emptyState: () => `No ${netEventFilter === "denied" ? "denied " : ""}network audit events.`,
  row: event => `
    <tr>
      <td class="muted time">${esc(formatDateTime(event.timestamp))}</td>
      <td class="mono">${esc(event.method)}</td>
      <td class="mono">${esc(networkEventTarget(event))}</td>
      <td>${badge(event.decision)}</td>
      <td>${event.decision === "denied" && event.reason_code ? esc(formatNetworkReason(event.reason_code)) : ""}</td>
    </tr>`,
});

export const toolLog = createPagedLog({
  endpoint: "/v1/tools/events",
  summaryId: "tool-page-summary",
  tableId: "tool-events",
  pagerId: "tool-event-pager",
  pageAction: "tool-page",
  pauseWhileOpen: "#tool-events .tool-event-arguments[open]",
  columns: 5,
  header: `<tr><th>time</th><th>tool</th><th>action</th><th>outcome</th><th>arguments</th></tr>`,
  emptySummary: () => "No events",
  emptyState: () => "No tool audit events yet.",
  row: event => `
    <tr>
      <td class="muted time">${esc(formatDateTime(event.timestamp))}</td>
      <td class="mono">${esc(event.tool_id)}</td>
      <td class="mono">${esc(event.action_id)}</td>
      <td>${badge(event.outcome)}${event.detail ? ` <span class="muted">${esc(event.detail)}</span>` : ""}</td>
      <td>${event.has_arguments ? `<details class="tool-event-arguments" data-tool-event-seq="${esc(event.seq)}"><summary class="muted">view</summary><pre class="metadata"></pre></details>` : ""}</td>
    </tr>`,
});

// Argument objects can each be up to 64 KiB. Audit pages therefore return only
// has_arguments and load one exact object when the operator expands it, instead
// of transferring up to 100 payloads on every five-second live refresh.
document.addEventListener("toggle", async event => {
  const details = event.target;
  if (!(details instanceof HTMLDetailsElement) || !details.open || !details.matches(".tool-event-arguments")) return;
  const seq = details.dataset.toolEventSeq;
  const pre = details.querySelector("pre.metadata");
  if (!seq || !pre || pre.dataset.filled === "1") return;
  pre.dataset.filled = "1";
  try {
    const response = await api("GET", `/v1/tools/events/${encodeURIComponent(seq)}`);
    pre.textContent = JSON.stringify(response.event.arguments, null, 2);
  } catch (error) {
    pre.textContent = `(could not load arguments: ${error.message})`;
    pre.dataset.filled = "";
  }
}, true);

export function toggleNetDeniedFilter() {
  netEventFilter = netEventFilter === "denied" ? "all" : "denied";
  $("net-filter-denied").textContent = netEventFilter === "denied" ? "Show all" : "Show denied";
  netLog.showFirstPage().catch(() => {});
}
