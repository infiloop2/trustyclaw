# Google Calendar tool

Read events on the connected Google Calendar account and propose event changes.
Reads run directly; creating, updating, or deleting an event is approval-gated.
The package is `host/tools/google_calendar/`.

- **`tool_id`**: `google_calendar`
- **Connection**: `oauth` — the operator connects one Google account through the
  admin UI. Tokens live in the host credential store.
- **Config**: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` — the same
  Google OAuth client can serve both Calendar and Gmail.
- **Scope of data**: all reads and writes target the account's **`primary`**
  calendar only.
- **Egress**: `https://www.googleapis.com/calendar/v3` plus the Google OAuth
  token and userinfo endpoints used by the shared credential store.

## OAuth scopes

Connect requests:

```
openid  email
https://www.googleapis.com/auth/calendar.events.readonly
https://www.googleapis.com/auth/calendar.events
```

Both `calendar.events*` scopes are *required*: a saved connection missing either
one is treated as needing reconnection rather than running with reduced access.

## Data policy

- **Third party**: Google (Calendar REST API), the provider of the connected
  account, which already holds the calendar. The tool moves the account's *own*
  events between Google and the host under the operator's OAuth grant; it does
  not expose the calendar to any *new* third party, and sends nothing to Brave or
  any non-Google service.
- **What leaves the host to Google**: for reads, the calendar id (`primary`) and
  the time range; for writes, the event fields being set — summary, description,
  location, start/end times, time zone — and the target event id. Every call is
  authorized by the connected account's own token.
- **What comes back into the host (and to the agent)**: the operator's own
  calendar data — event ids, times, titles, locations, descriptions, and (for
  the approval preview) attendee count, recurrence, and guest/organizer status.
  The agent and model see real event content returned by reads. Reads run
  directly; `event_change` is approval-gated, so no event is created, changed, or
  deleted at Google without explicit operator approval.
- **What the third party can do with it**: Google processes the API calls under
  the connected account's Google Terms of Service and the deployment's OAuth
  client, limited to the requested scopes.
- **Credentials**: OAuth tokens live only in the host credential store and are
  never returned to the agent or sent anywhere except Google's token endpoint.

## Setup

In Google Cloud Console, create an OAuth 2.0 Client ID of type "Web application"
under a Google account you control (the same client can serve Gmail and
Calendar), and add the redirect URI shown by Connect (the loopback admin origin,
e.g. `http://localhost:7443/oauth/callback`) to its Authorized redirect URIs.
Enable the Google Calendar API for the project. On the OAuth consent screen,
publish to Production (or add the account as a test user) — Calendar scopes are
sensitive, so test-mode refresh tokens expire after 7 days. Then set
`GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET`, enable the tool, and
Connect with the target account. See
[host-integration.md](host-integration.md#oauth-callback-and-token-exchange) for
how the callback and token exchange are authorized.

## Actions

| Action | Approval | Input | Purpose |
| --- | --- | --- | --- |
| `read_events` | Direct | `start_time?`, `end_time?` | List events in a time range |
| `event_change` | **Approval** | `operation`, `event_id?`, `summary?`, `description?`, `location?`, `start_time?`, `end_time?`, `time_zone?` | Create / update / delete an event |

### `read_events`

Lists up to **10** events from the `primary` calendar, ordered by start time,
with recurring series expanded (`singleEvents=true`). `start_time` and
`end_time` bound the window; when omitted they default to **now** through **now +
7 days**. Each event is returned as `{id, summary, start_time, end_time,
location, description, html_link}`.

### `event_change`

One action covers all three write operations via the `operation` field:

- **`create`** — must **not** include `event_id`; requires `summary`,
  `start_time`, and `end_time`. Optional `description`, `location`, `time_zone`.
- **`update`** — requires `event_id` and at least one changed field among
  `summary`, `description`, `location`, `start_time`, `end_time`. `description`
  and `location` may be passed as an empty string to *clear* them; every other
  field must be non-empty.
- **`delete`** — takes only `operation` and `event_id`.

`time_zone` may only accompany a `start_time` or `end_time`. For update and
delete, the tool first fetches a **preview** of the current event (title, start,
id, status, `updated` timestamp, attendee count, whether it is recurring, and
whether the connected account is a guest rather than the organizer), which feeds
the approval summary and the post-approval safety check.

On approval the tool writes to the `primary` calendar: `POST` for create,
`PATCH` for update, `DELETE` for delete. Empty `description` / `location` on an
update are sent so the field is cleared.

## Approvals

The approval **summary** carries enough to decide safely and is capped at 500
UTF-8 bytes (progressively clipped so it always fits):

- **create** shows the proposed title, start, and end, plus any location,
  description, and time zone that will be written.
- **update / delete** show the *current* event's title and start time (from the
  preview) plus context that distinguishes a lookalike or high-stakes event: the
  event id, whether it is a recurring series, whether you are a guest rather than
  the organizer, the guest count, and location. Update also lists the field
  changes (`field → new value`).

Before an approved change runs, the tool re-verifies the account and the event:

- the connected account's `sub` must match the approved account; and
- the event's `updated` timestamp must be unchanged and its status not
  `cancelled` — otherwise it fails with "Calendar event changed after approval.
  Please queue a new approval.", so an event edited or removed after approval is
  never overwritten or re-deleted blindly.

If the connection was revoked or lost a required scope, actions fail with
`reconnect_required` set, prompting the operator to reconnect.
