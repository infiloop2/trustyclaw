-- The workspace runtime constraint tracks the host's supported runtimes: the
-- session-options matrix now offers pi and hermes, so selecting them must
-- store instead of failing the workspace update with a check violation.

-- migrate:up

ALTER TABLE workspace DROP CONSTRAINT workspace_agent_runtime_check;
ALTER TABLE workspace ADD CONSTRAINT workspace_agent_runtime_check
    CHECK (agent_runtime IN ('codex', 'claude_code', 'pi', 'hermes'));

-- migrate:down

UPDATE workspace SET agent_runtime = NULL, model = NULL, effort = NULL
    WHERE agent_runtime NOT IN ('codex', 'claude_code');
ALTER TABLE workspace DROP CONSTRAINT workspace_agent_runtime_check;
ALTER TABLE workspace ADD CONSTRAINT workspace_agent_runtime_check
    CHECK (agent_runtime IN ('codex', 'claude_code'));
