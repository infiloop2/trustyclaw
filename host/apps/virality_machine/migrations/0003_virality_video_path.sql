-- The workspace file is durable app state. Short-lived staged asset ids remain
-- transport between adjacent MCP calls and do not belong in the render queue.

-- migrate:up

ALTER TABLE render_jobs RENAME COLUMN video_asset_id TO video_path;

-- migrate:down

ALTER TABLE render_jobs RENAME COLUMN video_path TO video_asset_id;
