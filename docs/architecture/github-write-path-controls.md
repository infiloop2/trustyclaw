# GitHub `.github` Approval Control

The GitHub integration supports one file-level write control:
`require_dot_github_approval`. When this boolean is true, a write-repository
push that changes `.github/` is held for operator approval before anything is
sent to GitHub.

## Policy Surface

The toggle lives under `managed_network_integrations.github` next to
`write_repositories`. Reads are unchanged, and normal writes to configured write
repositories still pass. The guard denies REST write paths that can create or
move `.github` changes without entering `git-receive-pack`, including
`contents/.github...`, `git/{refs,trees,commits}`, merge APIs, and pull-merge
APIs. Those denials use `github_dot_github_rest_write_denied`.

## Push Flow

For smart-HTTP `git-receive-pack` pushes to a configured write repository, the
proxy buffers the request body and asks `host/runtime/github_push_gate.py` to
inspect it. The gate resolves thin packs against a per-repo quarantine mirror
and uses real `git` plumbing (`index-pack` and `diff-tree`) to compute changed
paths.

- If no changed path is `.github` or under `.github/`, the proxy forwards the
  original push body upstream.
- If `.github` is touched, the gate stores the new objects under
  `refs/pending/<push_id>/...` in the quarantine mirror, writes a
  `pending_pushes` row, and returns a git `report-status` rejection that tells
  the agent the push is queued for approval. The network event reason is
  `github_push_queued_for_approval`.

The proxy parses pkt-line command framing only. Pack objects are parsed by
`git`, not by custom Python object logic.

## Approval Flow

Operators list and resolve held pushes through the admin API:
`/v1/network-tools/github-pending-pushes`. Approving a push invokes the
root-owned `approve-github-push` helper with the reviewed ref updates and the
working GitHub token. The helper replays the queued refs to GitHub from the
quarantine mirror with `git push --atomic` and per-ref
`--force-with-lease=<ref>:<old>` checks. Rejecting a push invokes the same
helper in cleanup mode and removes the pending refs.

Only one approve or reject action can claim a pending push at a time. The admin
UI shows both `pending` and `resolving` rows, so a stuck claim is visible without
rewriting its state. Cleanup runs once as part of the action. If cleanup fails,
the row is marked terminal with the cleanup detail so separate maintenance
tooling can reclaim retained refs without keeping the push in the operator
approval queue.

## Failure Behavior

The gate fails closed. If receive-pack parsing, quarantine indexing, mirror
fetch, pending-row insertion, approval replay, or cleanup cannot complete, the
push is not forwarded silently. A held push is marked with a terminal resolved
state (`approved`, `rejected`, or `failed`); cleanup errors are recorded in
`detail` for later maintenance.
