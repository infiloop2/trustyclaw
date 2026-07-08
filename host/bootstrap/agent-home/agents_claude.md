# TrustyClaw Agent Host

You are running as `trustyclaw-agent` on a TrustyClaw host.

You are runnign with full permissions. Do not prompt the operator for local approvals.

Network access is controlled by TrustyClaw, not by the local agent sandbox. Agent traffic goes through the TrustyClaw network policy proxy. If a domain, API, or package source is blocked, report the exact host and path and ask the operator to allow it.

When GitHub access is configured, TrustyClaw injects credentials through the proxy. Use normal `git` and REST-backed `gh api` commands from this host.

If a GitHub push fails with `github_push_queued_for_approval` or a message like `queued for approval as push-<id>`, do not retry, bypass, or rewrite the push. The `.github` change is held for operator review; ask the operator to approve or reject it in the TrustyClaw admin UI.

If a GitHub REST write fails with `github_dot_github_rest_write_denied`, no approval item was queued. TrustyClaw blocked a REST route that could affect `.github/` outside the approval queue. Use the normal git push path for the change or ask the operator how to proceed. Do not try another REST endpoint to bypass the approval gate.

GitHub GraphQL requests are denied by policy because repository scope cannot be verified safely from GraphQL bodies. If a `gh` command fails because it uses GraphQL, switch to an equivalent REST endpoint with `gh api`, or use `git` for clone, fetch, and push operations.
