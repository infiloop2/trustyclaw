# Gmail tool

Read, search, and act on the connected Gmail account. Every read is direct;
every action that changes the mailbox is approval-gated. The package is
`host/tools/gmail/` (`__init__.py` for the tool and proposals, `api.py` for the
Gmail REST calls and previews).

- **`tool_id`**: `gmail`
- **Connection**: `oauth` — the operator connects one Google account through the
  admin UI. Tokens live in the host credential store; the tool holds no secrets
  itself.
- **Config**: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` — the
  deployment's Google OAuth client. The same client can serve both Gmail and
  Google Calendar.
- **Egress**: `https://www.googleapis.com` (Gmail REST v1) plus the Google OAuth
  token and userinfo endpoints used by the shared credential store.

## OAuth scopes

Connect requests these scopes:

```
openid  email
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.compose
https://www.googleapis.com/auth/gmail.labels
https://www.googleapis.com/auth/gmail.modify
```

The five `gmail.*` scopes are all *required*: if a saved connection is missing
any of them (for example after the scope set grew), the tool reports that Gmail
must be reconnected rather than running with reduced permission.

## Data policy

- **Third party**: Google (Gmail REST API), the provider of the connected
  account. Google already holds the mailbox, so this tool moves the account's
  *own* data between Google and the host under the operator's OAuth grant; it
  does not expose the mailbox to any *new* third party. Nothing is sent to Brave
  or any non-Google service.
- **What leaves the host to Google**: for reads, the Gmail query string and the
  message / thread / label / draft ids being fetched; for writes, the full
  composed content — recipients (To/Cc/Bcc), subject, body, label names and
  colors, and the target ids. Every call is authorized by the connected account's
  own token.
- **What comes back into the host (and to the agent)**: the operator's own
  mailbox data — headers, snippets, message and thread bodies, labels, and
  drafts. This is the sensitive direction: the agent and the model see real mail
  content returned by reads. Reads run directly; every write is approval-gated,
  so nothing is sent or changed at Google without explicit operator approval.
- **What the third party can do with it**: Google processes the API calls under
  the connected account's Google Terms of Service and the deployment's OAuth
  client. Standard Google account data handling applies; the tool grants Google
  no access beyond the requested scopes.
- **Credentials**: OAuth tokens live only in the host credential store and are
  never returned to the agent or sent anywhere except Google's token endpoint.

## Setup

In Google Cloud Console, create an OAuth 2.0 Client ID of type "Web application"
under a Google account you control, and add the redirect URI shown by Connect
(the loopback admin origin, e.g. `http://localhost:7443/oauth/callback`) to its
Authorized redirect URIs. Enable the Gmail API for the project. On the OAuth
consent screen, publish to Production (or add the account as a test user) —
Gmail scopes are restricted, so test-mode refresh tokens expire after 7 days.
Then set `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET`, enable the
tool, and Connect with the target account. See
[host-integration.md](host-integration.md#oauth-callback-and-token-exchange) for
how the callback and token exchange are authorized.

## Actions

Reads run directly; writes are queued for operator approval and reach Google
only after approval.

| Action | Approval | Input | Purpose |
| --- | --- | --- | --- |
| `search_messages` | Direct | `query?`, `start_time?`, `end_time?` | Search with a Gmail query string and/or a time window |
| `read_message` | Direct | `message_id` | Read one message (full format) |
| `read_thread` | Direct | `thread_id` | Read a conversation thread (metadata index) |
| `list_labels` | Direct | — | List the account's labels |
| `list_drafts` | Direct | `query?`, `page_token?`, `include_spam_trash?` | List drafts, paginated |
| `send_email` | **Approval** | `to`, `subject`, `blocks` | Compose and send a new message |
| `message_action` | **Approval** | `action`, `message_ids`, `label_ids?` | Archive / read / star / label / trash messages |
| `label_action` | **Approval** | `action`, `label_id?`, `name?`, `background_color?`, `text_color?` | Create / update / delete a label |
| `draft_action` | **Approval** | `action`, `draft_id?`, `to?`, `subject?`, `blocks?` | Create / update / send / delete a draft |

### Read actions

- **`search_messages`** builds a Gmail query from `query` plus optional
  `start_time` / `end_time`. The times accept an ISO 8601 timestamp or a
  `YYYY-MM-DD` date (naive values are treated as UTC) and become `after:` /
  `before:` epoch clauses; `start_time` must be before `end_time`. At least one
  of `query`, `start_time`, or `end_time` is required. Returns the matching
  message index.
- **`read_message`** fetches the message with `format=full` and returns a
  readable rendering (headers, body, structure).
- **`read_thread`** fetches the thread with `format=metadata` (From/To/Cc/Bcc/
  Subject/Date headers) and returns a message index of up to **100** entries;
  beyond that it sets `messageIndexTruncated`.
- **`list_labels`** returns the account's label list.
- **`list_drafts`** lists drafts (page size **10**), supporting `query`,
  `page_token`, and `include_spam_trash`, and returns a per-draft preview for
  each id on the page.

### Write actions (approval-gated)

Message bodies for `send_email` and draft create/update are supplied as
structured **blocks**, not raw text: an array of `{type: "paragraph", text}` and
`{type: "line_group", lines: [...]}` items. The host renders them into a MIME
message with a plain-text part and an HTML alternative.

- **`send_email`** composes a new message to `to` with `subject` and `blocks`
  and queues it for approval. On approval the host builds the MIME message and
  calls `users.messages.send`.
- **`message_action`** applies one operation to 1–**20** messages: `archive`,
  `mark_read`, `mark_unread`, `star`, `unstar`, `add_labels`, `remove_labels`,
  `trash`, `untrash`. Label operations take `label_ids` (up to **50**). Archive/
  read/star map to `users.messages.batchModify` add/remove of the `INBOX`,
  `UNREAD`, `STARRED` system labels or the given labels; `trash` / `untrash`
  operate on a single message.
- **`label_action`** creates, updates (rename and/or recolor), or deletes a
  label. Colors are chosen from named palettes (nine background names, `black`/
  `white` text) that map to Gmail's hex values; setting a color requires both a
  background and a text color.
- **`draft_action`** creates, updates, sends, or deletes a draft. Update, send,
  and delete require `draft_id` and capture the current draft first. Drafts that
  carry attachments cannot be *updated* through the tool (the tool cannot
  reconstruct the attachment parts), though they can still be sent or deleted.

## Approvals

Each write builds a proposal and an approval **summary** that discloses what will
actually happen, not just the target. Summaries are capped at 500 UTF-8 bytes
(the host limit), so every field is clipped per field and lower-priority detail
(attachment names, body previews) is dropped before a valid action is ever
blocked — the mandatory disclosures always fit. Examples:

- `send_email` discloses recipient, subject, and a clipped body preview plus its
  character count (so the operator approves content, not just an address).
- `message_action` names the target messages by subject and sender (from a
  proposal-time preview) and any label names.
- `draft_action` send/delete disclose recipients, routing headers (Cc/Bcc,
  In-Reply-To, References, Reply-To, thread), attachment count and best-effort
  names, and a body preview with a caveat when the preview is truncated or an
  HTML alternative would be sent.

Before an approved action runs, the tool re-verifies that nothing important
changed between proposal and execution, and fails with "…changed after approval.
Please queue a new approval." otherwise:

- **Account identity** — the connected account's `sub` must match the account
  the operator approved (across all write actions).
- **Labels** — for message-label and label actions, the label name (and color/
  type) must be unchanged, so a rename in between cannot silently redirect the
  action.
- **Drafts** — for draft send/update/delete, the draft's current `messageId`
  must match the approved one, so an edited draft is not sent as-is.

If the connection lost a required scope or was revoked, actions fail with
`reconnect_required` set, prompting the operator to reconnect from Integrations.
