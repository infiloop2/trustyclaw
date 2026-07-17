-- Virality Machine domain table: the render queue. One row per Runway generation or
-- editing job the agent starts, upserted as the agent polls runway_get_task to a
-- terminal status. The operator render-queue UI reads it through GET /api/render_jobs.
-- Every field the agent writes is bounded in backend.py before it reaches this table;
-- the enum CHECKs below are defense in depth against a damaged write path.

-- migrate:up

CREATE TABLE IF NOT EXISTS render_jobs (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('video', 'edit', 'image', 'speech')),
    prompt TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
    output_url TEXT,
    video_asset_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_render_jobs_updated ON render_jobs(updated_at DESC);

-- migrate:down

DROP TABLE IF EXISTS render_jobs;
