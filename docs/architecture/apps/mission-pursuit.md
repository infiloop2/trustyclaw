# Mission Pursuit

Mission Pursuit is an installed app (see [Apps](apps.md) for the platform contract)
that gives the operator one persistent workspace shared with one agent. The
app opens with a focused prompt asking what the operator wants to achieve.
From the first message, the agent guides them through defining the goal,
measuring progress, and furnishing the workspace with
durable state of its own making:

- a **goal** the collaboration is working toward, and a **measurement** for
  how progress on it is judged,
- **artifacts**: agent-created stored JSON, optionally presented using the
  app's built-in typed view blocks,
- **memories**: small structured facts the agent keeps over time,
  which the operator can browse, edit, and delete,
- a **tools inventory** the agent maintains: what this workspace needs, at
  what priority, and whether each tool is enabled, implemented but not
  enabled, or not implemented yet,
- **schedules**: future runs the agent plans for itself, fired by the app
  backend as normal host tasks and reported with per-run status — including
  a seeded nightly **dream cycle** that tidies memory.

The product idea is that the agent stops being a chat transcript and becomes a
resident machine: it runs full time, does planned work while the operator is
away, and leaves behind an operating surface the operator can inspect and
control. The operator keeps per-item immediate controls (pause, resume,
delete, stop, edit, forget) over everything the agent sets up.

## Builder Setup

A fresh workspace starts in a guided setup: until a goal is set, every
composed task input carries a setup brief that tells the agent to (1) ask for
an ambitious goal — one that takes days or months and real collaboration, (2)
agree how progress will be measured and store both, (3) sketch the working
design (artifacts to keep, inputs needed from the human, how to prompt day to
day, which scheduled runs to create), (4) record the tools the goal needs in
the tools inventory with priority and status, asking the human to enable what
exists and flagging plainly what does not, and (5) walk the human through the
workspace levers: goal, measurement, memories, artifacts, schedules, and
tools. Setup is conversational rather than a form, and everything it produces
is ordinary workspace state the human can change later by asking.

## Security Boundary

The boundary in one sentence: **the agent's only channel into the app is the
host's agent app API — kernel-attributed calls to this backend's `/agent/`
routes — and the app backend is the sole validator and writer.**

Mission Pursuit opts in with `"agent": {"instructions": "agent.md", "api": true}`
in its manifest and serves three agent routes ([Agent App API](agent-app-api.md)
describes the transport:
the `app_api` tool, the dedicated proxy service, cgroup thread attribution, the
`/agent/` namespace restriction, and transport caps). The agent
runtime still cannot reach the app's HTTP listener directly (nftables
restricts new connections on app ports to the two proxy uids), cannot connect
to the app-backend admin socket (peer-uid authenticated), and cannot touch
the app's Postgres schema (peer auth, no agent role). Calls arrive with a
host-asserted thread marker, and this backend serves only the current internal
provider thread while it has an active run; an idle, stale, or foreign thread
fails closed. A completed task's `output_message`
is plain chat; nothing is parsed out of it.

Consequences of that shape:

- A prompt-injected or misbehaving agent can only do what the action protocol
  allows: edit this workspace's goal, measurement, memories, tools inventory,
  artifacts, and schedules. Every applied action is journaled in the feed;
  every rejected action is shown in the feed and returned synchronously as
  the call's error. There is no silent path inside the app's action protocol.
- Declarative artifact views are JSON blocks rendered by app-owned code with
  normal HTML escaping; the agent has no HTML or script channel into the app
  frame. Native controls carry only a stable `control_id` and a typed value;
  they cannot name a route, action, script, or direct state mutation. The
  backend validates each interaction against the currently stored view before
  delivering it to the agent.
- Self-triggering is budgeted. Writes are capped at 16 actions per turn
  (reads are free — they add no feed rows and no durable state), recurring
  schedules have a 5-minute floor and a cap of 20 schedules, a schedule skips
  its fire while its previous run chain is still active, and the host's
  queued-task limit backstops everything.
- All host access stays inside the existing app-backend socket allowlist:
  create task, read task, cancel, kill, steer, list thread tasks. Task inputs
  and outputs already flow through host state; Mission Pursuit adds no new host
  surface, socket, or route.

## How A Turn Works

Mission Pursuit presents one continuous conversation. One persistent **Agent
settings** control chooses the runtime, model, and effort before the first
message and can change them later while the workspace is idle. These settings
are global to the workspace: every future human turn and every future
scheduled run uses the same selected runtime, model, and effort. A schedule
does not have its own agent settings or its own thread.

The conversation has three distinct layers:

| Layer | Identity and lifetime | What uses it |
| --- | --- | --- |
| Workspace conversation | The one app-owned feed the operator sees. It lasts for the workspace. | Human messages, agent replies, events, and errors. It never resets when Agent settings change. |
| Host thread generation | The app-visible id is `ws-<n>`; the host namespaces it internally as `mission_pursuit__ws-<n>`. Exactly one generation is current. | Every serialized host task, whether created by a human message or a schedule firing. Completed generations remain historical and receive no new tasks. |
| Provider session | The Codex thread or resumable Claude session the host owns for one host thread generation. The operator never manages it directly. | Provider short-term conversation and tool context for tasks on that generation. |

Each human message and schedule firing creates a `runs` row, then one host
task. Runs and tasks are units of work, not additional threads. Dispatch is
serialized, so at most one run is active and chat and scheduled work share the
same current thread in order.

Changing Agent settings replaces the workspace's runtime/model/effort triple;
the triple itself is not incremented. Because the host cannot change an
existing thread's configuration, the app also advances its internal
`thread_seq` counter and names the replacement thread `ws-<thread_seq>`. The
new triple applies to all later chat and scheduled runs; it does not mutate
the old thread or individually rewrite schedules. Existing schedules
continue, and their next firing uses the replacement thread. A change is
refused while any run is queued or active, so work cannot cross the settings
boundary. Saving the same triple is a no-op and does not advance `thread_seq`.
Queued chat turns are capped at 20, which also bounds the busy list the UI
polls.

The next run after a settings change, whether chat or scheduled, creates the
first task on the new host thread and sends `agent_runtime`, `model`, and
`effort` together. Later tasks send none of them and inherit the host-stored
thread configuration. This is the host task contract, not app fallback logic.
That first task also carries at most the two most recent human/agent feed
messages, each capped at 1,900 characters. Events, errors, action calls, and
provider transcripts are never replayed. The durable workspace digest remains
the authoritative handoff; the small recent section only preserves immediate
conversational references. Provider short-term context outside that bounded
handoff can therefore be forgotten when Agent settings change.

A message sent while a turn is running is steered into that turn instead of
waiting: the app records the message and a queued run as usual, then delivers
the message through the allowlisted host task steer route and marks the run
done (`steered`) with a feed event. If the steer fails — the task finished in
the window, or too many steers are pending — the queued run simply dispatches
as its own turn. Delivery is therefore at-least-once: a crash between a
successful steer and the bookkeeping degrades to the message also arriving as
its own turn, never to a lost message.

An artifact interaction follows the same human-input path. The UI posts
`{artifact_id, control_id, value}` to the app backend. In one transaction, the
backend loads the current stored view, verifies the control still exists and
the value matches its block type, records a human feed message, and inserts a
normal chat run. It then attempts the same mid-turn steer as typed chat. The
agent receives a deterministic `artifact_interaction` JSON object, reads the
artifact, interprets the control in context, and applies any change through
the normal typed action protocol. The interaction itself never mutates the
artifact.

Every unit of agent work is a `runs` row handled by one app-owned run worker.
Messages and app mutations wake it immediately. The open UI polls every 5
seconds; each `/workspace` read also wakes the worker, so task completion is
normally visible on the next refresh. A 30-second idle fallback keeps
schedules, queued work, and restart recovery moving when no browser is open.
The UI request only signals the worker and reads a snapshot; it never executes
worker logic itself. Each worker tick performs, in order:

1. **Reap**: for each `active` run, read its host task. Actions were already
   applied live during the turn, so reaping a terminal task only records the
   plain-chat reply (or the failure), marks the run done, and retires the
   turn's action budget.
2. **Fire**: each enabled schedule whose `next_run_at` is due inserts a
   `pending` run and advances drift-free to the next slot on its original
   grid. One-shot schedules disable themselves after firing. A due schedule
   whose previous run chain is still active is skipped (recurring, with a
   feed event) or stays due until the chain finishes (one-shot).
3. **Dispatch**: the oldest `pending` run becomes a host task via
   `POST /v1/tasks`. Dispatch is strictly serialized: nothing is
   dispatched while another run is active, so every turn's digest reflects
   all previously completed turns. Dispatch retries from scratch after a
   failure. If the host accepted the request but its response was lost, the
   retry creates another task; this rare duplicate is the explicit trade for
   having no idempotency cache, durable replay records, or cross-service
   recovery protocol. Repeating the complete matching runtime/model/effort
   triple is accepted on an existing host thread, so the retry still
   converges when the lost response belonged to that thread's first task.
   Dispatch failures (queue full, runtime logged out) are recorded on the run,
   shown in the UI, and retried every 30 seconds; the operator can discard a
   queued turn that cannot dispatch to unblock the queue.

The host delivers Mission Pursuit's static `agent.md` as app instructions,
separate from the composed `input_message`. That file defines the action
protocol, view blocks, limits, and working style. The task message contains
only current app-owned context:

```
== Recent conversation ==  # first task after an agent-settings change only;
                            # two bounded human/agent messages
== Workspace state ==       # live digest: goal, schedules, artifact index,
                            # tools, memories
== Message ... ==           # the human message or schedule prompt
```

Section sizes are individually capped so the composed input always fits the
host's 50,000-character task input limit. The static app instructions have
their own 16 KiB manifest limit and do not consume the task-message budget.
Normal continuity comes from the current host provider thread, so past
messages are not repeatedly embedded. Scheduled runs must still carry their
own durable context in their prompt, and `agent.md` says so.

## The Action Protocol

The agent acts during its turn with the `app_api` tool:

```
app_api {"method": "POST", "path": "/agent/actions",
         "body": {"action": "create_artifact", "artifact_id": "tracker", "title": "Tracker", "data": {...}, "view": [...]}}
app_api {"method": "GET", "path": "/agent/artifacts/tracker"}
app_api {"method": "GET", "path": "/agent/workspace"}
```

`POST /agent/actions` takes one action object per call and applies it
immediately; unknown actions, unknown fields, and over-cap values are
rejected with a crisp reason that appears in the feed and comes back as the
call's 422 error, so the agent fixes the action and retries within the same
turn. `GET /agent/artifacts/<id>` returns the full artifact (title, data,
view) — mid-turn reads replaced the old `get_artifact`
continuation-turn machinery outright. `GET /agent/workspace` returns the
digest's data as JSON for a mid-turn state refresher. The agent's final
reply is plain chat.

| Action | Effect |
| --- | --- |
| `set_goal` | Set or clear the workspace goal shown in the UI banner and every digest. |
| `set_measurement` | Set or clear how goal progress is measured, shown with the goal. |
| `remember` / `forget` | Create/replace or delete one structured memory (slug id, content up to 300 chars). All memories ride in every digest, newest-updated first. |
| `upsert_tool` / `delete_tool` | Maintain the tools inventory: `priority` (`must_have`/`good_to_have`), `status` (`enabled`/`implemented`/`not_implemented`), optional note. |
| `create_artifact` / `update_artifact` | Create or partially update an artifact: `title`, free-form JSON `data`, and an optional declarative `view`. `"view": null` removes that surface. |
| `delete_artifact` | Delete an artifact. |
| `create_schedule` / `update_schedule` | Recurring (`every_minutes`, 5 to 10080) or one-shot (`at`, UTC). A past `at` means "as soon as possible". |
| `delete_schedule` | Delete a schedule. |

Views are JSON arrays of typed blocks rendered natively by the app UI:
`heading`, `text` (with `**bold**`, `*italic*`, `` `code` ``), `callout`,
`metrics`, `cards`, `details`, `list`, `table`, `checklist`, `progress`,
`timeline`, `kanban`, `chart` (single-series bar or line), `code`, and
`divider`, plus three interactive blocks: `button`, `toggle`, and `field`.
Every interactive block has a slug `control_id` unique within its view.
Buttons send `true`; toggles send their newly selected boolean; fields send a
string of at most 1,000 characters. Buttons may choose a native
`primary`/`neutral`/`danger` tone, and fields may include a placeholder. These
compose into dashboards, plans, reports, trackers, and operating documents
without introducing executable content. An artifact without a view renders
as pretty-printed JSON.

Caps, all enforced by the backend before any write: 100 artifacts, 20
schedules, 40 memories, 30 tools, 16 actions per turn (write calls, per
attributed task id), 64 blocks per view,
16,000 serialized characters each for `data` and `view`, plus per-field string
and count limits inside every block type.

## Memory And The Dream Cycle

Memories are the agent's structured long-term knowledge: one slug id and one
small self-contained fact each. They are embedded in every composed input, so
the agent never has to fetch them, and they survive agent-setting changes.
The operator's Memory panel lists every
entry with immediate edit and forget controls (journaled in the feed like any
other change).

Workspace creation seeds one schedule, `dream_cycle`, that runs nightly at
03:00 UTC with a fixed prompt: merge duplicate memories, drop stale or wrong
ones, rewrite unclear ones, capture anything important that recent work left
unrecorded, and keep the tools inventory and goal/measurement honest. It is a
completely ordinary schedule — the operator can pause or delete it, and the
agent can retune it — so the cleanup loop needs no special machinery.

## Digest Budgeting

Composed inputs must fit the host's 50,000-character task input limit while
carrying the setup brief until a goal exists, the digest
(goal, measurement, schedules, tools, artifacts index, memories), and the
message. The message is never trimmed; the digest flexes into whatever room
remains, dropping item lines tail-first per section (artifacts, then tools,
then memories, then schedules) down to a per-section floor, with an explicit
`...and N more` marker. Trimming costs little now: the agent re-reads
anything it needs mid-turn through `GET /agent/workspace`.

## Storage

Everything lives in the app schema `app_mission_pursuit`:

- `workspace`: singleton row: runtime, model, effort, internal thread
  generation, goal, measurement.
- `messages`: the feed: `user`, `agent`, `event` (applied actions, fires,
  skips), and `error` roles.
- `runs`: one row per unit of agent work (`chat`, `schedule`), its lifecycle
  (`pending` → `active` → `done`), and host task reference.
- `schedules`, `artifacts`, `memories`, `tools`: the agent-built surface.

The feed and runs tables grow without pruning; the UI reads the newest 200
feed rows. Letting the database grow is a deliberate simplicity trade.

## Future Extensions

- **Multiple missions or agents** would need the host's future app capability
  scoping first.
