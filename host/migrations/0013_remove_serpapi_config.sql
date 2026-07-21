-- LinkedIn Discovery now uses Serper. The old provider credential cannot be
-- used or converted, so remove its encrypted tool-config row during upgrade.

-- migrate:up

DELETE FROM tool_config
WHERE tool_id = 'linkedin_discovery' AND key = 'SERPAPI_API_KEY';

-- migrate:down

-- Deleted ciphertext cannot be reconstructed. Rolling back the code requires
-- the operator to configure the old provider again.
SELECT 1;
