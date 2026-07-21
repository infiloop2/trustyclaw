-- One AWS Bedrock integration serves the Pi and Hermes task
-- runtimes. Its region, one durable IAM credential, and derived account/spend
-- record are all shared.

-- migrate:up

-- The validated operator-connected credential. The access key id is public
-- SigV4 metadata; the secret access key is secretbox ciphertext. The proxy
-- reads this same row and decrypts the secret only for an enabled Bedrock
-- request.
CREATE TABLE bedrock_credentials (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    access_key_id TEXT NOT NULL,
    secret_access_key_encrypted TEXT NOT NULL,
    region TEXT NOT NULL CHECK (region IN ('us-east-1', 'us-east-2', 'us-west-2'))
);
GRANT SELECT ON bedrock_credentials TO "trustyclaw-proxy";

ALTER TABLE managed_integrations DROP CONSTRAINT managed_integrations_integration_check;
ALTER TABLE managed_integrations ADD CONSTRAINT managed_integrations_integration_check
    CHECK (integration IN ('openai', 'claude', 'bedrock', 'github', 'python_packages', 'npm_packages'));
ALTER TABLE provider_accounts DROP CONSTRAINT provider_accounts_provider_check;
ALTER TABLE provider_accounts ADD CONSTRAINT provider_accounts_provider_check
    CHECK (provider IN ('openai', 'claude', 'bedrock'));

-- The thread-session option matrix (host/session_options.py) now offers the
-- pi and hermes runtimes; without extending this check, the first Bedrock
-- task would fail its thread_sessions insert with a check violation.
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

-- migrate:down

-- Tasks reference thread_sessions (tasks_thread_id_fkey), so the Bedrock
-- runtimes' tasks go first (task_steers cascade), then their sessions.
DELETE FROM tasks WHERE thread_id IN (
    SELECT thread_id FROM thread_sessions WHERE agent_runtime IN ('pi', 'hermes')
);
DELETE FROM thread_sessions WHERE agent_runtime IN ('pi', 'hermes');
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
);

DELETE FROM provider_accounts WHERE provider = 'bedrock';
ALTER TABLE provider_accounts DROP CONSTRAINT provider_accounts_provider_check;
ALTER TABLE provider_accounts ADD CONSTRAINT provider_accounts_provider_check
    CHECK (provider IN ('openai', 'claude'));
DELETE FROM managed_integrations WHERE integration = 'bedrock';
ALTER TABLE managed_integrations DROP CONSTRAINT managed_integrations_integration_check;
ALTER TABLE managed_integrations ADD CONSTRAINT managed_integrations_integration_check
    CHECK (integration IN ('openai', 'claude', 'github', 'python_packages', 'npm_packages'));

DROP TABLE bedrock_credentials;
