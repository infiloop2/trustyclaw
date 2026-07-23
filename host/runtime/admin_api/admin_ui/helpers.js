// Pure DOM and formatting helpers shared by every admin UI module. Nothing
// here owns feature state or calls the backend.

export const $ = id => document.getElementById(id);

export const RUNTIME_PROVIDERS = {
  codex: { label: "Codex", provider: "openai", providerLabel: "OpenAI" },
  claude_code: { label: "Claude Code", provider: "claude", providerLabel: "Claude" },
  hermes: { label: "Hermes", provider: "bedrock", providerLabel: "AWS Bedrock" },
};

export function providerRuntime(provider) {
  for (const [runtime, meta] of Object.entries(RUNTIME_PROVIDERS)) {
    if (meta.provider === provider) return runtime;
  }
  return null;
}

export function runtimeLabel(runtime) {
  return RUNTIME_PROVIDERS[runtime]?.label || runtime;
}

export function notice(message, kind) {
  const node = $("notice");
  // A 401 already moves the operator to the password prompt. Keep the error
  // intact for callers and every authenticated view, but omit the redundant
  // toast when the login screen itself is asking for the password.
  const visibleMessage = message === "unauthorized" && !$("login").hidden ? "" : message;
  node.textContent = visibleMessage || "";
  node.classList.toggle("error", kind === "error");
  if (visibleMessage) setTimeout(() => { node.textContent = ""; }, 8000);
}

const inlineMessageTimers = new WeakMap();

export function inlineMessage(node, message, isError = false) {
  if (!node) return;
  const timer = inlineMessageTimers.get(node);
  if (timer) clearTimeout(timer);
  inlineMessageTimers.delete(node);
  node.textContent = message || "";
  node.classList.toggle("error", isError === true);
  if (message && !isError) {
    inlineMessageTimers.set(node, setTimeout(() => {
      node.textContent = "";
      inlineMessageTimers.delete(node);
    }, 8000));
  }
}

export function badge(value) { return `<span class="status ${value}">${value}</span>`; }

export function esc(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

export function informationIcon() {
  return `<svg class="integration-info-icon" viewBox="0 0 20 20" aria-hidden="true">
    <circle cx="10" cy="10" r="7.25" fill="none" stroke="currentColor" stroke-width="1.5"/>
    <circle cx="10" cy="6.55" r="1" fill="currentColor"/>
    <path d="M10 9.35v4.1" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round"/>
  </svg>`;
}

// Catalog copy can mark short path names with backticks. Escape every segment
// before wrapping those marked spans, so manifests cannot inject markup.
export function inlineCode(value) {
  return String(value == null ? "" : value)
    .split("`")
    .map((part, index) => index % 2 ? `<code>${esc(part)}</code>` : esc(part))
    .join("");
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

// Managed and bundled tool rows are rendered by separate modules into one
// operator-facing list. Replace only the caller's rows, then sort the shared
// list by its visible names.
export function replaceIntegrationRows(container, selector, html) {
  for (const child of [...container.children]) {
    if (child.matches(selector)) child.remove();
  }
  const template = document.createElement("template");
  template.innerHTML = html;
  container.append(template.content);
  const rows = [...container.children].filter(child => child.matches(".integration-row"));
  rows.sort((left, right) => {
    const leftLabel = left.querySelector("h2")?.textContent || "";
    const rightLabel = right.querySelector("h2")?.textContent || "";
    return leftLabel.localeCompare(rightLabel, undefined, { sensitivity: "base" });
  });
  for (const row of rows) container.append(row);
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

export function formatTokenCount(value) {
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}k`;
  return String(value);
}

// Month-to-date usage the proxy metered live from Bedrock responses, formatted
// for display; null when the payload is absent.
export function bedrockUsage(account) {
  const usage = account.bedrock_usage;
  const amount = usage && typeof usage === "object" ? Number(usage.month_to_date) : NaN;
  if (!Number.isFinite(amount)) return null;
  const currency = !usage.currency || usage.currency === "USD" ? "$" : `${usage.currency} `;
  return {
    cost: `${currency}${amount.toFixed(2)}`,
    inputTokens: Number(usage.input_tokens) || 0,
    outputTokens: Number(usage.output_tokens) || 0,
    cacheReadTokens: Number(usage.cache_read_tokens) || 0,
    cacheWriteTokens: Number(usage.cache_write_tokens) || 0,
    requests: Number(usage.requests) || 0,
    meteredRequests: Number(usage.metered_requests) || 0,
  };
}
