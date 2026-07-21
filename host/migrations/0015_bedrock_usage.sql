-- Live Bedrock usage counters, written by the network proxy. AWS reports
-- authoritative token usage in every allowed Converse/ConverseStream
-- response; the proxy adds it, together with the USD cost it prices from the
-- catalog at that moment, to one counter row per (runtime, model, UTC day).
-- The recorded cost is final: it is priced when the response is metered, not
-- recomputed at read time, so a later rate edit only affects subsequently
-- metered requests and never rewrites history. Growth is bounded by
-- arithmetic, not pruning: models outside the price table collapse into a
-- single 'other' bucket, so 2 runtimes x (3 catalog models + 1 bucket) x 366
-- days is ~2.9k rows per year.

-- migrate:up

CREATE TABLE bedrock_usage (
    runtime TEXT NOT NULL CHECK (runtime IN ('pi', 'hermes')),
    -- The invoked model id, normalized to the price catalog before recording:
    -- a catalog id is kept as-is, anything else collapses into 'other'. That
    -- bounds the row count and stops a buggy or adversarial agent from
    -- creating unbounded rows by looping over random model ids.
    model_id TEXT NOT NULL CHECK (model_id <> '' AND length(model_id) <= 256),
    day DATE NOT NULL,
    -- requests counts every allowed, forwarded invocation; metered_requests
    -- only those whose response carried a parseable usage record. The gap is
    -- the fail-visible signal for AWS errors and unparsed responses.
    requests BIGINT NOT NULL DEFAULT 0 CHECK (requests >= 0),
    metered_requests BIGINT NOT NULL DEFAULT 0 CHECK (metered_requests >= 0),
    input_tokens BIGINT NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens BIGINT NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cache_read_tokens BIGINT NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens BIGINT NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    -- USD cost accumulated at record time from the manifest price table, fixed
    -- to 6 decimal places. Final once written: read paths sum this column
    -- instead of re-pricing tokens, so historical cost is stable across rate
    -- edits. A model outside the catalog contributes 0 (its tokens still
    -- count) rather than an estimate at a guessed rate.
    cost_usd NUMERIC(18, 6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    PRIMARY KEY (runtime, model_id, day)
);
-- The proxy increments with INSERT ... ON CONFLICT DO UPDATE, which reads the
-- conflicting row, so it needs SELECT alongside INSERT and UPDATE.
GRANT SELECT, INSERT, UPDATE ON bedrock_usage TO "trustyclaw-proxy";

-- migrate:down

DROP TABLE bedrock_usage;
