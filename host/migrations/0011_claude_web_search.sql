-- Claude integration-level web-search toggle, in a singleton row (not the
-- presence-only managed_integrations table), mirroring github_settings. The
-- proxy reads it back into the policy it reconstructs so the Anthropic
-- server-tool guard can allow web search only when the operator enabled it.
-- Default false: web search stays off unless explicitly turned on.

-- migrate:up

CREATE TABLE claude_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    web_search BOOLEAN NOT NULL DEFAULT FALSE
);
GRANT SELECT ON claude_settings TO "trustyclaw-proxy";

-- migrate:down

DROP TABLE claude_settings;
