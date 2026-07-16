-- Keep the exact bounded arguments for tool calls in their host-owned audit
-- record. NULL distinguishes lifecycle events such as enablement and OAuth
-- connection changes from a tool action whose valid arguments are `{}`.

-- migrate:up
ALTER TABLE tool_events ADD COLUMN arguments JSONB;

-- migrate:down
ALTER TABLE tool_events DROP COLUMN arguments;
