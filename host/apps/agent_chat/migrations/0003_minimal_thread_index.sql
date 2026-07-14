-- Session configuration belongs to the host. Agent Chat keeps only its own
-- thread index, archive state, and task references.

-- migrate:up

ALTER TABLE threads DROP COLUMN agent_runtime;
ALTER TABLE threads DROP COLUMN created_at;
ALTER TABLE threads DROP COLUMN updated_at;
ALTER TABLE thread_tasks DROP COLUMN created_at;

-- migrate:down

-- Version 2 required a runtime value that version 3 deliberately no longer
-- stores. Clear version-3 references rather than inventing configuration.
DELETE FROM thread_tasks;
DELETE FROM threads;
ALTER TABLE threads ADD COLUMN agent_runtime TEXT NOT NULL
    CHECK (agent_runtime IN ('codex', 'claude_code'));
ALTER TABLE threads ADD COLUMN created_at TEXT NOT NULL;
ALTER TABLE threads ADD COLUMN updated_at TEXT NOT NULL;
ALTER TABLE thread_tasks ADD COLUMN created_at TEXT NOT NULL;
