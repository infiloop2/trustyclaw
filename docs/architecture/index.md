# Architecture

TrustyClaw runs Codex and Claude Code runtimes on an AWS EC2 instance behind
fail-closed network controls. The architecture docs are split by responsibility
so operators and contributors can jump to the trust boundary they need.

## Sections

| Doc | Contents |
| --- | --- |
| [Architecture diagram](diagram.md) | One-page host capability map covering operator access, service users, storage, and egress boundaries. |
| [Deployment and upgrades](deployment.md) | EC2 provisioning, upgrade/recovery behavior, drive lifecycle, and secret handling. |
| [Admin state storage and migrations](admin-state-storage.md) | The local Postgres database: schema, access control, and schema migrations. |
| [Control planes](control-planes.md) | Operator-plane and admin-plane responsibilities and authority. |
| [Privilege boundaries](privilege-boundaries.md) | Linux users, fixed sudo helpers, and root-owned helper pattern. |
| [Filesystem layout](filesystem.md) | Trusted root paths, durable volumes, and per-service ownership. |
| [Services and runtimes](services-and-runtimes.md) | systemd units, process inventory, threads, Codex, and Claude runtime model. |
| [Agent provider lifecycle](agent-provider-lifecycle.md) | Runtime status lifecycle, refresh triggers, live credential validation, account anchoring, proxy pinning, and operator recovery. |
| [Runtime harness dependencies](harness-dependencies.md) | Codex and Claude Code interfaces, auth files, request shapes, and upgrade review points. |
| [Admin API architecture](admin-api.md) | Local API security, task orchestration, and maintenance. |
| [Apps](apps/apps.md) | App services, storage and migrations, embedded admin UI surfaces, and app security boundaries. |
| [App: Agent Chat](apps/agent-chat.md) | The threaded chat app: thread index, task references, and its display-only agent surface. |
| [App: Mission Pursuit](apps/mission-pursuit.md) | The agent-furnished workspace app: action protocol, scheduling, artifacts, memory, and its structured agent boundary. |
| [Agent App API](apps/agent-app-api.md) | The `app_api` tool: kernel-attributed agent → app backend calls through the dedicated agent-app service. |
| [Network controls](network-controls.md) | nftables, typed integration guards (AI providers, GitHub, packages, custom domains), agent introspection, and fail-closed behavior. |
| [GitHub write-path controls](github-write-path-controls.md) | The implemented `.github` push-inspection, quarantine, approval, replay, and failure model. |
| [Tools](tools/README.md) | Bundled tool framework: the host-neutral tool contract, this host's integration, approvals, and the bundled tool packages. |
| [Local sockets](local-sockets.md) | Peer-credentialed Unix-domain sockets (tools, agent-app, app-backend, Postgres) and their trust boundaries. |
| [IAM policy notes](iam-policy.md) | Why each deploy IAM statement exists and why its scope is constrained. |

## Overview

TrustyClaw runs Codex and Claude Code runtimes on an AWS EC2 instance behind
fail-closed network controls. Each task chooses its runtime harness, such as
Codex or Claude Code. The host is long-lived in normal operation; the EC2
instance and its root EBS volume carry the
`trustyclaw-host-agent-name=<agent_name>` tag so that deploy can find,
terminate, and recreate them when the operator upgrades or recovers the host.

TrustyClaw's Python runtime uses only the Python 3 standard library. Admin,
network, app, and tool state live in a local Postgres database on the durable
admin volume, spoken to by an in-repo wire-protocol client
(`host/runtime/pgclient.py`). The proxy keeps only file-oriented TLS and Git
quarantine state in its own durable directory.
