-- Managed network integrations and the GitHub integration state.
--
-- Broadens managed provider access into managed integrations (OpenAI, Claude,
-- GitHub, Python packages, npm packages) with GitHub's repository scope as
-- typed rows, and adds the single fixed GitHub credential: either a pasted
-- fine-grained PAT or a GitHub App whose short-lived installation tokens the
-- host mints itself. The proxy role reads the policy tables it enforces from;
-- it gets no grant on the credential — the network guard only decides
-- repository reachability and never needs the secret.
--
-- proxy_github_token is the proxy's working copy of the active GitHub token
-- (the proxy_provider_pins pattern): the admin service writes it on
-- mint/set/replace and clears it on disable or delete, and the proxy injects
-- it into policy-approved GitHub requests so the agent never holds the
-- credential. The token column holds secretbox ciphertext like every other
-- stored secret, so the proxy also gets SELECT on secret_keys to decrypt its
-- own row. Grants stay per-table: the key row alone decrypts nothing else,
-- because the credential row (App PEM key, PAT storage) keeps no proxy
-- grant, so the proxy's reach stays exactly this working set — in app mode
-- a short-lived installation token refreshed hourly, in pat mode the PAT.
--
-- github_repo_audit stores per-repository facts fetched by the
-- audit-github-repo root helper (visibility, the token's effective
-- permissions, default-branch protection, workflows and their triggers).
-- Facts live here; the warning judgments live in code, so message changes
-- never need a re-audit. Admin-owned, no proxy grant.

-- migrate:up

-- Presence-based like managed_provider_access before it: a row means the
-- integration is enabled.
CREATE TABLE managed_integrations (
    integration TEXT PRIMARY KEY
        CHECK (integration IN ('openai', 'claude', 'github', 'python_packages', 'npm_packages'))
);
GRANT SELECT ON managed_integrations TO "trustyclaw-proxy";

-- The repositories the agent may write to (push and mutate through the API),
-- in operator-given order. Reads are universal when GitHub is enabled, so this
-- list only names write targets; owner/repo are stored normalized (lowercase)
-- as host/config.py validates them.
CREATE TABLE github_repositories (
    position BIGINT PRIMARY KEY,
    owner TEXT NOT NULL CHECK (owner ~ '^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$'),
    repo TEXT NOT NULL CHECK (repo ~ '^[a-z0-9._-]{1,100}$' AND repo !~ '\.git$'),
    UNIQUE (owner, repo)
);
GRANT SELECT ON github_repositories TO "trustyclaw-proxy";

INSERT INTO managed_integrations (integration)
    SELECT provider FROM managed_provider_access;
DROP TABLE managed_provider_access;

-- Managed-integration domains are reserved from raw rules from now on, and a
-- stored policy that still lists them would fail validation — denying all
-- agent egress on an upgraded host. Drop the old raw preset rows for these
-- domains so the stored policy validates again. Deliberately no
-- auto-activation: an upgrade never turns a managed integration on by
-- inference — the operator re-enables the ones they want through the admin
-- UI. Domain deletes cascade to their method and path-guard rows.
DELETE FROM allowed_domains
    WHERE domain IN (
        'openai.com', 'chatgpt.com', 'anthropic.com', 'claude.ai', 'claude.com',
        'github.com', 'githubusercontent.com',
        'pypi.org', 'pythonhosted.org', 'npmjs.org', 'nodejs.org'
    )
    OR domain LIKE '%.openai.com' OR domain LIKE '%.chatgpt.com'
    OR domain LIKE '%.anthropic.com' OR domain LIKE '%.claude.ai' OR domain LIKE '%.claude.com'
    OR domain LIKE '%.github.com' OR domain LIKE '%.githubusercontent.com'
    OR domain LIKE '%.pypi.org' OR domain LIKE '%.pythonhosted.org'
    OR domain LIKE '%.npmjs.org' OR domain LIKE '%.nodejs.org'
    -- Broad wildcards that *cover* a managed apex ('*.com' matches
    -- api.github.com) are also rejected by the new validation, so a stored
    -- one would deny all egress on an upgraded host the same way.
    OR domain IN ('*.com', '*.ai', '*.org');

-- Key for at-rest encryption of stored secrets (host.runtime.secretbox).
-- Kept in the database so all admin state lives in one place, and created
-- right here so the key exists from the moment the schema does — no lazy
-- first-encrypt path in code. gen_random_uuid() draws from Postgres's CSPRNG
-- (pg_strong_random); two UUIDs with dashes stripped give the 64 hex chars.
-- This is an accidental-exposure control: a stray SELECT * on a
-- secret-bearing table no longer reveals credential material. A full dump of
-- the database necessarily includes this key. No proxy grant.
CREATE TABLE secret_keys (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    key_hex TEXT NOT NULL CHECK (key_hex ~ '^[0-9a-f]{64}$')
);
INSERT INTO secret_keys (singleton, key_hex)
    VALUES (TRUE, translate(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''));

-- One fixed GitHub credential (admin-owned; no proxy grant). pat mode stores
-- the pasted token; app mode stores the GitHub App identity and signing key.
-- The working token the App mints lives only in proxy_github_token below.
-- Secret columns (token, private_key_pem) hold host.runtime.secretbox
-- ciphertext, so database contents alone never expose credential material;
-- the checks are therefore shape-light.
CREATE TABLE github_credential (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    mode TEXT NOT NULL CHECK (mode IN ('pat', 'app')),
    token TEXT CHECK (token IS NULL OR (token <> '' AND token !~ '\s')),
    app_id TEXT CHECK (app_id IS NULL OR app_id ~ '^[0-9]{1,20}$'),
    installation_id TEXT CHECK (installation_id IS NULL OR installation_id ~ '^[0-9]{1,20}$'),
    private_key_pem TEXT CHECK (private_key_pem IS NULL OR private_key_pem <> ''),
    updated_at TEXT NOT NULL,
    validation JSONB NOT NULL,
    CHECK (
        (mode = 'pat' AND token IS NOT NULL
            AND app_id IS NULL AND installation_id IS NULL AND private_key_pem IS NULL)
        OR
        (mode = 'app' AND token IS NULL
            AND app_id IS NOT NULL AND installation_id IS NOT NULL AND private_key_pem IS NOT NULL)
    )
);

CREATE TABLE proxy_github_token (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    token TEXT NOT NULL CHECK (token <> '' AND token !~ '\s'),
    -- App-mode expiry of the minted token (NULL for a PAT): the admin
    -- service re-mints before it passes. This row is the only copy of the
    -- working token — there is no separate mint cache.
    expires_at TEXT,
    updated_at TEXT NOT NULL
);
GRANT SELECT ON proxy_github_token TO "trustyclaw-proxy";
GRANT SELECT ON secret_keys TO "trustyclaw-proxy";

CREATE TABLE github_repo_audit (
    owner TEXT NOT NULL CHECK (owner ~ '^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$'),
    repo TEXT NOT NULL CHECK (repo ~ '^[a-z0-9._-]{1,100}$' AND repo !~ '\.git$'),
    fetched_at TEXT NOT NULL,
    facts JSONB NOT NULL,
    error TEXT CHECK (error IS NULL OR error <> ''),
    PRIMARY KEY (owner, repo)
);

-- migrate:down

DROP TABLE github_repo_audit;
DROP TABLE proxy_github_token;
DROP TABLE github_credential;
-- Rolled-back code expects plaintext tunnel tokens, and SQL cannot decrypt
-- secretbox values (the cipher lives in the openssl CLI; the key is dropped
-- below). Remove Cloudflare connections whose token is ciphertext so the
-- loss is explicit: the downgraded host keeps SSH access, and the operator
-- re-adds Cloudflare through deploy/reconfigure — instead of bootstrap
-- failing on an undecryptable token.
DELETE FROM operator_connections
    WHERE mode = 'cloudflare_access' AND tunnel_token LIKE 'enc:v1:%';
DROP TABLE secret_keys;

CREATE TABLE managed_provider_access (
    provider TEXT PRIMARY KEY CHECK (provider IN ('openai', 'claude'))
);
GRANT SELECT ON managed_provider_access TO "trustyclaw-proxy";
INSERT INTO managed_provider_access (provider)
    SELECT integration FROM managed_integrations WHERE integration IN ('openai', 'claude');

DROP TABLE github_repositories;
DROP TABLE managed_integrations;
