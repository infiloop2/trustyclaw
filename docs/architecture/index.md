# Architecture

TrustyClaw runs Codex and Claude Code runtimes on an AWS EC2 instance behind
fail-closed network controls. The architecture docs are split by responsibility
so operators and contributors can jump to the trust boundary they need.

## Sections

| Doc | Contents |
| --- | --- |
| [Deployment and upgrades](deployment.md) | EC2 provisioning, upgrade/recovery behavior, drive lifecycle, and secret handling. |
| [Admin state storage and migrations](admin-state-storage.md) | The local Postgres database: schema, access control, and schema migrations. |
| [Control planes](control-planes.md) | Operator-plane and admin-plane responsibilities and authority. |
| [Privilege boundaries](privilege-boundaries.md) | Linux users, fixed sudo helpers, and root-owned helper pattern. |
| [Filesystem layout](filesystem.md) | Root, admin, proxy, agent, and optional Cloudflare storage paths and ownership. |
| [Services and runtimes](services-and-runtimes.md) | systemd units, process inventory, threads, Codex, and Claude runtime model. |
| [Runtime harness dependencies](harness-dependencies.md) | Codex and Claude Code interfaces, auth files, request shapes, and upgrade review points. |
| [Admin API architecture](admin-api.md) | Local API security, idempotency, task orchestration, and maintenance. |
| [Apps](apps.md) | App services, storage and migrations, embedded admin UI surfaces, and app security boundaries. |
| [Network controls](network-controls.md) | nftables, proxy policy, managed integration guards (AI providers, GitHub, packages), internal guard fields, and fail-closed behavior. |
| [GitHub write-path controls](github-write-path-controls.md) | Design record for enforcement beyond the advisory audit: PR-only mode and the `.github` push-approval gate (not implemented). |
| [IAM policy notes](iam-policy.md) | Why each deploy IAM statement exists and why its scope is constrained. |

## Overview

TrustyClaw runs Codex and Claude Code runtimes on an AWS EC2 instance behind
fail-closed network controls. Each task chooses its runtime harness, such as
Codex or Claude Code. The host is long-lived in normal operation; the EC2
instance and its root EBS volume carry the
`trustyclaw-host-agent-name=<agent_name>` tag so that deploy can find,
terminate, and recreate them when the operator upgrades or recovers the host.

Everything is plain Python 3 standard library — no third-party runtime
dependencies. Admin state lives in a local Postgres database on the durable
admin volume, spoken to by an in-repo wire-protocol client
(`host/runtime/pgclient.py`); proxy state stays in proxy-owned files.
