-- Agent network introspection. Denials are identified by one stable
-- snake_case code:
--
-- network_events.reason_code replaces the free-text reason column. The code
-- is what the proxy returns in the 403 body and what the denial catalogs in
-- the integration manifests document with guidance; a parallel prose message
-- carried nothing the code and catalog do not. Old rows' prose is dropped
-- with the column (network events are a pruned log, not durable state).
--
-- trustyclaw-agent-network SELECT grants: the dedicated non-egress service serves the agent-facing
-- list_network_integrations and recent_network_denials tools, which read the
-- stored network policy (the same tables the proxy reconstructs it from) and
-- the denial log. Policy and events are operator config and the agent's own
-- traffic decisions — no secret material lives in any of these tables.

-- migrate:up

ALTER TABLE network_events DROP COLUMN reason;
ALTER TABLE network_events ADD COLUMN reason_code TEXT;
GRANT SELECT ON network_policy, managed_integrations, github_repositories,
    github_settings, claude_settings, allowed_domains, domain_methods,
    domain_path_guards, network_events TO "trustyclaw-agent-network";

-- migrate:down

REVOKE SELECT ON network_policy, managed_integrations, github_repositories,
    github_settings, claude_settings, allowed_domains, domain_methods,
    domain_path_guards, network_events FROM "trustyclaw-agent-network";
ALTER TABLE network_events DROP COLUMN reason_code;
ALTER TABLE network_events ADD COLUMN reason TEXT;
