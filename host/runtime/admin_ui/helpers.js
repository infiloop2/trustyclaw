// Pure DOM and formatting helpers shared by every admin UI module. Nothing
// here owns feature state or calls the backend.

export const $ = id => document.getElementById(id);

export const RUNTIME_PROVIDERS = {
  codex: { label: "Codex", provider: "openai", providerLabel: "OpenAI" },
  claude_code: { label: "Claude Code", provider: "claude", providerLabel: "Claude" },
};

export function runtimeLabel(runtime) {
  return RUNTIME_PROVIDERS[runtime]?.label || runtime;
}

export function notice(message) {
  $("notice").textContent = message || "";
  if (message) setTimeout(() => { $("notice").textContent = ""; }, 8000);
}

export function badge(value) { return `<span class="status ${value}">${value}</span>`; }

export function esc(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

export function formatNetworkReason(reason) {
  const text = String(reason || "").trim().replace(/_+/g, " ").replace(/\.+$/g, "");
  if (!text) return "";
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function networkEventTarget(event) {
  const protocol = event.protocol || "";
  const host = event.host || "";
  const port = event.port == null ? "" : `:${event.port}`;
  const path = event.path || "";
  const query = event.query ? `?${event.query}` : "";
  return `${protocol}://${host}${port}${path}${query}`;
}

// Skip innerHTML swaps when nothing changed: the UI polls every 5 seconds and
// unconditional re-renders would break in-flight taps, hover, and selection.
export function setHtml(el, html) {
  if (el.__lastHtml === html) return;
  el.__lastHtml = html;
  el.innerHTML = html;
}

export function gib(bytes) { return (bytes / 1073741824).toFixed(1); }

export function mib(bytes) { return (bytes / 1048576).toFixed(1); }

export function clampPercent(value) {
  return Math.max(0, Math.min(100, Number(value) || 0)).toFixed(1);
}

export function formatUnixTime(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return formatDateTime(numeric * 1000);
}

export function formatDateTime(value) {
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

export function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

export function eventPayloadText(event) {
  const payload = event && event.payload && typeof event.payload === "object" ? event.payload : {};
  if (payload.message) return payload.message;
  if (payload.error_message) return payload.error_message;
  return Object.keys(payload).length ? JSON.stringify(payload) : "";
}

export function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}
