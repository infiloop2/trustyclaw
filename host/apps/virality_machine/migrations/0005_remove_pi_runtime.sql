-- migrate:up

UPDATE schedules SET enabled = FALSE
    WHERE EXISTS (SELECT 1 FROM workspace WHERE agent_runtime = 'pi');
UPDATE workspace SET agent_runtime = NULL, model = NULL, effort = NULL
    WHERE agent_runtime = 'pi';
ALTER TABLE workspace DROP CONSTRAINT workspace_agent_runtime_check;
ALTER TABLE workspace ADD CONSTRAINT workspace_agent_runtime_check
    CHECK (agent_runtime IN ('codex', 'claude_code', 'hermes'));

-- migrate:down

ALTER TABLE workspace DROP CONSTRAINT workspace_agent_runtime_check;
ALTER TABLE workspace ADD CONSTRAINT workspace_agent_runtime_check
    CHECK (agent_runtime IN ('codex', 'claude_code', 'pi', 'hermes'));
