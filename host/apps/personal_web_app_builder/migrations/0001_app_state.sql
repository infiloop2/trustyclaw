-- The builder owns one agent-defined app bundle and its structured JSON data.
-- The host remains authoritative for the app-scoped builder thread.

-- migrate:up

CREATE TABLE app_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    html TEXT NOT NULL DEFAULT '',
    css TEXT NOT NULL DEFAULT '',
    javascript TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

INSERT INTO app_state (singleton, updated_at)
VALUES (TRUE, '1970-01-01T00:00:00Z');

-- migrate:down

DROP TABLE app_state;
