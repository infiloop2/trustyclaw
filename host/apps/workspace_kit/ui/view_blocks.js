// Declarative artifact-view renderer shared by every workspace_kit app.
// The host serves this canonical file at /workspace-kit/view_blocks.js for
// same-origin app frames. All agent-authored text is escaped here before it
// reaches the DOM; nothing in a view can run script.

const esc = value => {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
};
const escAttr = value => esc(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");

// Escape first, then apply the tiny inline markup: `code`, **bold**, *italic*.
function mdLite(value) {
  let html = esc(value);
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  return html.replace(/\n/g, "<br>");
}

function renderBlock(block, artifactId) {
  if (!block || typeof block !== "object") return "";
  if (block.type === "heading") {
    const level = block.level === 1 ? "b-h1" : block.level === 3 ? "b-h3" : "b-h2";
    return `<div class="b-heading ${level}">${esc(block.text)}</div>`;
  }
  if (block.type === "text") {
    return String(block.text).split(/\n{2,}/).map(paragraph => `<p class="b-text">${mdLite(paragraph)}</p>`).join("");
  }
  if (block.type === "callout") {
    const tone = ["success", "warning", "danger"].includes(block.tone) ? block.tone : "info";
    return `<aside class="b-callout ${tone}">
      ${block.title ? `<div class="callout-title">${esc(block.title)}</div>` : ""}
      <div class="callout-text">${mdLite(block.text)}</div>
    </aside>`;
  }
  if (block.type === "metrics") {
    return `<div class="b-metrics">${(block.items || []).map(item => `
      <div class="metric-tile">
        <span class="metric-label">${esc(item.label)}</span>
        <span class="metric-value">${esc(item.value)}</span>
        ${item.delta ? `<span class="metric-delta ${String(item.delta).startsWith("-") ? "down" : "up"}">${esc(item.delta)}</span>` : ""}
      </div>`).join("")}</div>`;
  }
  if (block.type === "cards") {
    return `<div class="b-cards">${(block.items || []).map(item => {
      const tone = ["info", "success", "warning", "danger"].includes(item.tone) ? item.tone : "neutral";
      return `<article class="artifact-card ${tone}">
        <div class="card-head"><span class="card-title">${esc(item.title)}</span>${item.badge ? `<span class="card-badge">${esc(item.badge)}</span>` : ""}</div>
        ${item.text ? `<div class="card-text">${mdLite(item.text)}</div>` : ""}
      </article>`;
    }).join("")}</div>`;
  }
  if (block.type === "details") {
    return `<dl class="b-details">${(block.items || []).map(item => `
      <div class="detail-row"><dt>${esc(item.label)}</dt><dd>${mdLite(item.value)}</dd></div>`).join("")}</dl>`;
  }
  if (block.type === "list") {
    const tag = block.style === "number" ? "ol" : "ul";
    return `<${tag} class="b-list">${(block.items || []).map(item => `<li>${mdLite(item)}</li>`).join("")}</${tag}>`;
  }
  if (block.type === "table") {
    const head = (block.columns || []).map(column => `<th>${esc(column)}</th>`).join("");
    const body = (block.rows || []).map(row => `<tr>${row.map(cell => `<td>${esc(cell)}</td>`).join("")}</tr>`).join("");
    return `<div class="b-table-wrap"><table class="b-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }
  if (block.type === "checklist") {
    return `<ul class="b-checklist">${(block.items || []).map(item => `
      <li class="${item.done ? "done" : ""}"><span class="check-mark" aria-hidden="true">${item.done ? "✓" : ""}</span>${esc(item.text)}</li>`).join("")}</ul>`;
  }
  if (block.type === "progress") {
    const value = Math.max(0, Math.min(100, Number(block.value) || 0));
    return `<div class="b-progress">
      ${block.label ? `<span class="progress-label">${esc(block.label)}</span>` : ""}
      <span class="progress-track"><span class="progress-fill" style="width:${value}%"></span></span>
      <span class="progress-value">${esc(Math.round(value))}%</span>
    </div>`;
  }
  if (block.type === "timeline") {
    return `<ol class="b-timeline">${(block.items || []).map(item => `
      <li class="${escAttr(item.status)}">
        <span class="timeline-marker" aria-hidden="true"></span>
        <div class="timeline-content">
          <div class="timeline-head"><span class="timeline-title">${esc(item.title)}</span>${item.time ? `<span class="timeline-time">${esc(item.time)}</span>` : ""}</div>
          ${item.text ? `<div class="timeline-text">${mdLite(item.text)}</div>` : ""}
        </div>
      </li>`).join("")}</ol>`;
  }
  if (block.type === "kanban") {
    return `<div class="b-kanban">${(block.columns || []).map(column => `
      <section class="kanban-column">
        <div class="kanban-title">${esc(column.title)}<span>${(column.items || []).length}</span></div>
        <div class="kanban-items">${(column.items || []).map(item => `<div class="kanban-item">${mdLite(item)}</div>`).join("") || `<div class="kanban-empty">Empty</div>`}</div>
      </section>`).join("")}</div>`;
  }
  if (block.type === "chart") return renderChart(block);
  if (block.type === "code") {
    return `<div class="b-code">${block.language ? `<span class="code-lang">${esc(block.language)}</span>` : ""}<pre>${esc(block.text)}</pre></div>`;
  }
  if (block.type === "button") {
    const tone = ["neutral", "danger"].includes(block.tone) ? block.tone : "primary";
    return `<div class="b-control b-button-control">
      <button type="button" class="b-control-button ${tone}" data-artifact-interaction
        data-artifact-id="${escAttr(artifactId)}" data-control-id="${escAttr(block.control_id)}"
        data-control-type="button">${esc(block.label)}</button>
    </div>`;
  }
  if (block.type === "toggle") {
    return `<label class="b-control b-toggle-control">
      <span class="control-label">${esc(block.label)}</span>
      <input type="checkbox" data-artifact-interaction data-artifact-id="${escAttr(artifactId)}"
        data-control-id="${escAttr(block.control_id)}" data-control-type="toggle" ${block.value ? "checked" : ""}>
      <span class="toggle-track" aria-hidden="true"><span></span></span>
    </label>`;
  }
  if (block.type === "field") {
    return `<label class="b-control b-field-control">
      <span class="control-label">${esc(block.label)}</span>
      <span class="field-input-row">
        <input type="text" maxlength="1000" value="${escAttr(block.value)}" placeholder="${escAttr(block.placeholder || "")}"
          data-artifact-id="${escAttr(artifactId)}" data-control-id="${escAttr(block.control_id)}" data-control-type="field">
        <button type="button" class="field-submit-button" data-field-submit aria-label="Submit field" title="Submit field">
          <svg width="16" height="16" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 6l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
      </span>
    </label>`;
  }
  if (block.type === "divider") return `<hr class="b-divider">`;
  return "";
}

// Single-series chart per the repo's chart conventions: thin marks with
// rounded data ends, a 2px gap between bars, recessive gridlines, labels in
// text tokens, and a hover tooltip. Bars are zero-based; lines pad min..max.
const CHART_SERIES_COLOR =
  (getComputedStyle(document.documentElement).getPropertyValue("--chart-series") || "").trim() || "#8b7cf6";

function renderChart(block) {
  const points = block.points || [];
  const W = 560, H = 220, padLeft = 44, padRight = 12, padTop = 14, padBottom = 26;
  const plotW = W - padLeft - padRight, plotH = H - padTop - padBottom;
  const values = points.map(point => Number(point.value));
  let min = Math.min(...values), max = Math.max(...values);
  if (block.kind === "bar") { min = Math.min(0, min); max = Math.max(0, max); }
  else { const pad = (max - min) * 0.1 || Math.abs(max) * 0.1 || 1; min -= pad; max += pad; }
  if (min === max) { min -= 1; max += 1; }
  const yFor = value => padTop + plotH - ((value - min) / (max - min)) * plotH;
  const ticks = [min, (min + max) / 2, max];
  const grid = ticks.map(tick => {
    const y = yFor(tick);
    return `<line x1="${padLeft}" y1="${y}" x2="${W - padRight}" y2="${y}" class="chart-grid"/>` +
      `<text x="${padLeft - 6}" y="${y + 3}" class="chart-tick" text-anchor="end">${esc(shortNumber(tick))}</text>`;
  }).join("");
  const labelEvery = Math.ceil(points.length / 7);
  const xLabels = points.map((point, index) => {
    if (index % labelEvery !== 0 && index !== points.length - 1) return "";
    const x = padLeft + (points.length === 1 ? plotW / 2 : (index / (points.length - 1)) * plotW);
    const text = String(point.label).length > 9 ? String(point.label).slice(0, 8) + "…" : String(point.label);
    return `<text x="${x}" y="${H - 8}" class="chart-tick" text-anchor="middle">${esc(text)}</text>`;
  }).join("");
  let marks = "";
  if (block.kind === "bar") {
    const slot = plotW / points.length;
    const barW = Math.max(3, Math.min(28, slot - 2));
    const zeroY = yFor(0);
    marks = points.map((point, index) => {
      const x = padLeft + slot * index + (slot - barW) / 2;
      const y = yFor(Number(point.value));
      const top = Math.min(y, zeroY), height = Math.max(2, Math.abs(zeroY - y));
      const radius = Math.min(4, barW / 2, height);
      return `<path class="chart-mark" data-index="${index}" d="${roundedBarPath(x, top, barW, height, radius, Number(point.value) >= 0)}" fill="${CHART_SERIES_COLOR}"/>`;
    }).join("");
    // Bar x labels use slot centers instead of the line positions.
    marks += points.map((point, index) => {
      if (index % labelEvery !== 0 && index !== points.length - 1) return "";
      const x = padLeft + slot * index + slot / 2;
      const text = String(point.label).length > 9 ? String(point.label).slice(0, 8) + "…" : String(point.label);
      return `<text x="${x}" y="${H - 8}" class="chart-tick" text-anchor="middle">${esc(text)}</text>`;
    }).join("");
  } else {
    const xFor = index => padLeft + (points.length === 1 ? plotW / 2 : (index / (points.length - 1)) * plotW);
    const path = values.map((value, index) => `${index ? "L" : "M"}${xFor(index).toFixed(1)},${yFor(value).toFixed(1)}`).join(" ");
    marks = `<path d="${path}" fill="none" stroke="${CHART_SERIES_COLOR}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>` +
      values.map((value, index) =>
        `<circle class="chart-mark chart-dot" data-index="${index}" cx="${xFor(index).toFixed(1)}" cy="${yFor(value).toFixed(1)}" r="4" fill="${CHART_SERIES_COLOR}"/>`).join("");
  }
  const baseline = `<line x1="${padLeft}" y1="${padTop + plotH}" x2="${W - padRight}" y2="${padTop + plotH}" class="chart-axis"/>`;
  const data = escAttr(JSON.stringify(points.map(point => ({ label: String(point.label), value: Number(point.value) }))));
  return `<figure class="b-chart" data-points="${data}">
    ${block.label ? `<figcaption class="chart-label">${esc(block.label)}</figcaption>` : ""}
    <div class="chart-frame">
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${escAttr(block.label || "chart")}">
        ${grid}${baseline}${marks}${block.kind === "line" ? xLabels : ""}
      </svg>
      <div class="chart-tooltip" hidden></div>
    </div>
  </figure>`;
}

function roundedBarPath(x, y, width, height, radius, positive) {
  if (positive) {
    return `M${x},${y + height} L${x},${y + radius} Q${x},${y} ${x + radius},${y} L${x + width - radius},${y} Q${x + width},${y} ${x + width},${y + radius} L${x + width},${y + height} Z`;
  }
  return `M${x},${y} L${x + width},${y} L${x + width},${y + height - radius} Q${x + width},${y + height} ${x + width - radius},${y + height} L${x + radius},${y + height} Q${x},${y + height} ${x},${y + height - radius} Z`;
}

function shortNumber(value) {
  const abs = Math.abs(value);
  if (abs >= 1e9) return (value / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (abs >= 1e6) return (value / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (abs >= 1e3) return (value / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
  return abs >= 100 || Number.isInteger(value) ? String(Math.round(value)) : value.toFixed(1);
}

function attachChartTooltips(root) {
  root.querySelectorAll(".b-chart").forEach(figure => {
    const points = JSON.parse(figure.dataset.points || "[]");
    const frame = figure.querySelector(".chart-frame");
    const tooltip = figure.querySelector(".chart-tooltip");
    const svg = figure.querySelector("svg");
    frame.addEventListener("mousemove", event => {
      const marks = [...svg.querySelectorAll(".chart-mark")];
      if (!marks.length) return;
      const frameRect = frame.getBoundingClientRect();
      let best = null, bestDistance = Infinity;
      marks.forEach(mark => {
        const rect = mark.getBoundingClientRect();
        const distance = Math.abs(event.clientX - (rect.left + rect.width / 2));
        if (distance < bestDistance) { bestDistance = distance; best = mark; }
      });
      const index = Number(best.dataset.index);
      const point = points[index];
      if (!point) return;
      const rect = best.getBoundingClientRect();
      tooltip.hidden = false;
      tooltip.textContent = `${point.label}: ${point.value}`;
      tooltip.style.left = `${rect.left + rect.width / 2 - frameRect.left}px`;
      tooltip.style.top = `${rect.top - frameRect.top - 8}px`;
    });
    frame.addEventListener("mouseleave", () => { tooltip.hidden = true; });
  });
}
