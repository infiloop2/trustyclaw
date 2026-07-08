-- The .github push-approval gate. Two pieces of state:
--
-- github_settings: the integration-level toggle, in a singleton row (not the
-- presence-only managed_integrations table). The proxy reads it back into the
-- policy it reconstructs. Later integration-level GitHub toggles add columns
-- here.
--
-- pending_pushes: a gated push that changed a .github/ path, held for operator
-- approval. The proxy writes rows (it detects the change and quarantines the
-- objects in its own mirror), the same way it writes network_events; the admin
-- service reads them, and on approve/reject updates the status. The pushed
-- objects live under refs/pending/<id> in the proxy's quarantine mirror (on
-- disk, root-readable), not in this table.

-- migrate:up

CREATE TABLE github_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    require_dot_github_approval BOOLEAN NOT NULL DEFAULT FALSE
);
GRANT SELECT ON github_settings TO "trustyclaw-proxy";

CREATE TABLE pending_pushes (
    id TEXT PRIMARY KEY CHECK (id ~ '^[a-f0-9]{6,64}$'),
    owner TEXT NOT NULL CHECK (owner ~ '^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$'),
    repo TEXT NOT NULL CHECK (repo ~ '^[a-z0-9._-]{1,100}$' AND repo !~ '\.git$'),
    ref_updates JSONB NOT NULL,
    changed_paths JSONB NOT NULL,
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'resolving', 'approved', 'rejected', 'failed')),
    claimed_at TEXT,
    resolved_at TEXT,
    detail TEXT CHECK (detail IS NULL OR detail <> '')
);
-- The proxy enqueues; the admin service lists and resolves. INSERT mirrors the
-- proxy's existing network_events grant.
GRANT INSERT ON pending_pushes TO "trustyclaw-proxy";

-- migrate:down

DROP TABLE pending_pushes;
DROP TABLE github_settings;
