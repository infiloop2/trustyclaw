# Brave Search tool

Read-only web search grounding via the [Brave Search API](https://api-dashboard.search.brave.com/).
The package is `host/tools/brave_search/`; it owns no credentials and makes one
kind of outbound call.

- **`tool_id`**: `brave_search`
- **Connection**: `enable_only` — no OAuth, no per-account connect step, no
  stored credentials (`BraveSearchTool.credentials` is `None`). The deployment's
  API key is the only secret, and it lives in tool config.
- **Config**: `BRAVE_SEARCH_API_KEY` — a Brave Search API subscription key for
  the hosting deployment. The free "Data for AI" tier is enough for grounding.
- **Egress**: `POST https://api.search.brave.com/res/v1/llm/context` only.

## Data policy

- **What leaves the host**: only the normalized search query text. No account
  identity, no operator identity, and no other host data (nothing from Gmail,
  Calendar, agent state, or config beyond the key) is included.
- **Where it goes**: Brave Software, Inc.'s Search API (`api.search.brave.com`),
  authenticated with the deployment's `BRAVE_SEARCH_API_KEY`. That key identifies
  the deployment's own Brave account for billing and rate limiting; it is sent as
  a request header and never returned to the agent.
- **What the third party can do with it**: Brave receives the query and returns
  web-search grounding results, subject to Brave's API terms and privacy policy.
  Treat anything the agent puts in a query as disclosed to Brave — the agent
  should not place private mailbox, calendar, or host content into a web search.
- **What comes back**: public web content (titles, URLs, snippets) flows back
  into the host and on to the agent. The action is read-only and runs directly,
  so no host state is written and nothing is sent on the operator's behalf.

## Setup

Create a Brave Search API account, subscribe to a plan, and generate a
subscription key at <https://api-dashboard.search.brave.com/>. Set
`BRAVE_SEARCH_API_KEY` in the tool's config and enable the tool. There is no
connect step — once the key is set and the tool is enabled, `search_web` works.

## Actions

| Action | Approval | Input | Output |
| --- | --- | --- | --- |
| `search_web` | Direct (no approval) | `query` (string) | `{status, message, query, results[]}` |

`search_web` is the only action, and it is read-only, so it runs directly with
no approval. The tool has no approval-gated actions; `execute_approved` always
fails.

### `search_web`

Accepts the search text under `query`. It is normalized before it leaves the host:
trimmed, capped to the first **50 words**, then truncated to **400 characters**.
An empty query is rejected.

The request Brave receives is the LLM-context grounding call:

```json
{
  "q": "<normalized query>",
  "count": 8,
  "maximum_number_of_urls": 8,
  "maximum_number_of_tokens": 4096,
  "context_threshold_mode": "balanced"
}
```

sent with the `X-Subscription-Token: <BRAVE_SEARCH_API_KEY>` header and a
30-second timeout.

The response's `grounding.generic` list is mapped into `results`, keeping up to
**10** entries. Each entry is `{title, url, snippets}`, where `snippets` holds up
to **4** strings, each clipped to 1,200 characters; entries with neither a title
nor a URL are dropped. A successful call returns:

```json
{
  "status": "success_executed",
  "message": "Brave Search returned N grounding result(s).",
  "query": "<normalized query>",
  "results": [{"title": "...", "url": "...", "snippets": ["...", "..."]}]
}
```

### Errors

Failures come back as an `ActionFailed` with a specific message:

- **key not set** → "Tool config BRAVE_SEARCH_API_KEY is not set. The operator
  must set it in the admin UI's Tools tab." (enablement is not gated on config)
- **401 / 403** → "Brave Search API rejected the configured API key."
- **429** → "Brave Search API rate limit was reached."
- other HTTP status → "Brave Search API returned HTTP `<code>`."
- network failure → "Brave Search API request failed."
- non-JSON / non-object body → "Brave Search API returned an invalid response."
