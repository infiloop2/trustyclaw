-- Persist the operator-selected model and effort on the durable thread. Tasks
-- join this canonical row for execution and API responses.

-- migrate:up

ALTER TABLE thread_sessions ADD COLUMN model TEXT;
ALTER TABLE thread_sessions ADD COLUMN effort TEXT;

UPDATE thread_sessions
SET model = CASE agent_runtime
        WHEN 'codex' THEN 'gpt-5.6-terra'
        WHEN 'claude_code' THEN 'opus'
    END,
    effort = 'high';

-- Legacy session rows were created only after successful provider turns and
-- could be pruned independently of retained tasks. Backfill every missing
-- task thread before adding the foreign key and removing tasks.agent_runtime.
INSERT INTO thread_sessions (
    agent_runtime, thread_id, provider_session_id, last_used_at, model, effort
)
SELECT
    agent_runtime,
    thread_id,
    NULL,
    max(updated_at),
    CASE agent_runtime
        WHEN 'codex' THEN 'gpt-5.6-terra'
        WHEN 'claude_code' THEN 'opus'
    END,
    'high'
FROM tasks
GROUP BY agent_runtime, thread_id
ON CONFLICT (agent_runtime, thread_id) DO NOTHING;

ALTER TABLE thread_sessions ALTER COLUMN model SET NOT NULL;
ALTER TABLE thread_sessions ALTER COLUMN effort SET NOT NULL;
ALTER TABLE thread_sessions ADD CONSTRAINT thread_sessions_options_check CHECK (
    (
        agent_runtime = 'codex'
        AND model IN ('gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-luna')
        AND effort IN ('high', 'max', 'ultra')
        AND NOT (model = 'gpt-5.6-luna' AND effort = 'ultra')
    )
    OR
    (
        agent_runtime = 'claude_code'
        AND model IN ('opus', 'fable', 'sonnet')
        AND effort IN ('high', 'max', 'ultracode')
    )
);

-- A thread id is the single session identity. Tasks reference it and derive
-- all session configuration through this row.
ALTER TABLE thread_sessions DROP CONSTRAINT thread_sessions_pkey;
ALTER TABLE thread_sessions ADD PRIMARY KEY (thread_id);
ALTER TABLE tasks ADD CONSTRAINT tasks_thread_id_fkey
    FOREIGN KEY (thread_id) REFERENCES thread_sessions (thread_id);
DROP INDEX tasks_runtime_status_idx;
ALTER TABLE tasks DROP COLUMN agent_runtime;

-- migrate:down

ALTER TABLE tasks ADD COLUMN agent_runtime TEXT;
UPDATE tasks
SET agent_runtime = thread_sessions.agent_runtime
FROM thread_sessions
WHERE thread_sessions.thread_id = tasks.thread_id;
ALTER TABLE tasks ALTER COLUMN agent_runtime SET NOT NULL;
CREATE INDEX tasks_runtime_status_idx ON tasks (agent_runtime, status);
ALTER TABLE tasks DROP CONSTRAINT tasks_thread_id_fkey;
ALTER TABLE thread_sessions DROP CONSTRAINT thread_sessions_pkey;
ALTER TABLE thread_sessions ADD PRIMARY KEY (agent_runtime, thread_id);
ALTER TABLE thread_sessions DROP CONSTRAINT thread_sessions_options_check;
-- The up migration creates one null-id mapping for each retained legacy task
-- that had never started a provider conversation. Remove those synthetic
-- mappings before returning to the schema where tasks alone owned that state.
DELETE FROM thread_sessions WHERE provider_session_id IS NULL;
ALTER TABLE thread_sessions DROP COLUMN effort;
ALTER TABLE thread_sessions DROP COLUMN model;
