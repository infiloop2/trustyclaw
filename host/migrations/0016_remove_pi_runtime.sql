-- Remove the Pi runtime and collapse Bedrock usage to the sole remaining
-- Hermes runtime. Historical Pi tasks, sessions, events, and usage are
-- deliberately deleted instead of retaining an unsupported runtime shape.

-- migrate:up

DELETE FROM agent_events
WHERE agent_runtime = 'pi'
   OR task_id IN (
       SELECT 'task_' || number FROM tasks
       WHERE thread_id IN (
           SELECT thread_id FROM thread_sessions WHERE agent_runtime = 'pi'
       )
   );
DELETE FROM tasks WHERE thread_id IN (
    SELECT thread_id FROM thread_sessions WHERE agent_runtime = 'pi'
);
DELETE FROM thread_sessions WHERE agent_runtime = 'pi';

ALTER TABLE thread_sessions DROP CONSTRAINT thread_sessions_options_check;
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
    OR
    (
        agent_runtime = 'hermes'
        AND model IN ('deepseek.v3.2', 'qwen.qwen3-coder-next', 'moonshotai.kimi-k2.5')
        AND effort = 'high'
    )
);

DELETE FROM bedrock_usage WHERE runtime = 'pi';
ALTER TABLE bedrock_usage DROP CONSTRAINT bedrock_usage_pkey;
ALTER TABLE bedrock_usage DROP CONSTRAINT bedrock_usage_runtime_check;
ALTER TABLE bedrock_usage DROP COLUMN runtime;
ALTER TABLE bedrock_usage ADD PRIMARY KEY (model_id, day);

-- migrate:down

ALTER TABLE bedrock_usage DROP CONSTRAINT bedrock_usage_pkey;
ALTER TABLE bedrock_usage ADD COLUMN runtime TEXT NOT NULL DEFAULT 'hermes';
ALTER TABLE bedrock_usage ALTER COLUMN runtime DROP DEFAULT;
ALTER TABLE bedrock_usage ADD CONSTRAINT bedrock_usage_runtime_check
    CHECK (runtime IN ('pi', 'hermes'));
ALTER TABLE bedrock_usage ADD PRIMARY KEY (runtime, model_id, day);

ALTER TABLE thread_sessions DROP CONSTRAINT thread_sessions_options_check;
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
    OR
    (
        agent_runtime = 'pi'
        AND model IN ('deepseek.v3.2', 'qwen.qwen3-coder-next', 'moonshotai.kimi-k2.5')
        AND effort IN ('medium', 'high', 'max')
    )
    OR
    (
        agent_runtime = 'hermes'
        AND model IN ('deepseek.v3.2', 'qwen.qwen3-coder-next', 'moonshotai.kimi-k2.5')
        AND effort = 'high'
    )
);
