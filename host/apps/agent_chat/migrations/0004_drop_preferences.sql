-- The preferences feature (density, show_completed) is gone from the Agent
-- Chat backend and UI; drop its orphaned singleton table.

-- migrate:up

DROP TABLE IF EXISTS preferences;

-- migrate:down

CREATE TABLE IF NOT EXISTS preferences (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    density TEXT NOT NULL DEFAULT 'comfortable' CHECK (density IN ('compact', 'comfortable', 'spacious')),
    show_completed BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TEXT NOT NULL
);
