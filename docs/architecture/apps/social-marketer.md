# Social Marketer

Social Marketer plans and publishes consistent, on-brand content for X and
LinkedIn. It owns the campaign calendar, draft queue, approval status, and
performance learnings for those two channels only.

The workspace starts with a useful default publishing goal and measurement. The
agent begins from the human's brand, audience, campaign, or draft request and
asks only for missing details needed for the current work. Both defaults remain
editable.

## Product surfaces

- **Campaign calendar.** Scheduled X and LinkedIn posts in one calendar.
- **Composer and draft queue.** The human and agent edit the same post records.
  Each post is a draft, approved, or posted item.
- **Performance.** Campaign summaries and durable learnings guide the next plan.
- **Schedules.** Weekly campaign planning and a daily engagement/trend check are
  visible, editable, pausable, and removable.
- **Memories.** Voice, audience, recurring formats, and posting-window facts stay
  small and directly editable.

## Publishing

Concrete posts support platform `x` or `linkedin`. X bodies are bounded to 4000
UTF-8 bytes and LinkedIn bodies to 3000; the shared store holds at most 500
posts. A post id is a slug, and an optional schedule is an exact UTC timestamp.
Draft content is editable and drafts can be deleted. Approval binds the exact
platform, body, and schedule, so approved and posted records are immutable and
remain in campaign history. Status moves only from draft toward approved and
posted; a changed version is a new draft.

Publishing is always a separate host approval. The agent drafts the exact text,
requests `twitter_post_tweet` or `linkedin_create_post`, waits for the operator's
decision, then records the posted status and returned reference. LinkedIn is
text-only. Social Marketer has no Instagram surface, tool, draft type, calendar
channel, or discovery workflow.

Brave Search and LinkedIn Discovery can supply public campaign context. Tool
availability is visible in the workspace, while enabling or connecting a tool
remains an operator choice.

For the shared workspace behavior behind goals, messages, artifacts, schedules,
memories, tools, agent runs, and authentication, see [Workspace Kit](workspace-kit.md).
