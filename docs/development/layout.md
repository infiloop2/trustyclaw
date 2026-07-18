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
    verify_deploy.py        # root end-of-deploy verification of the provisioned state
  cli/                      # operator-side lifecycle and power commands
  migrations/               # ordered admin-state SQL migrations with up/down sections
  runtime/                  # one package per service process; each socket has
                            # exactly one serving package (see runtime/__init__.py)
    admin_api/              # trustyclaw-admin: operator TCP API, admin UI assets,
                            # app-backend socket, orchestrator, agent CLI adapters,
                            # GitHub credential/audit flows
    network_proxy/          # trustyclaw-proxy: policy-enforcing HTTP(S)/WS(S) proxy
    tools/                  # trustyclaw-tools: tools socket, tool execution, assets
    agent_network/          # trustyclaw-agent-network: read-only introspection socket
    agent_app/              # trustyclaw-agent-app: agent app_api socket
    agent_shim/             # trustyclaw-agent: stdio MCP shim, client-side only
    core/                   # shared socketless libraries: db, pgclient, state,
                            # secretbox, network_policy, app_platform
    deploy/                 # bootstrap-run CLIs: migrate, app_migrate, write_config
    root_helpers/           # standalone CLIs invoked as root via sudo helpers
  network_integrations/     # network integrations: per-integration manifest,
                            # guard, and registry (see architecture/network-controls.md)
  tools/                    # host-neutral tool contract and bundled tool packages
  config.py                 # lifecycle input and network-policy validation
  constants.py              # shared ports, socket paths, and pinned service account ids
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
| `host/bootstrap/bootstrap.sh` | root through lifecycle SSH | Runs the ordered provisioning phases: mounts volumes, installs pinned dependencies, creates fixed users, configures PostgreSQL/nftables/systemd, applies migrations, writes trusted host files, and ends by running `verify_deploy`. |
| `host/bootstrap/verify_deploy.py` | root at the end of bootstrap | Independently re-checks accounts, path permissions, sockets, listeners, services, database peer auth, and live firewall behavior in both directions; any mismatch fails the deploy. |
| `host/bootstrap/helpers/` | root through exact `trustyclaw-admin` sudo rules | Launches runtimes as the agent user, reads or clears narrow agent-auth state, reads bounded agent files, reboots, and performs GitHub operations that need root egress. |
| `host/apps/` | App users, plus bootstrap/admin readers | Contains app manifests, agent instructions, backend/UI/migration files, and stable `host_slot` declarations. See [Apps](../architecture/apps/apps.md). |
| `host/tools/` | `trustyclaw-tools` | Defines the host-neutral tool contract and bundled packages. Package discovery is directory-based; helper packages are explicitly excluded. |
| `host/runtime/admin_api/service.py` | `trustyclaw-admin` | Serves `127.0.0.1:7443`, authenticates operator APIs, dispatches app/tool routes, owns task state, and starts workers and maintenance. |
| `host/runtime/admin_ui*` | Browser, served by admin API | Implements the native-ES-module operator UI and its static assets. |
| `host/runtime/admin_api/errors.py` | Admin route modules | Holds the shared `ApiError` class so the `__main__` service and imported route modules map status codes consistently. |
| `host/runtime/core/app_platform.py` | Operator/bootstrap and admin API | Validates installed app manifests and derives host-owned users, roles, schemas, routes, services, and ports. |
| `host/runtime/deploy/app_migrate.py` | App role for SQL; admin role for records | Applies replay-safe app SQL under the app schema and records versions in host-owned state. |
| `host/runtime/admin_api/app_api_proxy.py` | `trustyclaw-admin` | Proxies authenticated browser app requests to uid-firewalled loopback app ports without forwarding the raw admin bearer. |
| `host/runtime/admin_api/app_backend_api.py` | `trustyclaw-admin` | Serves the peer-authenticated app-backend Unix socket and scopes allowlisted task/thread routes to the calling app. |
| `host/runtime/admin_api/orchestrator.py` | `trustyclaw-admin` | Runs the six task workers, runtime status/account pollers, credential convergence, and task lifecycle coordination. |
| `host/runtime/admin_api/codex_app_server.py` | Admin adapter controlling an agent child | Implements the Codex stdio JSON-RPC protocol and runtime lifecycle. |
| `host/runtime/admin_api/claude_code.py` | Admin adapter controlling an agent child | Implements Claude Code stream-json turns, steering, login, and status probes. |
| `host/runtime/network_proxy/service.py` | `trustyclaw-proxy` | Serves `127.0.0.1:7445`, terminates/inspects proxied traffic, applies policy before upstream connections, and records network events. |
| `host/runtime/core/network_policy.py` | Admin and proxy processes | Loads policy and provides shared path canonicalization and route matching used by integration guards. |
| `host/runtime/agent_network/` | `trustyclaw-agent-network` | Serves read-only integration status and denial guidance over the peer-authenticated agent-network socket with no egress. |
| `host/runtime/core/db.py`, `pgclient.py` | Admin, proxy, tools, agent-network, app/bootstrap clients | Implement peer-authenticated Unix-socket PostgreSQL connections, pooling, and transactions without a driver dependency. |
| `host/runtime/core/state.py` | Admin, proxy, tools, and agent-network processes under their OS roles | Implements per-operation normalized storage access. Database grants remain the cross-process authority boundary. |
| `host/runtime/deploy/migrate.py` | `trustyclaw-admin` during bootstrap | Applies ordered admin-state migrations before application services start; the running admin service never migrates. |
| `host/runtime/deploy/write_config.py` | `trustyclaw-admin` during bootstrap | Chooses replacement or carried-over operator config for the lifecycle mode, encrypts secrets, stores normalized rows, and returns the effective config to root bootstrap. |
| `host/runtime/admin_api/tools_client.py` | `trustyclaw-admin` | Implements operator-facing tool listing, config, enablement, OAuth delegation, approvals, and audit routes. |
| `host/runtime/tools/service.py`, `api.py` | `trustyclaw-tools` | Own the tools socket, execute tool packages with scoped database access and direct HTTPS egress, and recover interrupted approvals. |
| `host/runtime/tools/tools_host.py` | Admin and tools processes | Implements tool discovery, manifest validation, config/credential views, single-use approvals, and tool audit events. |
| `host/runtime/agent_shim/mcp_shim.py` | `trustyclaw-agent`, spawned by each harness | Aggregates the peer-authenticated tools, network-introspection, and app sockets over one stdio MCP server. |
| `host/runtime/admin_api/github_*.py` | `trustyclaw-admin`, with fixed root helpers for egress | Converges the GitHub credential, derives repository warnings, queues `.github` pushes, and resolves operator decisions. |

Develop against Python 3.11, the Ubuntu 22.04 host and CI runtime version.
