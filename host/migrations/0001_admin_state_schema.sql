-- Admin-state schema: the host's durable state as normalized tables.
--
-- Timestamps the runtime compares and returns to API clients are stored as
-- UTC ISO-8601 strings ("YYYY-MM-DDTHH:MM:SSZ"); lexicographic order equals
-- chronological order. Idempotency-key replay records and agent runtime
-- statuses are deliberately absent: both are ephemeral (a retry convenience
-- and derived health) and live in the services' memory, resetting with the
-- process.

-- migrate:up

-- Deploy-provided host configuration, one row by constraint (the singleton
-- key). Replaced on every deploy/reconfigure; upgrade and recover carry the
-- stored credentials forward. The checks mirror host/config.py's validation
-- as a storage-level backstop.
CREATE TABLE config (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    agent_name TEXT CHECK (agent_name ~ '^[A-Za-z0-9_-]{1,50}$'),
    admin_password_sha256 TEXT CHECK (admin_password_sha256 ~ '^[0-9a-f]{64}$')
);

-- Operator access endpoints. mode as the primary key makes duplicate modes
-- impossible by construction; the row check enforces exactly the fields each
-- mode requires.
CREATE TABLE operator_connections (
    mode TEXT PRIMARY KEY CHECK (mode IN ('ssh', 'cloudflare_access')),
    ssh_public_key TEXT CHECK (ssh_public_key LIKE 'ssh-ed25519 %' OR ssh_public_key LIKE 'ssh-rsa %'),
    hostname TEXT CHECK (hostname <> ''),
    tunnel_token TEXT CHECK (tunnel_token <> '' AND tunnel_token !~ '\s'),
    CHECK (
        (mode = 'ssh' AND ssh_public_key IS NOT NULL AND hostname IS NULL AND tunnel_token IS NULL)
        OR (mode = 'cloudflare_access' AND ssh_public_key IS NULL AND hostname IS NOT NULL AND tunnel_token IS NOT NULL)
    )
);

-- Monotonic counters (next_task_number). A plain row instead of a sequence so
-- the number allocation rolls back with its transaction and task numbering
-- stays dense.
CREATE TABLE counters (
    name TEXT PRIMARY KEY,
    value BIGINT NOT NULL
);

-- The task queue and its bounded history. number (from the dense counter) is
-- the task identity; the public "task_<number>" identifier is just its label,
-- formatted by the storage accessors.
CREATE TABLE tasks (
    number BIGINT PRIMARY KEY,
    status TEXT NOT NULL,
    agent_runtime TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    input_message TEXT,
    output_message TEXT,
    error_message TEXT,
    created_at TEXT,
    updated_at TEXT
);
-- The hot paths: active-task lookups (claiming, health, listing), per-thread
-- history, per-runtime claim caps, and history pruning by recency.
CREATE INDEX tasks_status_idx ON tasks (status);
CREATE INDEX tasks_thread_id_idx ON tasks (thread_id);
CREATE INDEX tasks_runtime_status_idx ON tasks (agent_runtime, status);
CREATE INDEX tasks_status_updated_idx ON tasks (status, updated_at, number);

-- Undelivered steer messages for a running task, ordered by id (delivered
-- steers are deleted; their content lives on as task.message events). Capped
-- per task at the API (20).
CREATE TABLE task_steers (
    id BIGSERIAL PRIMARY KEY,
    task_number BIGINT NOT NULL REFERENCES tasks (number) ON DELETE CASCADE,
    message TEXT NOT NULL
);
CREATE INDEX task_steers_task_number_idx ON task_steers (task_number, id);

-- Agent runtime and task events (previously events.jsonl). seq is a serial:
-- an aborted transaction burns a value, and since-based pagination only needs
-- seq to be unique and increasing. The API's event_id "event_<seq>" is
-- derived on read.
CREATE TABLE agent_events (
    seq BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    task_id TEXT,
    -- The payload fields, typed: task.message events carry message+source,
    -- task.failed carries error_message, agent_runtime.* carries
    -- agent_runtime, and lifecycle events carry nothing.
    message TEXT,
    source TEXT CHECK (source IN ('user', 'agent')),
    error_message TEXT,
    agent_runtime TEXT
);
CREATE INDEX agent_events_task_id_idx ON agent_events (task_id, seq);

-- User thread -> provider session/thread mappings (previously the
-- codex_threads and claude_sessions maps in state.json).
CREATE TABLE thread_sessions (
    agent_runtime TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    provider_session_id TEXT,
    last_used_at TEXT,
    PRIMARY KEY (agent_runtime, thread_id)
);
-- LRU pruning of the mapping caps.
CREATE INDEX thread_sessions_last_used_idx ON thread_sessions (agent_runtime, last_used_at);

-- In-flight OAuth logins, one per provider flow: Codex uses a device-code
-- flow (device_code + a login-server handle), Claude a browser-code flow.
CREATE TABLE oauth_logins (
    runtime TEXT PRIMARY KEY CHECK (runtime IN ('codex', 'claude')),
    status TEXT NOT NULL,
    login_url TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    device_code TEXT,
    login_id TEXT,
    CHECK (
        (runtime = 'codex' AND device_code IS NOT NULL AND login_id IS NOT NULL)
        OR (runtime = 'claude' AND device_code IS NULL AND login_id IS NULL)
    )
);

-- Admin-side provider account records. account_id is promoted for direct
-- queries; metadata holds the provider CLI's own evolving shape (usage
-- blocks, plan fields, organization data) cached verbatim — deliberately not
-- typed here, because its schema belongs to the provider and changes with
-- CLI versions; the runtime treats it as opaque display metadata.
CREATE TABLE provider_accounts (
    provider TEXT PRIMARY KEY CHECK (provider IN ('openai', 'claude')),
    account_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Network allow/deny decisions (previously proxy-owned network_events.jsonl).
-- The proxy service writes them under its own role; that role gets exactly
-- this table and nothing else — the enforcement inputs (network policy,
-- account pins, CA material) stay proxy-owned files, and the rest of admin
-- state stays out of the proxy's reach. The role is created by bootstrap
-- (and the test harness) before migrations run.
CREATE TABLE network_events (
    seq BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    protocol TEXT NOT NULL,
    method TEXT NOT NULL,
    host TEXT NOT NULL,
    port BIGINT NOT NULL,
    path TEXT NOT NULL,
    query TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT
);
GRANT SELECT, INSERT, DELETE ON network_events TO "trustyclaw-proxy";
GRANT USAGE, SELECT ON SEQUENCE network_events_seq_seq TO "trustyclaw-proxy";

-- The active network policy, as rows: the shape is defined by
-- host/config.py, so its parts are typed. The admin service (schema owner)
-- replaces all of it in one transaction after validation; the proxy only
-- reads. A missing network_policy row means the fail-closed default (empty
-- policy) — nothing seeds these. Managed provider access is presence-based
-- (a row means enabled); method and guard lists keep their operator-given
-- order via position.
CREATE TABLE network_policy (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    updated_at TEXT NOT NULL
);
GRANT SELECT ON network_policy TO "trustyclaw-proxy";

CREATE TABLE managed_provider_access (
    provider TEXT PRIMARY KEY CHECK (provider IN ('openai', 'claude'))
);
GRANT SELECT ON managed_provider_access TO "trustyclaw-proxy";

CREATE TABLE allowed_domains (
    domain TEXT PRIMARY KEY CHECK (domain <> '')
);
GRANT SELECT ON allowed_domains TO "trustyclaw-proxy";

CREATE TABLE domain_methods (
    domain TEXT NOT NULL REFERENCES allowed_domains (domain) ON DELETE CASCADE,
    position BIGINT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE')),
    PRIMARY KEY (domain, position)
);
GRANT SELECT ON domain_methods TO "trustyclaw-proxy";

CREATE TABLE domain_path_guards (
    domain TEXT NOT NULL REFERENCES allowed_domains (domain) ON DELETE CASCADE,
    position BIGINT NOT NULL,
    pattern TEXT NOT NULL CHECK (pattern <> ''),
    PRIMARY KEY (domain, position)
);
GRANT SELECT ON domain_path_guards TO "trustyclaw-proxy";

-- The provider account pins the proxy guards check: exactly the two values
-- the guards compare; the proxy never receives the rest of the account
-- metadata. A missing row means no pin — fail closed.
CREATE TABLE proxy_provider_pins (
    provider TEXT PRIMARY KEY CHECK (provider IN ('openai', 'claude')),
    account_id TEXT,
    access_token_sha256 TEXT CHECK (access_token_sha256 ~ '^[0-9a-f]{64}$')
);
GRANT SELECT ON proxy_provider_pins TO "trustyclaw-proxy";

-- migrate:down
DROP TABLE proxy_provider_pins;
DROP TABLE domain_path_guards;
DROP TABLE domain_methods;
DROP TABLE allowed_domains;
DROP TABLE managed_provider_access;
DROP TABLE network_policy;
DROP TABLE network_events;
DROP TABLE provider_accounts;
DROP TABLE oauth_logins;
DROP TABLE thread_sessions;
DROP TABLE agent_events;
DROP TABLE task_steers;
DROP TABLE tasks;
DROP TABLE counters;
DROP TABLE operator_connections;
DROP TABLE config;
