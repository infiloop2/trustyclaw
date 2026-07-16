# TrustyClaw Tools

TrustyClaw's bundled tool framework and bundled tool packages. The framework and
packages live under `host/tools/`; they are host-neutral (no UI, and the only
state they own is one OAuth credential), so the same package could run on another
host implementation of the same contract.

- [`tool-contract.md`](tool-contract.md) — the complete, host-neutral contract
  between a tool package and its host: the manifest, actions and per-action data
  policy, results, credential flows, the host API (credentials, config,
  approvals, staged assets), and the rules of the boundary. This is the source
  of truth; the
  Python protocols under `host/tools/` express it as code.
- [`host-integration.md`](host-integration.md) — how *this* host implements the
  contract: which user and service run tool code and how they reach the internet,
  the agent-facing MCP surface, the local sockets involved, the operator UI, and
  the state model.

Tool-specific documentation lives in the admin UI's Integration Guides and is
rendered from the guide content owned by each package under `host/tools/`. Those
guides are the source of truth for what an integration does, setup, protections,
data flow, and technical notes. Per-tool Markdown references do not live here,
so adding or changing a package updates one operator-facing source.

A tool package is pure tool logic: action handlers, input schemas, third-party
API calls, third-party auth (OAuth flows, token refresh), and per-action data
policy. The host owns every deployment-specific concern: the credential store,
config, approval decisions, staged binary assets, and audit logging. Packages
reach them only through
the small host API.

```
agent / chat / MCP gateway
        │  action calls
        ▼
host  (host API: credentials · config · approvals)
        │  Tool.execute(action, input, api)
        ▼
tool package
        │  normal third-party API calls
        ▼
third-party APIs
```

Bundled tools use the same release train as the host runtime rather than
independent per-tool versions while they live in this repo.

## Testing

Tool package unit tests mock the host API and every third-party boundary, so
they run without network access or credentials. Hosts get the complementary
guarantee: a fake `HostAPI` (in-memory credential store, static config, scripted
approvals) is enough to exercise a whole tool package. Each bundled tool is
covered by its own tests in `tests/test_tools.py` (the original three tools) or
a per-tool `tests/test_tools_<tool_id>.py` file.
