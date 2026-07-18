// Full operator documentation for every managed integration, bundled tool,
// and custom domain access. Managed content comes from integration_catalog;
// bundled-tool content comes from the same manifests that drive execution.

import { api } from "./api.js";
import { $, esc, inlineCode, setHtml } from "./helpers.js";
import { CUSTOM_DOMAIN_GUIDE, MANAGED_INTEGRATIONS } from "./integration_catalog.js";

let selectedGuideId = "openai";
let loadedGuides = [];
let copyFeedbackTimer = null;
let copyFeedbackGeneration = 0;

function hideCallbackCopyFeedback() {
  if (copyFeedbackTimer) clearTimeout(copyFeedbackTimer);
  copyFeedbackTimer = null;
  for (const feedback of document.querySelectorAll("[data-callback-copy-feedback]")) {
    feedback.hidden = true;
  }
}

export function dismissCallbackCopyFeedback() {
  copyFeedbackGeneration += 1;
  hideCallbackCopyFeedback();
}

function legacyCopy(value) {
  const input = document.createElement("textarea");
  input.value = value;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.append(input);
  input.select();
  const copied = document.execCommand("copy");
  input.remove();
  if (!copied) throw new Error("Copy failed");
}

export async function copyCallbackUri(button) {
  const value = button.dataset.copyValue || "";
  const feedback = button.parentElement?.querySelector("[data-callback-copy-feedback]");
  const generation = ++copyFeedbackGeneration;
  hideCallbackCopyFeedback();
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else {
      legacyCopy(value);
    }
    if (generation !== copyFeedbackGeneration) return;
    if (feedback) {
      feedback.textContent = "Copied";
      feedback.hidden = false;
    }
    copyFeedbackTimer = setTimeout(dismissCallbackCopyFeedback, 2500);
  } catch (_error) {
    if (generation !== copyFeedbackGeneration) return;
    if (feedback) {
      feedback.textContent = "Copy failed";
      feedback.hidden = false;
      copyFeedbackTimer = setTimeout(dismissCallbackCopyFeedback, 2500);
    }
  }
}

function toolGuide(tool) {
  const oauth = tool.connection === "oauth";
  return {
    id: `tool:${tool.tool_id}`,
    label: tool.display_name,
    summary: tool.description,
    callbackUrl: oauth ? `${location.origin}/oauth/callback` : "",
    protections: Array.isArray(tool.protections) ? tool.protections : [],
    technicalDetails: Array.isArray(tool.technical_details) ? tool.technical_details : [],
    setupSteps: (tool.setup_steps || []).map(step => ({
      title: step.title,
      description: step.description,
      linkUrl: step.link_url,
      linkLabel: step.link_label,
      imagePath: step.image_path,
      imageAlt: step.image_alt,
      showCallback: step.show_callback,
      showConfig: step.show_config,
    })),
    capabilities: (tool.actions || []).map(action => ({
      name: action.id,
      codeName: true,
      description: action.description,
      approval: action.approval,
    })),
    dataSummary: {
      items: tool.data_summary.cards.map(card => ({
        title: card.title,
        description: card.description,
        points: card.points,
        links: card.links.map(link => ({ label: link.label, url: link.url })),
      })),
    },
    config: tool.config || [],
    networkScope: [],
  };
}

function allGuides(tools) {
  const managed = Object.entries(MANAGED_INTEGRATIONS).map(([id, guide]) => ({ id, ...guide }));
  const bundled = tools.map(toolGuide);
  return [...managed, ...bundled, CUSTOM_DOMAIN_GUIDE]
    .sort((left, right) => left.label.localeCompare(right.label, undefined, { sensitivity: "base" }));
}

export async function refreshConnectionGuide() {
  try {
    const response = await api("GET", "/v1/tools");
    const tools = Array.isArray(response.tools) ? response.tools : [];
    loadedGuides = allGuides(tools);
    if (!loadedGuides.some(guide => guide.id === selectedGuideId)) {
      selectedGuideId = loadedGuides[0]?.id || "";
    }
    renderConnectionGuide();
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    setHtml($("connection-guide-content"), `<div class="empty-state">Could not load the bundled tools: ${esc(message)}</div>`);
    throw error;
  }
}

export function openConnectionGuide(guideId) {
  if (guideId) selectedGuideId = guideId;
  if (loadedGuides.some(guide => guide.id === selectedGuideId)) renderConnectionGuide();
}

function renderConnectionGuide() {
  const selected = loadedGuides.find(guide => guide.id === selectedGuideId) || loadedGuides[0];
  setHtml($("connection-guide-select"), loadedGuides.map(guide => `
    <option value="${esc(guide.id)}" ${guide.id === selected?.id ? "selected" : ""}>${esc(guide.label)}</option>
  `).join(""));
  setHtml($("connection-guide-index"), loadedGuides.map(guide => `
    <button class="${guide.id === selected?.id ? "selected" : ""}" data-action="jump-connection-guide" data-guide="${esc(guide.id)}" ${guide.id === selected?.id ? 'aria-current="true"' : ""}>${esc(guide.label)}</button>
  `).join(""));
  const content = $("connection-guide-content");
  setHtml(content, selected ? renderGuide(selected) : '<div class="empty-state">No integration guides are available.</div>');
  content.scrollTop = 0;
}

function renderGuide(guide) {
  const connectionKind = guide.id.startsWith("tool:")
    ? "Bundled MCP tool"
    : guide.id === "custom_domain"
      ? "Custom rule"
      : "Direct network integration";
  return `
    <article class="connection-guide-entry" data-guide-section="${esc(guide.id)}" tabindex="-1">
      <header>
        <span class="guide-kind">${esc(connectionKind)}</span>
        <h2>${esc(guide.label)}</h2>
        <p class="guide-lead">${esc(guide.summary)}</p>
      </header>
      <section class="guide-section">
        <h3>What it enables</h3>
        <div class="guide-capabilities">${guide.capabilities.map(renderCapability).join("")}</div>
      </section>
      <section class="guide-section">
        <h3>Connection</h3>
        ${renderSetup(guide)}
      </section>
      ${renderDataSummary(guide.dataSummary)}
      ${renderTechnicalDetails(guide)}
    </article>`;
}

function renderConfig(config) {
  if (!config || !config.length) return "";
  return `<div class="guide-config">
    <h4>Configuration values</h4>
    ${config.map(entry => `
      <div><code>${esc(entry.key)}</code><span>${esc(entry.description)}</span></div>
    `).join("")}
  </div>`;
}

function renderPolicyPoints(points) {
  if (!points || !points.length) return "";
  return `<div class="guide-policy-points">${points.map(point => `
    <div class="guide-policy-point"><span>${esc(point.label)}</span><p>${esc(point.text)}</p></div>`).join("")}
  </div>`;
}

function setupLinkIsInline(step) {
  return Boolean(step.linkUrl && step.linkLabel && String(step.description || "").includes(step.linkLabel));
}

function renderSetupDescription(step) {
  const description = String(step.description || "");
  if (!setupLinkIsInline(step)) return inlineCode(description);
  const index = description.indexOf(step.linkLabel);
  const before = description.slice(0, index);
  const after = description.slice(index + step.linkLabel.length);
  const link = `<a href="${esc(step.linkUrl)}" target="_blank" rel="noopener noreferrer">${esc(step.linkLabel)}</a>`;
  return `${inlineCode(before)}${link}${inlineCode(after)}`;
}

// The callback URI and configuration keys render inside the step that needs
// them, so the operator sees each value at the moment the provider asks for it.
function renderSetup(guide) {
  const steps = guide.setupSteps;
  if (!steps || !steps.length) return "";
  return `
    <ol class="guide-steps">${steps.map(step => `
      <li>
        <div class="guide-step-copy">
          <h4>${esc(step.title)}</h4>
          <p>${renderSetupDescription(step)}</p>
          ${step.showCallback && guide.callbackUrl ? `<div class="guide-callback">
            <span class="guide-callback-label">Callback URI for this host</span>
            <div class="guide-callback-value">
              <code>${esc(guide.callbackUrl)}</code>
              <button class="guide-copy-button" data-action="copy-callback-uri" data-copy-value="${esc(guide.callbackUrl)}" aria-label="Copy callback URI" title="Copy callback URI">
                <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="6.5" y="6.5" width="8" height="9" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M5 13.5H4.5A1.5 1.5 0 0 1 3 12V4.5A1.5 1.5 0 0 1 4.5 3H11a1.5 1.5 0 0 1 1.5 1.5V5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
              </button>
              <span class="guide-copy-feedback" data-callback-copy-feedback role="status" hidden>Copied</span>
            </div>
          </div>` : ""}
          ${step.showConfig ? renderConfig(guide.config) : ""}
          ${step.linkUrl && !setupLinkIsInline(step) ? `<a href="${esc(step.linkUrl)}" target="_blank" rel="noopener noreferrer">${esc(step.linkLabel)}</a>` : ""}
        </div>
        ${step.imagePath ? `<figure><img src="${esc(step.imagePath)}" alt="${esc(step.imageAlt)}" loading="lazy"></figure>` : ""}
      </li>`).join("")}
    </ol>`;
}

function renderCapability(capability) {
  const approval = capability.approval === "operator"
    ? `<span class="status awaiting_login">approval required</span>`
    : capability.approval === "direct"
      ? `<span class="status active">runs directly</span>`
      : "";
  return `
    <div class="guide-capability">
      <div class="guide-capability-head"><h4>${capability.codeName ? `<code>${esc(capability.name)}</code>` : esc(capability.name)}</h4>${approval}</div>
      <p>${esc(capability.description)}</p>
      ${capability.linkUrl ? `<a href="${esc(capability.linkUrl)}" target="_blank" rel="noopener noreferrer">${esc(capability.linkLabel)}</a>` : ""}
    </div>`;
}

function renderDataSummary(summary) {
  return `
    <section class="guide-section guide-data-section">
      <h3>What happens to your data</h3>
      <div class="guide-data-summary">${summary.items.map(item => `
        <article>
          <h4>${esc(item.title)}</h4>
          ${item.description ? `<p>${esc(item.description)}</p>` : ""}
          ${renderPolicyPoints(item.points)}
          ${item.links.length ? `<div class="guide-data-summary-links">${item.links.map(link => `
            <a href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)}</a>`).join("")}
          </div>` : ""}
        </article>`).join("")}
      </div>
    </section>`;
}

function renderTechnicalDetails(guide) {
  const notes = [...(guide.technicalDetails || []), ...(guide.controls || [])];
  const hasNetworkScope = Boolean(guide.networkScope && guide.networkScope.length);
  if (!notes.length && !hasNetworkScope) return "";
  return `
    <section class="guide-section guide-technical-details">
      <h3>Technical notes</h3>
      ${notes.length ? `<div class="guide-protections">
        <ul>${notes.map(item => `<li>${inlineCode(item)}</li>`).join("")}</ul>
      </div>` : ""}
      ${renderNetworkScope(guide.networkScope)}
    </section>`;
}

function renderNetworkScope(rows) {
  if (!rows || !rows.length) return "";
  return `
    <div class="guide-network-scope">
      <h4>Exact network boundary</h4>
      <div class="guide-network-rows">
        ${rows.map(([host, scope]) => `<div><code>${esc(host)}</code><span>${esc(scope)}</span></div>`).join("")}
      </div>
    </div>`;
}
