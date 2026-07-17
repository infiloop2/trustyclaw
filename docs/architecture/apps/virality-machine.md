# Virality Machine

Virality Machine turns timely ideas into finished short-form videos and lets the
human review, play, and publish the best Reels to Instagram. It combines trend
research, storyboards, asynchronous Runway generation, a live render queue, and
approval-gated publishing in one durable workspace.

The workspace starts with a creation goal and weekly output measurement. The
agent begins from the human's idea, audience, or source material and asks only
for creative details needed for the current video. Both defaults remain editable.

## Product surfaces

- **Ideas and storyboard.** Concepts, hooks, and ordered shots remain editable
  artifacts rather than disappearing into chat.
- **Render queue.** Every Runway task has a durable id, kind, prompt, live status,
  provider task id, and finished-video workspace path.
- **Video review.** When a video succeeds, the agent saves the authoritative
  Runway output into its workspace. The render row opens that path in the
  host's existing Files viewer; it never opens an agent-authored URL.
- **Publish queue.** Finished Reels carry their caption, review state, workspace
  video path, approval state, and final Instagram reference.
- **Memories and schedules.** The human can edit creative rules and pause,
  change, or remove the daily Instagram trend scan.

## Generation and publishing

Runway generation is asynchronous. The agent starts a generation or edit,
records it as running, polls `runway_get_task`, and records every terminal state.
The temporary provider URL appears only as escaped text with a copy button; it
is not a link or navigation target. For a successful video the agent immediately
calls `runway_save_video {task_id}`. That action rechecks the task at Runway,
downloads only Runway's authoritative HTTPS output, and returns a host-generated
MP4 or MOV path under `/tool_assets` in the agent workspace.

The workspace path powers operator review. The video action sends that absolute
path through the typed parent Files command; the parent switches to its Files
viewer and never treats the path as a URL. To publish, the agent calls
`stage_video {path, for_tool: "instagram"}` and immediately passes the returned
`video_asset_id` to `instagram_post_reel`. The staged id is temporary transport,
never app state. Publishing still requires the operator's explicit host approval;
saving and viewing a video send nothing to Meta. The durable workspace file is
not consumed by publishing, and Meta never receives its pathname. The app never
opens or trusts an agent-supplied download URL.

Instagram Discovery and Brave Search supply research. X is an optional,
separately approved cross-post. Runway credit spend happens when generation is
started, while Instagram receives video bytes only after publish approval.

For the shared workspace behavior behind goals, messages, artifacts, schedules,
memories, tools, agent runs, and authentication, see [Workspace Kit](workspace-kit.md).
