# Admin State Storage and Migrations

Admin state lives in a local PostgreSQL database, `trustyclaw_admin`, served by
a PostgreSQL instance that runs on the host itself (`trustyclaw-postgres`
systemd unit). The service talks to it through an in-repo, standard-library
wire-protocol client (`host/runtime/pgclient.py`) — no driver dependency;
the client supports exactly the scope the runtime uses (Unix socket, peer
auth, text-format values). Nothing about it is network-reachable: there is no TCP listener
at all (`listen_addresses = ''`), only the local Unix socket. The network
proxy participates under its own database role with narrow grants: read-only
on `network_policy` and `proxy_provider_pins` (which the admin service writes
after validation), plus insert/select/delete on its own `network_events`
table. There is deliberately no fallback cache in the proxy: a database
outage denies every request until the database returns — simple and fail
safe, and nothing could keep flowing anyway since the pins and the decision
log live in the same database. Exactly two things stay
files: the proxy's TLS material (the `ssl` module and `openssl` consume
paths, and the CA private key stays out of the admin-owned schema) and the
deploy-plane `version.json`, which bootstrap must read before the database
exists.

## What lives in the database

The schema lives in `host/migrations/` as ordered, numbered SQL migrations
applied by the migration runner; the current shape is the result of applying
them all, so this document describes the resulting tables, never the
migration history. Columns are
typed and constrained wherever the shape is ours; the single remaining JSON
value is provider account metadata — the provider CLI's own evolving shape,
cached verbatim, deliberately not ours to type.

| Table | Contents |
| --- | --- |
| `config` | Agent name and admin password hash — a single row by constraint, format-checked; refreshed on every deploy (upgrade/recover carry the stored credentials forward). |
| `operator_connections` | Operator access endpoints, one row per mode (duplicates impossible by key); per-mode field requirements are row constraints. |
| `tasks` | Task queue and finished-task history (pruned to the newest 100,000 finished). `number` is the single identity; the public `task_<number>` id is just its label, formatted by the accessors. |
| `task_steers` | Undelivered steer messages per running task, ordered; delivered steers are deleted (their content lives on as events). |
| `agent_events` | Agent runtime and task events with typed payload columns (message/source, error, runtime); pruned to the newest 1,000,000. |
| `thread_sessions` | User thread -> provider session/thread maps (pruned to the 100,000 most recently used per runtime). |
| `oauth_logins` | In-flight OAuth logins, fully typed per flow (device code + login handle for Codex, browser code for Claude). |
| `provider_accounts` | Admin-side provider account records: `account_id` typed, remaining provider-owned metadata as a cached document. |
| `network_policy` + `managed_integrations`, `github_repositories`, `github_settings`, `allowed_domains`, `domain_methods`, `domain_path_guards` | The active network policy as typed, ordered rows (shape defined by `host/config.py`): enabled integrations are presence rows, the GitHub `write_repositories` list keeps operator order (reads are universal, so the rows name only write targets), and `github_settings` is the singleton for integration-level GitHub toggles (`require_dot_github_approval`); replaced atomically, and a missing `network_policy` row is the fail-closed empty default. |
| `github_credential` | The single fixed GitHub credential (PAT, or GitHub App identity/key). Admin-owned only — no proxy grant; secret columns hold `secretbox` ciphertext, and plaintext never leaves the admin service except into the two credential root helpers. The working token the App mints lives only in `proxy_github_token`. |
| `proxy_provider_pins` | Exactly the two values the proxy guards compare: account id and token hash (format-checked). |
| `proxy_github_token` | The proxy's working copy of the active GitHub token, injected into policy-approved GitHub requests. `secretbox` ciphertext like every other stored secret; the proxy role holds SELECT on this row and on `secret_keys`, and because grants are per-table that pair decrypts exactly this working set — the credential and audit tables keep no proxy grant. A proxy compromise therefore exposes just this token: short-lived in app mode, the PAT itself in pat mode (one reason to prefer app mode). |
| `github_repo_audit` | Per-repository audit facts (visibility, the token's effective permissions, default-branch protection, workflows and triggers) fetched by the `audit-github-repo` helper. Admin-owned, no proxy grant; the warning judgments live in code, so message changes never touch stored facts. |
| `pending_pushes` | Pushes held by the `.github` approval gate. Written by the proxy's role (INSERT, like `network_events`) when a gated push touches `.github/`; the admin service lists and resolves them (approve/reject). The held objects live under `refs/pending/<id>` in the proxy's quarantine mirror on disk, not in this table. |
| `network_events` | Network allow/deny decisions, written by the proxy's role, fully typed (pruned to the newest 1,000,000; URL fields are size-capped so the row cap is a real disk bound). |
| `counters` | `next_task_number` (event seqs are a database serial). |
| `secret_keys` | The at-rest encryption key for stored secrets (see below). Proxy SELECT grant so the proxy can decrypt `proxy_github_token`, the only secret-bearing row it can read. |
| `schema_migrations` | Applied migration versions (owned by the migration runner). |

Stored secrets — the GitHub token/private key/working token and the
Cloudflare tunnel token — are encrypted at rest (`host/runtime/secretbox.py`:
AES-256-CBC via openssl) with a key in the `secret_keys` table, created by
the schema migration (from Postgres's CSPRNG), so the key exists from the
moment the schema does. Keeping the key in the database keeps all admin
state in one place; upgrade and recovery carry key and ciphertext together by
construction. This is an accidental-exposure control, not a root/offline
defense: a stray `SELECT *` on a secret-bearing table (or a pasted table
dump) no longer reveals credential material, while a full database dump
necessarily includes the key. Rows written before encryption existed keep
working and re-encrypt on their next write: `decrypt` passes non-ciphertext
values through unchanged, `write_config` re-saves the tunnel token on every
deploy/upgrade, and the proxy's working GitHub token is republished by the
next mint or credential change. No startup sweep is needed.

Ephemeral values — idempotency-key replay records
(`admin_api.IDEMPOTENCY_ENTRIES`) and cached agent runtime statuses
(`orchestrator._RUNTIME_STATUSES`) — live in service memory and reset with the
process.

The data directory is `/mnt/trustyclaw-admin/postgres/<major>/main` on the
durable admin volume, so state survives root-volume replacement on
upgrade. The path is versioned by PostgreSQL major
(currently 14, pinned in bootstrap); a future base-image bump that changes the
major requires an explicit `pg_upgrade` step rather than silently opening old
data files with new binaries.

## How runtime code uses it

`host/runtime/state.py` is a storage API of per-operation accessors that run
real queries against the normalized tables — nothing ever materializes the
whole state, so request cost is independent of history size (the hot paths
are indexed: task status/thread/runtime lookups, per-thread history, event
paging, prune recency). Reads are plain lock-free transactions (MVCC
snapshots) that fetch only what the caller needs. Writes go through
`state.mutation()`, which pairs one database transaction with a process-wide
mutation lock so check-then-act sequences (read a status, decide, write) stay
atomic without per-row lock ceremony — the admin service is the single writer,
enforced by its port bind. Events appended inside a mutation join its
transaction, so an aborted update rolls back its events too; event seqs come
from a database serial (unique and increasing, with harmless gaps from
aborts).

`host/runtime/db.py` owns connections: a small process-wide pool, with nested
transactions on one thread taking separate connections so a read inside a
mutation sees the last committed state.

Growth stays bounded by deliberately high caps (100k finished tasks, 1M
agent events, 1M network events, 100k session mappings per runtime), enforced
by O(1)-planning range deletes (seq is a serial, so newest-N retention is a
primary-key range below `MAX(seq) - N`) on the append cadence and the hourly
maintenance pass. The admin volume is sized for those caps; time-based
auto-cleanup and volume monitoring can replace them later without touching
the storage API.

## Access control

Access control is deliberately minimal — the operating system's user model,
not database passwords or per-table grants:

- Connections are Unix-socket only with `peer` authentication: the client's
  OS user must match its database role, and no passwords exist anywhere.
- `trustyclaw-admin` owns the database and the schema; the admin service (and
  the bootstrap's migration/config steps) connect as it.
- `trustyclaw-proxy` holds the one narrow role: read-only on the policy and
  pin tables, write on its own event table (granted in the schema migration).
- The `postgres` superuser is reachable only by the `postgres` OS user, i.e.
  by operators through sudo: `sudo -u postgres psql trustyclaw_admin`.
- Everyone else — most importantly `trustyclaw-agent` — has no role, and
  `pg_hba.conf` ends with an explicit reject rule, so even a process that can
  reach the socket cannot authenticate.

`pg_hba.conf` and `postgresql.conf` are managed config, rewritten by bootstrap
on every deploy (they live inside the preserved data directory, but their
content comes from the root-owned bootstrap, so a previous compromise cannot
persist config edits across an upgrade).

## Schema migrations

Migrations are plain SQL files in `host/migrations/`, named
`NNNN_description.sql`, with goose-style up and down sections:

```sql
-- migrate:up
ALTER TABLE tasks ADD COLUMN priority BIGINT;

-- migrate:down
ALTER TABLE tasks DROP COLUMN priority;
```

`host/runtime/migrate.py` is the runner
(`python3 -m host.runtime.migrate {up|down|status} [--to VERSION]`). Applied
versions are recorded in `schema_migrations`; `up` applies only pending files,
all in one transaction (PostgreSQL DDL is transactional), under an advisory
lock so concurrent runners serialize. It is an in-repo runner rather than
goose/alembic/dbmate so the host needs no extra toolchain and migrations ship
inside the runtime code archive; the file format deliberately matches that
family of tools.

Migrations are deploy-plane work, applied in exactly one place: bootstrap
runs `migrate up` (as `trustyclaw-admin`) after the database is up and before
services start. This is the upgrade path — redeploy replaces the root volume
and code, preserves the admin volume, and `migrate up` brings the preserved
database to the new code's schema. The admin service itself never migrates:
code and schema change together in one bootstrap or not at all, so a stray
service start can never move the schema under a live instance, and a
schema/code mismatch (unsupported) fails loudly instead of being papered
over.

`migrate down` is a manual operator action only
(`sudo -u trustyclaw-admin env PYTHONPATH=/opt/trustyclaw-host python3 -m host.runtime.migrate down`).

Rules for changing persisted state shape from now on:

- Never edit an applied migration; add a new `NNNN+1_*.sql` file.
- Every migration needs a working down section (the test suite migrates the
  whole history up and back down on every run).
- Old code is not expected to run against a newer schema: deploy replaces the
  code and migrates in one operation, and downgrades go through `migrate down`
  before installing older code.
