-- App-owned preferences and thread reference state for the independent Agent Chat UI.

-- migrate:up

CREATE TABLE IF NOT EXISTS preferences (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    density TEXT NOT NULL DEFAULT 'comfortable' CHECK (density IN ('compact', 'comfortable', 'spacious')),
    show_completed BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    agent_runtime TEXT NOT NULL CHECK (agent_runtime IN ('codex', 'claude_code')),
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thread_tasks (
    task_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_thread_tasks_thread_id ON thread_tasks(thread_id);

-- migrate:down

DROP TABLE IF EXISTS thread_tasks;
DROP TABLE IF EXISTS threads;
DROP TABLE IF EXISTS preferences;
