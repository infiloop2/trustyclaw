# Code Layout

The runtime is pure Python 3 standard library — no third-party dependencies.
Even the admin-state database is reached through an in-repo protocol client
rather than a driver (pinned by a unit test that rejects non-stdlib imports).

```
host/
  cli/                       # operator-side lifecycle and power commands
  config.py                  # input config + network policy validation
  constants.py               # shared admin/proxy port constants
  bootstrap/
    user_data.sh             # first-boot SSH/bootstrap access setup
    bootstrap.sh             # full host bootstrap run over SSH as root
    helpers/                 # root-owned sudo helper scripts installed on host
  migrations/
    NNNN_description.sql     # versioned admin-state schema migrations (up/down)
  runtime/
    admin_api.py             # localhost admin API
    orchestrator.py          # task worker pool + runtime session cache
    admin_ui.html            # single-page admin UI shell served at GET /
    admin_ui.css             # admin UI styling
    admin_ui.js              # admin UI behavior
    codex_app_server.py      # stdio JSON-RPC client for the Codex app-server
    claude_code.py           # Claude Code CLI adapter + OAuth login process
    network_proxy.py         # policy-enforcing HTTP(S)/WS(S) proxy
    network_policy.py        # policy files, domain matching, request decisions
    proxy_state_client.py    # admin-side client for proxy-state helpers
    read_network_state.py    # proxy-owned state read helper entrypoint
    db.py                    # Postgres connection pool/transactions for admin state
    migrate.py               # schema migration runner (up/down/status)
    pgclient.py              # minimal stdlib PostgreSQL wire-protocol client
    state.py                 # admin-state storage + proxy JSON/JSONL file helpers
    task_status.py           # task lifecycle transition helpers
    write_config.py          # bootstrap helper: replace the config table from stdin
    update_network_policy.py # proxy-owned policy writer entrypoint
    update_provider_account.py # proxy-owned provider-pin writer entrypoint
tests/                       # unit tests, local UI smoke, and live AWS tests
  smoke/                     # manual smoke tests (NOT run in CI)
  stage/                     # persistent staging tests (NOT run in CI)
  smoke-ui/                  # local admin UI mock backend + browser smoke
.github/                     # no-network CI plus admin-triggered live AWS workflows
```

Important source files and the context that runs them:

| Module | Runs as | Purpose |
| --- | --- | --- |
| `host/cli/` | operator's machine | Provisions, upgrades, recovers, and bootstraps the host. Never runs on the host. |
| `host/config.py` | operator machine and host services | Input config and network policy validation. |
| `host/bootstrap/user_data.sh` | root via EC2 user data | Minimal first-boot script: creates the operator account, installs the one-use deploy key, and opens the SSH bootstrap path. |
| `host/bootstrap/bootstrap.sh` | root via SSH deploy | Full host bootstrap: mounts volumes, installs packages and CLIs, creates users including optional `cloudflared`, sets up the admin-state Postgres and applies schema migrations, installs helpers, configures nftables/systemd services, and creates an empty runtime network policy only when no preserved policy exists. |
| `host/bootstrap/helpers/run-codex-app-server.sh` | root via sudo helper | Root-owned launcher that demotes to `trustyclaw-agent` and starts Codex with proxy/CA environment. |
| `host/bootstrap/helpers/run-claude-code.sh` | root via sudo helper | Root-owned launcher that demotes to `trustyclaw-agent` and starts Claude Code with proxy/CA environment. |
| `host/bootstrap/helpers/read-codex-account-id.sh` | root via sudo helper | Narrow helper that reads Codex auth as `trustyclaw-agent` and prints only the inferred OpenAI account id. |
| `host/bootstrap/helpers/read-claude-account.sh` | root via sudo helper | Narrow helper that reads Claude auth as `trustyclaw-agent` and prints only account metadata plus the OAuth bearer hash. |
| `host/bootstrap/helpers/read-agent-file.sh` | root via sudo helper | Narrow helper that reads agent-home directories and bounded file previews as `trustyclaw-agent`. |
| `host/bootstrap/helpers/reboot-host.sh` | root via sudo helper | Root-owned reboot helper used by the admin API. |
| `host/runtime/admin_api.py` | `trustyclaw-admin` | Localhost admin API on `127.0.0.1:7443`. |
| `host/runtime/orchestrator.py` | `trustyclaw-admin` | Task worker pool, runtime process cache, and runtime status poller. |
| `host/runtime/admin_ui.html` | served by admin API | Single-page admin UI shell; a thin layer over the API. |
| `host/runtime/admin_ui.css` | served by admin API | Admin UI styling. |
| `host/runtime/admin_ui.js` | served by admin API | Admin UI behavior and API calls. |
| `host/runtime/codex_app_server.py` | `trustyclaw-admin` | Stdio JSON-RPC client for the Codex app-server. |
| `host/runtime/claude_code.py` | `trustyclaw-admin` | Claude Code CLI adapter and OAuth login process management. |
| `host/runtime/network_proxy.py` | `trustyclaw-proxy` | Policy-enforcing HTTP(S)/WS(S) proxy on `127.0.0.1:7445`. |
| `host/runtime/network_policy.py` | `trustyclaw-admin` and `trustyclaw-proxy` | Policy files, domain matching, request decisions, provider guards. |
| `host/runtime/proxy_state_client.py` | `trustyclaw-admin` | Admin-side client for the proxy-state helpers. |
| `host/runtime/read_network_state.py` | `trustyclaw-proxy` via root sudo helper | Narrow read helper for proxy-owned policy and network events. |
| `host/runtime/db.py` | `trustyclaw-admin` | Postgres connection pool and transaction helper for admin state. |
| `host/runtime/pgclient.py` | `trustyclaw-admin` | Minimal stdlib PostgreSQL wire-protocol client (Unix socket, peer auth, text format). |
| `host/runtime/migrate.py` | `trustyclaw-admin` (bootstrap and service startup) | Applies versioned SQL migrations from `host/migrations/`. |
| `host/runtime/state.py` | `trustyclaw-admin`; selected proxy helpers | Admin-state storage (Postgres) and proxy JSON/JSONL file helpers. |
| `host/runtime/write_config.py` | `trustyclaw-admin` via bootstrap | Computes the effective host config (payload vs. carried-over credentials by operation mode), stores it in the config table, and echoes it for the root-only bootstrap steps. |
| `host/runtime/task_status.py` | `trustyclaw-admin` | Shared task status transition helpers. |
| `host/runtime/update_network_policy.py` | `trustyclaw-proxy` via root sudo helper | The only writer of the network policy files. |
| `host/runtime/update_provider_account.py` | `trustyclaw-proxy` via root sudo helper | Narrow write helper for proxy-owned provider account pins. |

Develop against Python 3.11 (the Ubuntu 22.04 host runtime) to match CI.
