/**
 * TrustyClaw bundled-tools bridge for the Pi harness.
 *
 * Pi is the one harness with no MCP client, so this host-owned extension is
 * the adapter: it spawns the same stdio MCP shim every other harness uses
 * (host.runtime.agent_shim.mcp_shim), lists the shim's tools once at load,
 * and registers each one as a Pi custom tool whose execute() forwards the
 * call over the shim's newline-delimited JSON-RPC and returns the MCP
 * content blocks unchanged (both sides speak {type: "text", text} content).
 *
 * The launcher loads this file explicitly (run-pi.sh passes
 * "-e /opt/trustyclaw-host/host/runtime/agent_shim/pi_tools_bridge.js"
 * while keeping --no-extensions, which disables discovery but honors
 * explicit paths), so the only extension is this root-owned one and the
 * agent-writable extension surface stays closed. The shim child inherits
 * the Pi process's user and thread-scope cgroup, so kernel peer-credential
 * auth and app-thread identity derivation work exactly as they do when
 * Claude Code or Codex spawn the shim.
 *
 * Failure contract: a shim that cannot start or serve its listing leaves
 * the bundled tools unregistered and the session running — the same
 * omit-unavailable behavior the shim itself applies to its tool sockets.
 * Only a missing bridge file fails the Pi process (Pi exits at startup),
 * which cannot happen in a consistent deploy: this file and the launcher
 * flag ship in the same /opt/trustyclaw-host tree.
 */
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

// The shim reads its socket paths from its own environment; these mirror the
// shim's env-override convention so tests can point the bridge at a repo
// checkout instead of the installed host tree.
const SHIM_PYTHON = process.env.TRUSTYCLAW_SHIM_PYTHON || "/usr/bin/python3";
const SHIM_PYTHONPATH = process.env.TRUSTYCLAW_SHIM_PYTHONPATH || "/opt/trustyclaw-host";
// Connect-phase calls answer from local code only; keep their bound tight so
// a broken shim degrades the session in seconds. Tool calls forward to host
// services whose slowest actions stream media, so their bound sits above the
// shim's own 120s socket timeout.
const CONNECT_TIMEOUT_MS = 15_000;
const CALL_TIMEOUT_MS = 130_000;

export default async function trustyclawTools(pi) {
  const child = spawn(SHIM_PYTHON, ["-m", "host.runtime.agent_shim.mcp_shim"], {
    env: { ...process.env, PYTHONPATH: SHIM_PYTHONPATH },
    stdio: ["pipe", "pipe", "ignore"],
  });

  let nextId = 1;
  const pending = new Map();
  const settleAll = (message) => {
    for (const [id, settle] of [...pending]) {
      pending.delete(id);
      settle(message);
    }
  };
  child.on("error", () => settleAll({ error: { message: "TrustyClaw tools shim failed to start" } }));
  child.on("close", () => settleAll({ error: { message: "TrustyClaw tools shim exited" } }));
  child.stdin.on("error", () => {});
  createInterface({ input: child.stdout }).on("line", (line) => {
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      return;
    }
    const settle = pending.get(message?.id);
    if (settle) {
      pending.delete(message.id);
      settle(message);
    }
  });

  const request = (method, params, timeoutMs) =>
    new Promise((resolve) => {
      const id = nextId++;
      const timer = setTimeout(() => {
        pending.delete(id);
        resolve({ error: { message: "TrustyClaw tools shim timed out" } });
      }, timeoutMs);
      pending.set(id, (message) => {
        clearTimeout(timer);
        resolve(message);
      });
      try {
        child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
      } catch {
        // The close handler settles this request.
      }
    });

  const initialized = await request(
    "initialize",
    {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "pi-trustyclaw-bridge", version: "1.0.0" },
    },
    CONNECT_TIMEOUT_MS,
  );
  if (initialized.error) return;
  const listing = await request("tools/list", {}, CONNECT_TIMEOUT_MS);
  if (listing.error || !Array.isArray(listing.result?.tools)) return;

  for (const tool of listing.result.tools) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description || tool.name,
      parameters: tool.inputSchema || { type: "object", properties: {} },
      async execute(_toolCallId, params) {
        const response = await request(
          "tools/call",
          { name: tool.name, arguments: params ?? {} },
          CALL_TIMEOUT_MS,
        );
        if (response.error) {
          return {
            content: [{ type: "text", text: String(response.error.message || "tool call failed") }],
            isError: true,
          };
        }
        const result = response.result || {};
        return {
          content: Array.isArray(result.content) ? result.content : [],
          isError: result.isError === true,
        };
      },
    });
  }
}
