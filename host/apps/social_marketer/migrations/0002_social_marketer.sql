-- Social Marketer domain tables. The posts table is the single source of truth
-- shared by the agent (upsert_post / set_post_status actions) and the operator
-- composer (GET/POST /api/posts). Body length is capped by encoded bytes per
-- platform in the backend before any write; the enum CHECKs here are a second
-- structural guard. Campaign plans and performance summaries stay as free-form
-- workspace artifacts.

-- migrate:up

CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL CHECK (platform IN ('x', 'linkedin')),
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'posted')),
    scheduled_for TEXT,
    external_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_posts_scheduled_for ON posts(scheduled_for);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);

-- migrate:down

DROP TABLE IF EXISTS posts;
