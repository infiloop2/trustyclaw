-- Clear pre-app-prefix host task references. Existing Agent Chat rows point to
-- host task/thread ids that were created before the admin API namespaced
-- app-backend task threads, so they must not be reused after upgrade.

-- migrate:up

DELETE FROM thread_tasks;
DELETE FROM threads;

-- migrate:down

-- One-time cleanup cannot be reversed.
