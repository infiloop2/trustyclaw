-- Tool framework host state: which bundled tools are enabled, their per-tool
-- deployment configuration, per-tool OAuth credentials, and host-owned approval
-- workflow records. Formats mirror docs/architecture/tools/tool-contract.md as a
-- storage-level backstop; the runtime validates before writing. Each host is
-- single-operator, so tool state is partitioned by tool_id.

-- migrate:up

-- Bundled tools the operator has enabled. Presence-based (a row means
-- enabled), like managed_provider_access.
CREATE TABLE enabled_tools (
    tool_id TEXT PRIMARY KEY CHECK (tool_id ~ '^[a-z][a-z0-9_]{0,63}$')
);

-- Deployment-level configuration values declared by tool manifests (OAuth
-- client ids/secrets, API keys). Keyed by (tool_id, manifest config key):
-- config is scoped per tool, so two tools that declare the same key (for
-- example GOOGLE_OAUTH_CLIENT_ID) hold their own independent value. All config
-- values are secrets, stored as secretbox ciphertext at rest (see
-- host/runtime/secretbox.py), the same accidental-exposure control as the
-- GitHub credential and tunnel token columns.
CREATE TABLE tool_config (
    tool_id TEXT NOT NULL CHECK (tool_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    key TEXT NOT NULL CHECK (key ~ '^[A-Z][A-Z0-9_]{0,127}$'),
    value TEXT NOT NULL CHECK (value <> ''),
    PRIMARY KEY (tool_id, key)
);

-- Tool OAuth credentials, the store behind HostAPI.credentials. One row per
-- tool holds that tool's single StoredCredential, stored as its contract
-- fields (host/tools/host_api.py) rather than one opaque blob: the non-secret
-- connected-account metadata (stable provider account id, display label,
-- granted scopes), the provider token material, and the tool's non-secret
-- bookkeeping. Only ``secret`` is secret: it is the serialized token JSON
-- object stored as secretbox ciphertext, so OAuth access and refresh tokens
-- are encrypted at rest like every other secret column; the runtime decrypts
-- and re-parses it on read.
CREATE TABLE tool_credentials (
    tool_id TEXT PRIMARY KEY CHECK (tool_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    account_id TEXT NOT NULL CHECK (account_id <> ''),
    account_label TEXT NOT NULL,
    account_scopes JSONB NOT NULL,
    secret TEXT NOT NULL CHECK (secret <> ''),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Host-owned approval workflow records for proposed tool actions. number is
-- the record identity; the public "approval_<number>" id is formatted by the
-- storage accessors. Status transitions are atomic conditional updates from
-- the expected prior status, so an approval is single-use by construction.
-- An approved action's outcome is a single user-visible text per the tool
-- contract (ApprovalExecuted.message, or the failure error), so result is
-- text; the status column says which of the two it is.
CREATE TABLE tool_approvals (
    number BIGSERIAL PRIMARY KEY,
    tool_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'expired', 'executed', 'failed')),
    summary TEXT NOT NULL,
    payload JSONB NOT NULL,
    check_token TEXT NOT NULL CHECK (check_token ~ '^[A-Za-z0-9_-]{32,255}$'),
    result TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL CHECK (created_at >= 0),
    decided_at BIGINT NOT NULL DEFAULT 0
);
-- The hot paths: pending lists in the admin UI and the expiry sweep.
CREATE INDEX tool_approvals_status_idx ON tool_approvals (status, number);

-- Tool audit log: one row per tool event (agent call, approval decision,
-- connect/disconnect), the tool-side peer of the agent and network event
-- logs. Newest-first pages read the seq primary-key index; retention keeps
-- the most recent rows, pruned amortized on insert.
CREATE TABLE tool_events (
    seq BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

-- The dedicated tools service reads tool state with its own scoped Postgres
-- role, the same pattern as the trustyclaw-proxy grants in 0001/0002 (the role
-- itself and its pg_hba line are provisioned by bootstrap before migrations
-- run, like the proxy role). Enablement and config are operator actions
-- written only by the admin API, so the tools role is read-only on
-- enabled_tools and tool_config and cannot enable a tool or rewrite config; it
-- writes only the credentials/approvals/events it mutates. SELECT on
-- secret_keys decrypts the secretbox-encrypted tool config and OAuth
-- credentials (read-only, exactly as the proxy role holds it for its own
-- secrets). The REVOKE first drops the broader write grants a host got from an
-- earlier iteration of this feature; it is a no-op on a fresh install.
REVOKE INSERT, UPDATE, DELETE ON enabled_tools, tool_config FROM "trustyclaw-tools";
GRANT SELECT ON enabled_tools, tool_config TO "trustyclaw-tools";
GRANT SELECT, INSERT, UPDATE, DELETE ON tool_credentials, tool_approvals, tool_events TO "trustyclaw-tools";
GRANT USAGE ON SEQUENCE tool_approvals_number_seq, tool_events_seq_seq TO "trustyclaw-tools";
GRANT SELECT ON secret_keys TO "trustyclaw-tools";

-- migrate:down
DROP TABLE tool_events;
DROP TABLE tool_approvals;
DROP TABLE tool_credentials;
DROP TABLE tool_config;
DROP TABLE enabled_tools;
REVOKE SELECT ON secret_keys FROM "trustyclaw-tools";
