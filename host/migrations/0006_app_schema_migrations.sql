-- Host-owned records for app migration versions.
--
-- App migration SQL runs under the app database role and app schema, but the
-- record of which app versions have been applied stays host-owned so an app
-- cannot mark its own failed migration as complete.

-- migrate:up

CREATE TABLE app_schema_migrations (
    app_id TEXT NOT NULL CHECK (app_id ~ '^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$'),
    version BIGINT NOT NULL,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, version)
);

-- migrate:down

DROP TABLE app_schema_migrations;
