-- First-capture binding: Claude login completion records sha256 of the access
-- token the operator's login flow wrote, so agent credentials swapped after
-- completion do not inherit the approval. Codex rows never set it (Codex
-- capture is bound to the completed device-login id instead).

-- migrate:up

ALTER TABLE oauth_logins ADD COLUMN access_token_sha256 TEXT;

-- migrate:down

ALTER TABLE oauth_logins DROP COLUMN access_token_sha256;
