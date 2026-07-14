-- The operator-approved provider account anchor is immutable at the database.
-- An anchored row (account_id plus its provider's approval marker) accepts
-- only two kinds of writes: metadata rewrites that keep the same anchored
-- account (every active refresh re-saves the row), and the operator reset
-- that clears the row to no account. No single statement can move an anchored
-- account_id to a different account or strip its approval marker, and an
-- anchored row cannot be deleted. Re-anchoring is therefore mechanically
-- forced through reset-then-login. This backs the orchestrator's trust checks
-- with a guarantee that holds against future refresh-path bugs.

-- migrate:up

CREATE FUNCTION provider_account_anchor_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_anchored boolean;
    new_anchored boolean;
BEGIN
    -- COALESCE keeps every branch two-valued: a missing marker key yields
    -- SQL NULL, and a NULL condition would silently skip the guard.
    old_anchored := OLD.account_id IS NOT NULL AND (
        (OLD.provider = 'claude' AND COALESCE(OLD.metadata->>'identity_attestation', '') = 'anthropic_oauth_profile')
        OR (OLD.provider = 'openai' AND COALESCE(OLD.metadata->>'operator_approval', '') = 'codex_device_login')
    );
    IF NOT old_anchored THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'provider account anchor for % cannot be deleted; clear it with a linked-account reset', OLD.provider;
    END IF;
    IF NEW.account_id IS NULL THEN
        RETURN NEW;  -- the operator reset clearing the anchor
    END IF;
    new_anchored := (NEW.provider = 'claude' AND COALESCE(NEW.metadata->>'identity_attestation', '') = 'anthropic_oauth_profile')
        OR (NEW.provider = 'openai' AND COALESCE(NEW.metadata->>'operator_approval', '') = 'codex_device_login');
    IF NEW.account_id <> OLD.account_id OR NOT new_anchored THEN
        RAISE EXCEPTION 'provider account anchor for % is immutable; reset the linked account before anchoring another account', OLD.provider;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER provider_accounts_anchor_guard
BEFORE UPDATE OR DELETE ON provider_accounts
FOR EACH ROW EXECUTE FUNCTION provider_account_anchor_guard();

-- migrate:down

DROP TRIGGER provider_accounts_anchor_guard ON provider_accounts;
DROP FUNCTION provider_account_anchor_guard();
