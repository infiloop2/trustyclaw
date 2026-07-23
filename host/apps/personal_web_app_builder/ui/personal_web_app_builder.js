"use strict";

const APP_ID = "personal_web_app_builder";
const MAX_WORKER_MESSAGES_PER_SECOND = 100;
const MAX_WORKER_MESSAGES_PER_TURN = 128;
const MAX_WORKER_MUTATIONS_PER_TURN = 16;
const WORKER_TURN_TIMEOUT_MS = 3000;
const MAX_RENDER_HTML_BYTES = 128 * 1024;
const MAX_RENDER_CSS_BYTES = 64 * 1024;
const MAX_CSS_CONDITION_BYTES = 512;
const MAX_AGENT_MESSAGE_BYTES = 4000;
const MAX_EVENT_FIELDS = 64;
const MAX_EVENT_FIELD_BYTES = 8192;
const MAX_EVENT_PAYLOAD_BYTES = 64 * 1024;
const MAX_DATA_VALUE_BYTES = 256 * 1024;
const textEncoder = new TextEncoder();
const textDecoder = new TextDecoder();
const pendingApi = new Map();
let requestCounter = 0;
let sessionOptions = {};
let snapshot = { app: null, tasks: [], session: null };
let renderedRevision = -1;
let generatedRoot = null;
let workerRun = null;
let pollBusy = false;
let messageBusy = false;
let establishedSession = null;
let establishedSessionKey = "";

const $ = id => document.getElementById(id);

window.addEventListener("message", event => {
  const message = event.data;
  if (event.source !== parent || !message || message.type !== "trustyclaw-app-api-result") return;
  const pending = pendingApi.get(message.request_id);
  if (!pending) return;
  pendingApi.delete(message.request_id);
  if (message.ok) pending.resolve(message.body);
  else pending.reject(new Error(message.error || "App request failed"));
});

function api(method, path, body) {
  const requestId = `pwa-${Date.now()}-${++requestCounter}`;
  return new Promise((resolve, reject) => {
    pendingApi.set(requestId, { resolve, reject });
    parent.postMessage({
      type: "trustyclaw-app-api",
      request_id: requestId,
      method,
      path: `/v1/apps/${APP_ID}/api${path}`,
      body,
    }, "*");
    setTimeout(() => {
      if (!pendingApi.has(requestId)) return;
      pendingApi.delete(requestId);
      reject(new Error("App request timed out"));
    }, 12000);
  });
}

function capabilityWorkerBootstrap(maxRenderHtmlBytes, maxRenderCssBytes) {
  "use strict";
  // The frame CSP is authoritative. Function.prototype.constructor can still
  // recover the real Function constructor and WebAssembly remains available,
  // so script-src must keep unsafe-eval and wasm-unsafe-eval absent. The blob
  // worker inherits that policy and connect-src 'none'. Scrubbing common
  // globals below is defense in depth, not the code-execution or egress bound.
  const send = globalThis.postMessage.bind(globalThis);
  const clone = globalThis.structuredClone.bind(globalThis);
  const resolvePromise = Promise.resolve.bind(Promise);
  const encodeText = TextEncoder.prototype.encode.bind(new TextEncoder());
  const denied = () => { throw new Error("This capability is not available"); };
  const deniedGlobals = [
    "fetch", "XMLHttpRequest", "WebSocket", "EventSource", "RTCPeerConnection",
    "webkitRTCPeerConnection", "Worker", "SharedWorker", "importScripts",
    "WebSocketStream", "WebTransport", "FontFace", "BroadcastChannel", "indexedDB",
    "caches", "navigator", "eval", "Function",
  ];
  for (const name of deniedGlobals) {
    let target = globalThis;
    while (target) {
      const descriptor = Object.getOwnPropertyDescriptor(target, name);
      if (descriptor && descriptor.configurable) delete target[name];
      target = Object.getPrototypeOf(target);
    }
    try {
      Object.defineProperty(globalThis, name, {
        value: denied, writable: false, configurable: false, enumerable: false,
      });
    } catch (_error) {
      // CSP is still the authoritative network and code-loading boundary if
      // a browser exposes a non-configurable compatibility property.
    }
  }

  let durableData = {};
  let requestId = 0;
  let loadHandler = null;
  const handlers = new Map();
  const pending = new Map();
  const actionName = value => {
    if (typeof value !== "string" || !/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(value)) {
      throw new TypeError("action must be a bounded name");
    }
    return value;
  };
  const mutation = (action, path, value, includeValue) => new Promise((resolve, reject) => {
    const id = `mutation-${++requestId}`;
    pending.set(id, { resolve, reject });
    const message = { type: "data-action", request_id: id, action, path: clone(path) };
    if (includeValue) message.value = clone(value);
    send(message);
  });
  const api = Object.freeze({
    onLoad(handler) {
      if (typeof handler !== "function") throw new TypeError("handler must be a function");
      loadHandler = handler;
    },
    on(action, handler) {
      action = actionName(action);
      if (typeof handler !== "function") throw new TypeError("handler must be a function");
      handlers.set(action, handler);
    },
    data() { return clone(durableData); },
    render(html, css = "") {
      if (typeof html !== "string" || typeof css !== "string") {
        throw new TypeError("render content must be strings");
      }
      if (encodeText(html).length > maxRenderHtmlBytes || encodeText(css).length > maxRenderCssBytes) {
        throw new RangeError("render content exceeds its encoded size limit");
      }
      send({ type: "render", html, css });
    },
    set(path, value) { return mutation("set", path, value, true); },
    delete(path) { return mutation("delete", path, undefined, false); },
    append(path, value) { return mutation("append", path, value, true); },
    askAgent(message) { send({ type: "agent-request", message }); },
    notify(message, level = "info") { send({ type: "notify", message, level }); },
  });
  Object.defineProperty(globalThis, "app", {
    value: api, writable: false, configurable: false, enumerable: true,
  });
  Object.defineProperty(globalThis, "postMessage", {
    value: denied, writable: false, configurable: false, enumerable: false,
  });

  globalThis.addEventListener("message", event => {
    const message = event.data;
    if (!message || typeof message !== "object") return;
    if (message.type === "init") {
      durableData = clone(message.data);
      resolvePromise()
        .then(() => message.load && loadHandler ? loadHandler() : undefined)
        .then(() => send({ type: "initialized" }))
        .catch(() => send({ type: "initialization-error" }));
      return;
    }
    if (message.type === "data-result") {
      const waiter = pending.get(message.request_id);
      if (!waiter) return;
      pending.delete(message.request_id);
      if (message.ok) {
        durableData = clone(message.data);
        waiter.resolve(clone(durableData));
      } else {
        waiter.reject(new Error("Data update failed"));
      }
      return;
    }
    if (message.type === "event") {
      const handler = handlers.get(message.action);
      resolvePromise(handler ? handler(clone(message.event)) : undefined)
        .then(() => send({ type: "turn-complete", turn_id: message.turn_id }))
        .catch(() => send({ type: "turn-error", turn_id: message.turn_id }));
    }
  });
  send({ type: "ready" });
}

const allowedElements = new Set([
  "ABBR", "ADDRESS", "ARTICLE", "ASIDE", "BDI", "BDO", "BLOCKQUOTE", "BR",
  "BUTTON", "CAPTION", "CITE", "CODE", "DATA", "DATALIST", "DD", "DEL",
  "DETAILS", "DFN", "DIV", "DL", "DT", "EM", "FIELDSET", "FIGCAPTION",
  "FIGURE", "FOOTER", "FORM", "H1", "H2", "H3", "H4", "H5", "H6", "HEADER",
  "HR", "I", "INPUT", "INS", "KBD", "LABEL", "LEGEND", "LI", "MAIN", "MARK",
  "MENU", "METER", "NAV", "OL", "OPTGROUP", "OPTION", "OUTPUT", "P", "PRE",
  "PROGRESS", "Q", "RP", "RT", "RUBY", "S", "SAMP", "SEARCH", "SECTION",
  "SELECT", "SMALL", "SPAN", "STRONG", "SUB", "SUMMARY", "SUP", "TABLE",
  "TBODY", "TD", "TEXTAREA", "TFOOT", "TH", "THEAD", "TIME", "TR", "U", "UL",
  "VAR", "WBR",
]);
const droppedElements = new Set([
  "A", "AUDIO", "BASE", "EMBED", "IFRAME", "IMG", "LINK", "META", "OBJECT",
  "PICTURE", "SCRIPT", "SOURCE", "STYLE", "TRACK", "VIDEO",
]);
const globalAttributes = new Set([
  "id", "class", "title", "hidden", "role", "lang", "spellcheck",
]);
const safeAttributes = new Set([
  "abbr", "checked", "cols", "datetime", "disabled", "for", "headers", "high",
  "inputmode", "label", "list", "low", "max", "maxlength", "min", "minlength",
  "multiple", "name", "open", "optimum", "pattern", "placeholder", "readonly",
  "required", "reversed", "rows", "scope", "selected", "size", "start", "step",
  "value", "wrap",
]);
const allowedInputTypes = new Set([
  "checkbox", "color", "date", "datetime-local", "email", "month", "number", "radio",
  "range", "search", "tel", "text", "time", "week",
]);
const allowedCssProperties = new Set(`
  accent-color align-content align-items align-self animation animation-delay
  animation-direction animation-duration animation-fill-mode animation-iteration-count
  animation-name animation-play-state animation-timing-function appearance aspect-ratio
  backdrop-filter background background-attachment background-blend-mode background-clip
  background-color background-image background-origin background-position
  background-position-x background-position-y background-repeat background-size block-size
  border border-block border-block-color border-block-end
  border-block-start border-bottom border-bottom-color border-bottom-left-radius
  border-bottom-right-radius border-bottom-style border-bottom-width border-collapse
  border-color border-inline border-inline-color border-inline-end border-inline-start
  border-left border-left-color border-left-style border-left-width border-radius border-right
  border-right-color border-right-style border-right-width border-spacing border-style
  border-top border-top-color border-top-left-radius border-top-right-radius border-top-style
  border-top-width border-width bottom box-shadow box-sizing caret-color clear clip clip-path
  color color-scheme column-gap columns content counter-increment counter-reset counter-set cursor
  direction display filter flex flex-basis flex-direction flex-flow flex-grow flex-shrink
  flex-wrap float font-family font-feature-settings font-kerning font-optical-sizing font-size
  font-stretch font-style font-variant font-variation-settings font-weight gap
  grid grid-area grid-auto-columns grid-auto-flow grid-auto-rows grid-column grid-column-end
  grid-column-gap grid-column-start grid-gap grid-row grid-row-end grid-row-gap grid-row-start
  grid-template grid-template-areas grid-template-columns grid-template-rows height hyphens
  inline-size inset inset-block inset-block-end inset-block-start inset-inline inset-inline-end
  inset-inline-start isolation justify-content justify-items justify-self left letter-spacing
  line-height list-style list-style-position list-style-type margin margin-block margin-block-end
  margin-block-start margin-bottom margin-inline margin-inline-end margin-inline-start margin-left
  margin-right margin-top max-block-size max-height max-inline-size max-width min-block-size
  min-height min-inline-size min-width mix-blend-mode object-fit object-position opacity order
  outline outline-color outline-offset outline-style outline-width overflow overflow-anchor
  overflow-clip-margin overflow-wrap overflow-x overflow-y overscroll-behavior
  overscroll-behavior-block overscroll-behavior-inline overscroll-behavior-x
  overscroll-behavior-y padding padding-block
  padding-block-end padding-block-start padding-bottom padding-inline padding-inline-end
  padding-inline-start padding-left padding-right padding-top place-content place-items place-self
  pointer-events position quotes resize right rotate row-gap scale scroll-behavior scroll-margin
  scroll-margin-block scroll-margin-block-end scroll-margin-block-start scroll-margin-bottom
  scroll-margin-inline scroll-margin-inline-end scroll-margin-inline-start scroll-margin-left
  scroll-margin-right scroll-margin-top scroll-padding scroll-padding-block scroll-padding-block-end
  scroll-padding-block-start scroll-padding-bottom scroll-padding-inline scroll-padding-inline-end
  scroll-padding-inline-start scroll-padding-left scroll-padding-right scroll-padding-top
  scroll-snap-align scroll-snap-stop scroll-snap-type scrollbar-color scrollbar-gutter
  scrollbar-width tab-size table-layout text-align text-decoration text-decoration-color
  text-decoration-line text-decoration-style text-decoration-thickness text-emphasis
  text-emphasis-color text-emphasis-position text-emphasis-style text-indent text-overflow
  text-rendering text-shadow text-transform text-underline-offset text-wrap text-wrap-mode
  text-wrap-style top
  touch-action transform transform-origin transition transition-delay transition-duration
  transition-property transition-timing-function translate user-select vertical-align visibility
  white-space white-space-collapse width word-break word-spacing word-wrap writing-mode z-index
`.trim().split(/\s+/));
const safeCustomProperty = /^--[A-Za-z][A-Za-z0-9_-]{0,63}$/;
const forbiddenCssValue = /url\s*\(|\bimage\s*\(|image-set\s*\(|cross-fade\s*\(|element\s*\(|paint\s*\(|src\s*\(/i;

function sanitizeHtml(html) {
  if (typeof html !== "string" || textEncoder.encode(html).length > MAX_RENDER_HTML_BYTES) {
    throw new Error("Generated HTML is invalid or too large");
  }
  const template = document.createElement("template");
  template.innerHTML = html;
  const output = document.createDocumentFragment();
  for (const node of template.content.childNodes) cloneSafeNode(node, output);
  return output;
}

function cloneSafeNode(node, parent) {
  if (node.nodeType === Node.TEXT_NODE) {
    parent.append(document.createTextNode(node.data));
    return;
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return;
  if (droppedElements.has(node.tagName)) return;
  if (!allowedElements.has(node.tagName)) {
    for (const child of node.childNodes) cloneSafeNode(child, parent);
    return;
  }
  const clean = document.createElement(node.tagName.toLowerCase());
  for (const attribute of node.attributes) copySafeAttribute(node, clean, attribute.name, attribute.value);
  if (node.tagName === "BUTTON") clean.type = "button";
  if (node.tagName === "INPUT") {
    const type = node.getAttribute("type") || "text";
    clean.type = allowedInputTypes.has(type.toLowerCase()) ? type.toLowerCase() : "text";
  }
  for (const child of node.childNodes) cloneSafeNode(child, clean);
  parent.append(clean);
}

function copySafeAttribute(source, target, name, value) {
  const lower = name.toLowerCase();
  if (globalAttributes.has(lower)) {
    target.setAttribute(lower, value.slice(0, 512));
    return;
  }
  if (lower.startsWith("aria-") && /^[a-z-]{1,40}$/.test(lower)) {
    target.setAttribute(lower, value.slice(0, 512));
    return;
  }
  if (lower === "dir" && ["auto", "ltr", "rtl"].includes(value.toLowerCase())) {
    target.setAttribute(lower, value.toLowerCase());
    return;
  }
  if (lower === "tabindex" && ["-1", "0"].includes(value)) {
    target.setAttribute(lower, value);
    return;
  }
  if (lower === "data-action" && /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(value)) {
    target.setAttribute(lower, value);
    return;
  }
  if (lower === "data-field" && /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(value)) {
    target.setAttribute(lower, value);
    return;
  }
  if (safeAttributes.has(lower)) target.setAttribute(lower, value.slice(0, 512));
  if ((lower === "colspan" || lower === "rowspan") && /^[1-9][0-9]?$/.test(value)) {
    target.setAttribute(lower, value);
  }
}

function sanitizeCss(css) {
  if (typeof css !== "string" || textEncoder.encode(css).length > MAX_RENDER_CSS_BYTES) {
    throw new Error("Generated CSS is invalid or too large");
  }
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(css);
  return Array.from(sheet.cssRules, sanitizeRule).filter(Boolean).join("\n");
}

function sanitizeRule(rule) {
  const kind = rule.constructor.name;
  if (kind === "CSSStyleRule") {
    // Shadow CSS can otherwise restyle its host and escape the generated
    // canvas. Reject escapes too, so an encoded :host cannot bypass the check.
    if (rule.selectorText.includes("\\") || /:host(?:-context)?(?:\b|\()/i.test(rule.selectorText)) return "";
    return `${rule.selectorText}{${sanitizeDeclarations(rule.style)}}`;
  }
  if (kind === "CSSMediaRule") {
    if (textEncoder.encode(rule.conditionText).length > MAX_CSS_CONDITION_BYTES) return "";
    return `@media ${rule.conditionText}{${Array.from(rule.cssRules, sanitizeRule).filter(Boolean).join("")}}`;
  }
  if (kind === "CSSKeyframesRule") {
    const name = /^[A-Za-z_][A-Za-z0-9_-]{0,63}$/.test(rule.name) ? rule.name : "generated";
    return `@keyframes ${name}{${Array.from(rule.cssRules, child => `${child.keyText}{${sanitizeDeclarations(child.style)}}`).join("")}}`;
  }
  return "";
}

function sanitizeDeclarations(style) {
  const safe = [];
  for (const property of style) {
    const normalized = property.toLowerCase();
    if (!allowedCssProperties.has(normalized) && !safeCustomProperty.test(property)) continue;
    const value = style.getPropertyValue(property);
    if (textEncoder.encode(value).length > 4096 || value.includes("\\") || forbiddenCssValue.test(value)) continue;
    const priority = style.getPropertyPriority(property) === "important" ? "!important" : "";
    safe.push(`${normalized}:${value}${priority}`);
  }
  return safe.join(";");
}

function renderGenerated(html, css) {
  const fragment = sanitizeHtml(html);
  const safeCss = sanitizeCss(css);
  const host = $("generated-host");
  if (!generatedRoot) generatedRoot = host.attachShadow({ mode: "open" });
  generatedRoot.replaceChildren();
  const style = document.createElement("style");
  style.textContent = `:host{display:block;min-height:100%;color:var(--text);background:var(--bg);font-family:system-ui,sans-serif}${safeCss}`;
  generatedRoot.append(style, fragment);
  host.hidden = false;
  $("empty-state").hidden = true;
}

function clearGenerated() {
  generatedRoot.replaceChildren();
  $("generated-host").hidden = true;
  $("empty-state").hidden = false;
}

function syncEmptyState() {
  const firstRun = !snapshot.session;
  $("first-run-how").hidden = !firstRun;
  $("first-run-guidance").hidden = !firstRun;
  $("empty-title").textContent = firstRun ? "Build your personal app" : "Your app will appear here";
  $("empty-description").textContent = firstRun
    ? "Open agent chat and describe what you want. The agent can create the interface, behavior, and structured data."
    : "Open agent chat to continue building or ask the agent to create the first version.";
  $("empty-open-chat").textContent = firstRun ? "Start building" : "Open agent chat";
}

function eventPayload(element) {
  const fields = Object.create(null);
  for (const field of Array.from(generatedRoot.querySelectorAll("[data-field]")).slice(0, MAX_EVENT_FIELDS)) {
    const key = field.dataset.field;
    if (field.type === "checkbox" || field.type === "radio") fields[key] = Boolean(field.checked);
    else fields[key] = clipEncodedText(String(field.value || ""), MAX_EVENT_FIELD_BYTES);
  }
  const payload = {
    action: element.dataset.action,
    value: "value" in element ? clipEncodedText(String(element.value || ""), MAX_EVENT_FIELD_BYTES) : "",
    checked: "checked" in element ? Boolean(element.checked) : false,
    fields,
  };
  if (jsonByteLength(payload) > MAX_EVENT_PAYLOAD_BYTES) {
    return { action: element.dataset.action, value: "", checked: false, fields: {} };
  }
  return payload;
}

function clipEncodedText(value, limit) {
  const encoded = textEncoder.encode(value);
  if (encoded.length <= limit) return value;
  let clipped = textDecoder.decode(encoded.slice(0, limit));
  while (textEncoder.encode(clipped).length > limit) clipped = clipped.slice(0, -1);
  return clipped;
}

function jsonByteLength(value) {
  try {
    const encoded = JSON.stringify(value);
    return encoded === undefined ? -1 : textEncoder.encode(encoded).length;
  } catch (_error) {
    return -1;
  }
}

function generatedInteraction(event) {
  if (!(event.target instanceof Element)) return;
  const changeControl = event.target.closest("input, select, textarea");
  if ((event.type === "click" && changeControl) || (event.type === "change" && !changeControl)) return;
  const target = event.target.closest("[data-action]");
  if (!target || !generatedRoot.contains(target)) return;
  event.preventDefault();
  if (workerRun) {
    showRuntimeStatus("Finishing the previous app action");
    return;
  }
  runCapabilityWorker({ action: target.dataset.action, event: eventPayload(target) });
}

async function runCapabilityWorker(pendingEvent = null) {
  if (!snapshot.app || !snapshot.app.javascript) return;
  if (workerRun) workerRun.finish("restarted");
  const source = (
    `(${capabilityWorkerBootstrap.toString()})(${MAX_RENDER_HTML_BYTES},${MAX_RENDER_CSS_BYTES});\n`
    + `${snapshot.app.javascript}\n`
  );
  const url = URL.createObjectURL(new Blob([source], { type: "application/javascript" }));
  const worker = new Worker(url);
  URL.revokeObjectURL(url);
  const run = {
    worker,
    state: "starting",
    event: pendingEvent,
    count: 0,
    totalMessages: 0,
    mutations: 0,
    mutationPending: false,
    agentRequested: false,
    windowStarted: performance.now(),
    timer: null,
    finish(reason) {
      clearTimeout(this.timer);
      worker.terminate();
      if (workerRun === this) workerRun = null;
      if (reason === "timeout" || reason === "error") showRuntimeStatus("Generated behavior stopped safely", "error");
    },
  };
  workerRun = run;
  run.timer = setTimeout(() => run.finish("timeout"), WORKER_TURN_TIMEOUT_MS);
  worker.addEventListener("error", event => {
    event.preventDefault();
    run.finish("error");
  });
  worker.addEventListener("message", event => handleWorkerMessage(run, event.data));
}

async function handleWorkerMessage(run, message) {
  if (workerRun !== run || !message || typeof message !== "object") return;
  const now = performance.now();
  if (now - run.windowStarted >= 1000) {
    run.windowStarted = now;
    run.count = 0;
  }
  if (++run.count > MAX_WORKER_MESSAGES_PER_SECOND || ++run.totalMessages > MAX_WORKER_MESSAGES_PER_TURN) {
    run.finish("error");
    return;
  }
  if (message.type === "ready" && run.state === "starting") {
    run.state = "initializing";
    run.worker.postMessage({ type: "init", data: snapshot.app.data, load: !run.event });
    return;
  }
  if (message.type === "initialization-error" && run.state === "initializing") {
    run.finish("error");
    return;
  }
  if (message.type === "initialized" && run.state === "initializing") {
    if (!run.event) {
      run.finish("complete");
      return;
    }
    run.state = "event";
    run.worker.postMessage({
      type: "event", action: run.event.action, event: run.event.event, turn_id: "turn",
    });
    return;
  }
  if ((message.type === "turn-complete" || message.type === "turn-error") && run.state === "event" && message.turn_id === "turn") {
    run.finish(message.type === "turn-complete" ? "complete" : "error");
    return;
  }
  if (message.type === "render" && typeof message.html === "string" && typeof message.css === "string") {
    try { renderGenerated(message.html, message.css); }
    catch (_error) { run.finish("error"); }
    return;
  }
  if (message.type === "notify") {
    if (typeof message.message !== "string" || textEncoder.encode(message.message).length > 1000) return;
    if (!["info", "success", "error"].includes(message.level)) return;
    showRuntimeStatus(message.message, message.level);
    return;
  }
  if (message.type === "agent-request") {
    if (
      run.state !== "event" || run.agentRequested || typeof message.message !== "string"
      || !message.message.trim()
      || textEncoder.encode(message.message).length > MAX_AGENT_MESSAGE_BYTES
    ) return;
    run.agentRequested = true;
    void sendMessage(message.message.trim());
    return;
  }
  if (message.type === "data-action") await handleWorkerDataAction(run, message);
}

async function handleWorkerDataAction(run, message) {
  if (
    run.state !== "event" || run.mutationPending
    || run.mutations >= MAX_WORKER_MUTATIONS_PER_TURN
    || typeof message.request_id !== "string"
    || !/^mutation-[1-9][0-9]{0,8}$/.test(message.request_id)
    || !["set", "delete", "append"].includes(message.action)
    || !validDataPath(message.path)
    || (message.action !== "delete" && jsonByteLength(message.value) > MAX_DATA_VALUE_BYTES)
    || (message.action !== "delete" && jsonByteLength(message.value) < 0)
  ) {
    run.finish("error");
    return;
  }
  run.mutationPending = true;
  run.mutations += 1;
  const body = {
    action: message.action,
    expected_revision: snapshot.app.revision,
    path: message.path,
  };
  if (message.action !== "delete") body.value = message.value;
  try {
    const response = await api("POST", "/runtime/actions", body);
    if (workerRun !== run) {
      await refreshSnapshot();
      return;
    }
    snapshot.app = response.app;
    renderedRevision = response.app.revision;
    $("revision-label").textContent = `Revision ${response.app.revision}`;
    run.mutationPending = false;
    run.worker.postMessage({ type: "data-result", request_id: message.request_id, ok: true, data: response.app.data });
  } catch (_error) {
    if (workerRun !== run) return;
    run.worker.postMessage({ type: "data-result", request_id: message.request_id, ok: false });
    await refreshSnapshot();
  }
}

function validDataPath(path) {
  if (!Array.isArray(path) || !path.length || path.length > 16) return false;
  return path.every(segment => (
    Number.isInteger(segment) && segment >= 0
  ) || (
    typeof segment === "string" && segment.length > 0
    && textEncoder.encode(segment).length <= 128
  ));
}

function showRuntimeStatus(message, level = "info") {
  const status = $("runtime-status");
  status.textContent = message;
  status.className = `runtime-status ${level}`;
  status.hidden = false;
  setTimeout(() => { if (status.textContent === message) status.hidden = true; }, 4500);
}

function openChat() {
  $("chat-drawer").hidden = false;
  $("open-chat").setAttribute("aria-expanded", "true");
  $("message").focus();
}

function closeChat() {
  $("chat-drawer").hidden = true;
  $("open-chat").setAttribute("aria-expanded", "false");
}

function showChatStatus(message, error = false) {
  const status = $("chat-status");
  status.textContent = message;
  status.className = error ? "chat-status error" : "chat-status";
  status.hidden = !message;
}

async function sendMessage(forcedMessage = null) {
  const fromGeneratedApp = forcedMessage !== null;
  const message = (forcedMessage || $("message").value).trim();
  if (!message) return;
  if (messageBusy) {
    if (fromGeneratedApp) showRuntimeStatus("Agent is already starting");
    return;
  }
  messageBusy = true;
  const body = { content: message };
  if (!snapshot.session) {
    body.agent_runtime = $("runtime").value;
    body.model = $("model").value;
    body.effort = $("effort").value;
  }
  $("send-message").disabled = true;
  showChatStatus("Starting agent…");
  if (fromGeneratedApp) showRuntimeStatus("Starting agent…");
  try {
    await api("POST", fromGeneratedApp ? "/runtime/agent-requests" : "/messages", body);
    if (!fromGeneratedApp) $("message").value = "";
    await refreshSnapshot();
    showChatStatus("");
    if (fromGeneratedApp) showRuntimeStatus("Agent started", "success");
  } catch (error) {
    showChatStatus(error.message || "Could not start the agent", true);
    if (fromGeneratedApp) showRuntimeStatus("Could not start the agent", "error");
  } finally {
    messageBusy = false;
    $("send-message").disabled = false;
  }
}

function renderChat() {
  const history = $("chat-history");
  history.replaceChildren();
  const ordered = snapshot.tasks.slice().sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  if (!ordered.length) {
    const empty = document.createElement("p");
    empty.className = "chat-empty";
    empty.textContent = "Describe the personal app you want. The agent can build its UI, behavior, and data, then keep changing it here.";
    history.append(empty);
  }
  for (const task of ordered) {
    const turn = document.createElement("article");
    turn.className = "chat-turn";
    const user = document.createElement("div");
    user.className = "chat-user";
    user.textContent = task.input_message || "";
    turn.append(user);
    if (task.output_message) {
      const agent = document.createElement("div");
      agent.className = "chat-agent";
      agent.textContent = task.output_message;
      turn.append(agent);
    }
    if (task.error_message) {
      const error = document.createElement("div");
      error.className = "chat-error";
      error.textContent = task.error_message;
      turn.append(error);
    }
    const meta = document.createElement("div");
    meta.className = "chat-task-meta";
    meta.append(document.createTextNode(`${task.status || "unknown"} · ${task.task_id || "task"}`));
    if (task.status === "queued" || task.status === "running") {
      const stop = document.createElement("button");
      stop.className = task.status === "running" ? "danger ghost" : "ghost";
      stop.textContent = task.status === "running" ? "Stop" : "Cancel";
      stop.addEventListener("click", async () => {
        await api("POST", `/tasks/${encodeURIComponent(task.task_id)}/${task.status === "running" ? "kill" : "cancel"}`, {});
        await refreshSnapshot();
      });
      meta.append(stop);
    }
    turn.append(meta);
    history.append(turn);
  }
  syncAgentSettings(snapshot.session);
  if (!history.matches(":hover")) history.scrollTop = history.scrollHeight;
}

function setSessionOptions() {
  const runtimeSelect = $("runtime");
  const modelSelect = $("model");
  const effortSelect = $("effort");
  const runtime = establishedSession?.agent_runtime || runtimeSelect.value;
  runtimeSelect.value = runtime;
  const models = sessionOptions[runtime] || {};
  const currentModel = establishedSession?.model || modelSelect.value;
  const modelValues = Object.keys(models);
  if (establishedSession && currentModel && !modelValues.includes(currentModel)) modelValues.push(currentModel);
  modelSelect.replaceChildren(...modelValues.map(value => new Option(value, value)));
  if (modelValues.includes(currentModel)) modelSelect.value = currentModel;
  const efforts = [...(models[modelSelect.value] || [])];
  const currentEffort = establishedSession?.effort || effortSelect.value;
  if (establishedSession && currentEffort && !efforts.includes(currentEffort)) efforts.push(currentEffort);
  effortSelect.replaceChildren(...efforts.map(value => new Option(value, value)));
  if (efforts.includes(currentEffort)) effortSelect.value = currentEffort;
  const locked = Boolean(establishedSession);
  const settingRows = [
    [runtimeSelect, $("runtime-fixed"), runtimeSelect.selectedOptions[0]?.textContent || runtimeSelect.value],
    [modelSelect, $("model-fixed"), modelSelect.selectedOptions[0]?.textContent || modelSelect.value],
    [effortSelect, $("effort-fixed"), effortSelect.value ? `${effortSelect.value[0].toUpperCase()}${effortSelect.value.slice(1)}` : ""],
  ];
  for (const [select, value, text] of settingRows) {
    select.disabled = locked;
    select.hidden = locked;
    value.hidden = !locked;
    value.textContent = text;
  }
  $("agent-settings").classList.toggle("locked", locked);
  $("agent-settings-help-text").textContent = locked
    ? "Agent, Model, and Level are fixed for this session and cannot be changed in this app version."
    : "Choose Agent, Model, and Level before your first message. After that, they are fixed and cannot be changed in this app version.";
  $("send-message").disabled = !locked && (!modelSelect.value || !effortSelect.value);
}

function setRuntimeOptions() {
  const labels = { codex: "Codex", claude_code: "Claude Code", hermes: "Hermes" };
  const current = establishedSession?.agent_runtime || $("runtime").value;
  const runtimes = Object.keys(sessionOptions);
  if (current && !runtimes.includes(current)) runtimes.push(current);
  $("runtime").replaceChildren(...runtimes.map(
    value => new Option(labels[value] || value, value)
  ));
  if (runtimes.includes(current)) $("runtime").value = current;
}

function syncAgentSettings(task) {
  const next = task ? {
    agent_runtime: String(task.agent_runtime || ""),
    model: String(task.model || ""),
    effort: String(task.effort || ""),
  } : null;
  const key = next ? `${next.agent_runtime}\0${next.model}\0${next.effort}` : "open";
  if (key === establishedSessionKey) return;
  establishedSession = next;
  establishedSessionKey = key;
  setRuntimeOptions();
  setSessionOptions();
}

async function refreshSnapshot() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    const [stateResponse, conversationResponse] = await Promise.all([
      api("GET", "/state"),
      api("GET", "/conversation"),
    ]);
    const next = {
      app: stateResponse.app,
      tasks: conversationResponse.tasks || [],
      session: conversationResponse.session || null,
    };
    if (snapshot.app && next.app.revision < snapshot.app.revision) next.app = snapshot.app;
    snapshot = next;
    if (next.app.revision !== renderedRevision) {
      renderedRevision = next.app.revision;
      $("revision-label").textContent = next.app.revision ? `Revision ${next.app.revision}` : "Empty app";
      if (next.app.html || next.app.css || next.app.javascript) {
        renderGenerated(next.app.html, next.app.css);
        runCapabilityWorker();
      } else clearGenerated();
    }
    renderChat();
    syncEmptyState();
  } catch (_error) {
    showRuntimeStatus("Builder backend unavailable", "error");
  } finally {
    pollBusy = false;
  }
}

async function initialize() {
  generatedRoot = $("generated-host").attachShadow({ mode: "open" });
  generatedRoot.addEventListener("click", generatedInteraction);
  generatedRoot.addEventListener("change", generatedInteraction);
  generatedRoot.addEventListener("submit", event => event.preventDefault());
  try {
    const options = await api("GET", "/session-options");
    sessionOptions = options.session_options || {};
    setRuntimeOptions();
    setSessionOptions();
  } catch (_error) {
    showRuntimeStatus("Agent settings are unavailable", "error");
  }
  await refreshSnapshot();
  setInterval(refreshSnapshot, 3000);
}

$("open-chat").addEventListener("click", () => $("chat-drawer").hidden ? openChat() : closeChat());
$("empty-open-chat").addEventListener("click", openChat);
$("close-chat").addEventListener("click", closeChat);
$("runtime").addEventListener("change", setSessionOptions);
$("model").addEventListener("change", setSessionOptions);
$("send-message").addEventListener("click", () => sendMessage());
$("message").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
});
initialize();
