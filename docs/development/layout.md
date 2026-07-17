# Code Layout

The production Python runtime uses only the standard library. PostgreSQL is
reached through the in-repo wire client rather than a third-party driver, and a
unit test rejects non-standard-library runtime imports.

```text
host/
  apps/                     # bundled app packages; each manifest declares a stable host_slot
  bootstrap/
    agent-home/             # immutable runtime instructions and harness settings
    helpers/                # root-owned fixed sudo helpers installed on the host
    user_data.sh            # minimal first-boot operator/deploy-key setup
    bootstrap.sh            # full host bootstrap run over SSH as root
  cli/                      # operator-side lifecycle and power commands
  migrations/               # ordered admin-state SQL migrations with up/down sections
  runtime/
    admin_api.py            # localhost admin API, UI server, and route dispatcher
    app_*.py                # app discovery, migrations, browser proxy, and backend socket
    github_*.py             # credential convergence, audits, and held-push lifecycle
    tools_*.py              # tools admin routes, service, socket, MCP shim, and HostAPI
    orchestrator.py         # task workers, runtime status/account pollers, and convergence
    codex_app_server.py     # Codex stdio JSON-RPC adapter
    claude_code.py          # Claude Code stream-json adapter and login process
    network_proxy.py        # policy-enforcing HTTP(S)/WS(S) proxy
    network_policy.py       # policy load and shared route canonicalization
    network_introspection_api.py  # read-only agent network tools and Unix socket
    db.py                   # process-local Postgres pool and transactions
    pgclient.py             # minimal PostgreSQL wire-protocol client
    state.py                # normalized Postgres storage accessors
    migrate.py              # admin-state migration runner
  network_integrations/     # network integrations: per-integration manifest,
                            # guard, and registry (see architecture/network-controls.md)
  tools/                    # host-neutral tool contract and bundled tool packages
  config.py                 # lifecycle input and network-policy validation
  constants.py              # shared loopback port constants
tests/
  smoke/                    # fresh live AWS host checks
  stage/                    # persistent, credentialed live AWS checks
  smoke-ui/                 # deterministic local admin UI mock and browser smoke
docs/                       # API, architecture, development, and commit-scoped audits
.github/                    # no-network CI and admin-triggered live AWS workflows
```

Important source areas and the context that runs them:

| Source | Runs as | Responsibility |
| --- | --- | --- |
| `host/cli/` | Operator machine | Validates lifecycle input; provisions, replaces, recovers, starts, or stops AWS resources; renders and runs bootstrap. |
| `host/config.py` | Operator machine and host services | Validates lifecycle input and the stored/runtime network policy. |
| `host/bootstrap/user_data.sh` | root through EC2 user data | Creates the operator account and installs only the single-use deploy SSH key. |
| `host/bootstrap/bootstrap.sh` | root through lifecycle SSH | Mounts volumes, installs pinned dependencies, creates fixed users, configures PostgreSQL/nftables/systemd, applies migrations, and writes trusted host files. |
| `host/bootstrap/helpers/` | root through exact `trustyclaw-admin` sudo rules | Launches runtimes as the agent user, reads or clears narrow agent-auth state, reads bounded agent files, reboots, and performs GitHub operations that need root egress. |
| `host/apps/` | App users, plus bootstrap/admin readers | Contains app manifests, agent instructions, backend/UI/migration files, and stable `host_slot` declarations. See [Apps](../architecture/apps/apps.md). |
| `host/tools/` | `trustyclaw-tools` | Defines the host-neutral tool contract and bundled packages. Package discovery is directory-based; helper packages are explicitly excluded. |
| `host/runtime/admin_api.py` | `trustyclaw-admin` | Serves `127.0.0.1:7443`, authenticates operator APIs, dispatches app/tool routes, owns task state, and starts workers and maintenance. |
| `host/runtime/admin_ui*` | Browser, served by admin API | Implements the native-ES-module operator UI and its static assets. |
| `host/runtime/admin_errors.py` | Admin route modules | Holds the shared `ApiError` class so the `__main__` service and imported route modules map status codes consistently. |
| `host/runtime/app_platform.py` | Operator/bootstrap and admin API | Validates installed app manifests and derives host-owned users, roles, schemas, routes, services, and ports. |
| `host/runtime/app_migrate.py` | App role for SQL; admin role for records | Applies replay-safe app SQL under the app schema and records versions in host-owned state. |
| `host/runtime/app_api_proxy.py` | `trustyclaw-admin` | Proxies authenticated browser app requests to uid-firewalled loopback app ports without forwarding the raw admin bearer. |
| `host/runtime/app_backend_admin_api.py` | `trustyclaw-admin` | Serves the peer-authenticated app-backend Unix socket and scopes allowlisted task/thread routes to the calling app. |
| `host/runtime/orchestrator.py` | `trustyclaw-admin` | Runs the six task workers, runtime status/account pollers, credential convergence, and task lifecycle coordination. |
| `host/runtime/codex_app_server.py` | Admin adapter controlling an agent child | Implements the Codex stdio JSON-RPC protocol and runtime lifecycle. |
| `host/runtime/claude_code.py` | Admin adapter controlling an agent child | Implements Claude Code stream-json turns, steering, login, and status probes. |
| `host/runtime/network_proxy.py` | `trustyclaw-proxy` | Serves `127.0.0.1:7445`, terminates/inspects proxied traffic, applies policy before upstream connections, and records network events. |
| `host/runtime/network_policy.py` | Admin and proxy processes | Loads policy and provides shared path canonicalization and route matching used by integration guards. |
| `host/runtime/network_introspection_api.py`, `network_introspection_service.py` | `trustyclaw-agent-network` | Serve read-only integration status and denial guidance over the peer-authenticated agent-network socket with no egress. |
| `host/runtime/db.py`, `pgclient.py` | Admin, proxy, tools, agent-network, app/bootstrap clients | Implement peer-authenticated Unix-socket PostgreSQL connections, pooling, and transactions without a driver dependency. |
| `host/runtime/state.py` | Admin, proxy, tools, and agent-network processes under their OS roles | Implements per-operation normalized storage access. Database grants remain the cross-process authority boundary. |
| `host/runtime/migrate.py` | `trustyclaw-admin` during bootstrap | Applies ordered admin-state migrations before application services start; the running admin service never migrates. |
| `host/runtime/write_config.py` | `trustyclaw-admin` during bootstrap | Chooses replacement or carried-over operator config for the lifecycle mode, encrypts secrets, stores normalized rows, and returns the effective config to root bootstrap. |
| `host/runtime/tools_admin_api.py` | `trustyclaw-admin` | Implements operator-facing tool listing, config, enablement, OAuth delegation, approvals, and audit routes. |
| `host/runtime/tools_service.py`, `tools_api.py` | `trustyclaw-tools` | Own the tools socket, execute tool packages with scoped database access and direct HTTPS egress, and recover interrupted approvals. |
| `host/runtime/tools_host.py` | Admin and tools processes | Implements tool discovery, manifest validation, config/credential views, single-use approvals, and tool audit events. |
| `host/runtime/tools_mcp_shim.py` | `trustyclaw-agent`, spawned by each harness | Aggregates the peer-authenticated tools, network-introspection, and app sockets over one stdio MCP server. |
| `host/runtime/github_*.py` | `trustyclaw-admin`, with fixed root helpers for egress | Converges the GitHub credential, derives repository warnings, queues `.github` pushes, and resolves operator decisions. |

Develop against Python 3.11, the Ubuntu 22.04 host and CI runtime version.
