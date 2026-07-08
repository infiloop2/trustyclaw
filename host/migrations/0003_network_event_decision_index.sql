-- The network audit log pages newest-first with an optional decision filter.
-- Unfiltered pages walk the primary key backwards, but a filtered page
-- (WHERE decision = ... ORDER BY seq DESC LIMIT n) otherwise scans and
-- discards non-matching rows across a table capped at a million entries.
-- (decision, seq) serves the filtered page as a direct backward index scan.

-- migrate:up

CREATE INDEX network_events_decision_seq_idx ON network_events (decision, seq);

-- migrate:down

DROP INDEX network_events_decision_seq_idx;
