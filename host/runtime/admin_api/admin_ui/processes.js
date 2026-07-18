// Agent processes tab: the live process table for the agent slice.

import { api } from "./api.js";
import { $, esc, formatDuration, mib, setHtml } from "./helpers.js";

export async function refreshAgentProcesses() {
  const response = await api("GET", "/v1/agent-processes");
  const processes = Array.isArray(response.processes) ? response.processes : [];
  $("process-message").textContent = response.truncated ? "Showing first 1000 processes." : "";
  setHtml($("processes"), `<tr><th>pid</th><th>process</th><th>state</th><th>memory</th><th>elapsed</th></tr>` +
    (processes.length ? processes.map(process => `
      <tr>
        <td class="mono">${esc(process.pid)}</td>
        <td>
          <div>${esc(process.name || "")}</div>
          <div class="mono muted process-command">${esc(process.cmdline || "")}</div>
        </td>
        <td>${esc(process.state || "")}</td>
        <td>${process.rss_bytes == null ? `<span class="muted">unknown</span>` : `${esc(mib(process.rss_bytes))} MiB`}</td>
        <td>${process.elapsed_seconds == null ? `<span class="muted">unknown</span>` : esc(formatDuration(process.elapsed_seconds))}</td>
      </tr>`).join("") : `<tr><td colspan="5" class="empty-state">No agent processes are running.</td></tr>`));
}
