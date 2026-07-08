# Audit: Settings Clarity and Least Surprise

Finding ID prefix: `UX`. See [README.md](README.md) for the sweep process,
entry template, and severity scale.

## Audit question

Do the settings and configurations, as presented through the UI (and the
deploy-time `config.json` they build on), make the intended behavior clear to
the operator? Could an operator, reading only what the product shows them, be
surprised by what the system actually does — even where the technical
implementation is correct?

## Threat model

This axis has no adversary; the failure mode is honest miscommunication. The
"asset" is the operator's accurate mental model: an operator who approves a
setting should be able to predict its effect. Judge against a competent
operator who reads the UI and the README but not the source code.

- **In scope:** every operator-facing control and status surface — network
  policy editing (what a wildcard matches, what enabling a managed AI
  provider actually opens up, method/path guard semantics, what happens to
  in-flight traffic on policy change), agent/task controls (what stop,
  reboot, or redeploy preserves and destroys), login/account status wording,
  health states (`error`, `awaiting_login`) and whether the severity they
  imply matches reality, defaults (fail-closed empty policy, auto-approve
  mode) and whether the UI says so, destructive-action confirmations, and
  mismatches between UI copy, `example_config.json`, README, and actual
  behavior.
- **Out of scope:** visual design and polish; whether a correctly-described
  behavior is a good idea; missing features, unless their absence makes an
  existing control misleading.

Severity mapping for this axis: **Critical/High** — the operator is misled
about a security- or data-loss-relevant behavior (e.g. believes traffic is
blocked that is allowed, or that data survives an operation that destroys
it). **Medium** — a plausible misreading causes rework or wrong operational
decisions. **Low** — confusing but self-correcting.

## Scope checklist

This checklist is not comprehensive: it names known-important areas, but the
audit question and threat model define the scope. Account for each item in
your coverage section, and report anything else within scope even if no item
below names it.

1. Walk every screen/control in `admin_ui.html`/`admin_ui.js`: for each,
   state what a reader would expect, then what the code does; report deltas.
2. Network policy editor: wildcard and precedence semantics, provider toggle
   expansion (which exact domains/paths it opens), empty-policy default.
3. Lifecycle actions: reboot, task stop, redeploy — what is preserved
   (database, agent home) vs lost, and whether the UI says so before acting.
4. Status and health vocabulary: does each state name lead the operator to
   the right action?
5. Deploy-time config: `example_config.json` and README table vs actual
   `host/config.py` validation and defaults.
6. Auto-approve mode: is the "agent acts without permission prompts" posture
   stated where an operator will see it before their first task?

## Key code and docs

- `host/runtime/admin_ui.html`, `host/runtime/admin_ui.js`
- `host/runtime/admin_api.py`, `host/runtime/network_policy.py` (actual
  semantics to compare against presented semantics)
- `example_config.json`, `host/config.py`, `README.md`, `docs/api/`

## Audit entries

## 2026-07-04 — Claude Opus 4.8 — `f28b50e`

Reviewer: Claude Opus 4.8 (claude-opus-4-8)
Commit: `f28b50e`
Methodology: read each admin-UI control and compared the behavior a competent
operator would predict from the UI copy against what the code actually does
(`admin_ui.js`/`admin_ui.html` vs `admin_api.py`, `orchestrator.py`,
`network_policy.py`, `config.py`). No usability testing with real operators.

### What was reviewed

The network policy builder (presets, manual domain form, JSON proposal editor,
Replace flow), the runtime login/deactivation guidance, the reboot and
task steer/cancel/kill confirmations, health/status wording, and the
fresh-deploy empty-policy default.

### Findings

| ID | Status | Severity | Location | Summary |
| --- | --- | --- | --- | --- |
| UX-1 | Open | Medium | `host/runtime/admin_ui.js:1111`, `host/runtime/orchestrator.py:282,309` | Replacing the active policy to disable a managed provider (untoggling OpenAI/Claude) synchronously fails every *running* task on that runtime and closes its runtime process (`reconcile_runtime_status_after_policy_change` → `deactivate_runtime` → `fail_running_tasks`), but the only confirmation is "Replace the active network policy with the proposed policy?" — an operator narrowing network access to tighten security can unexpectedly kill in-flight agent work with no warning and no undo. Name the affected runtime(s) and the running-task kill in the confirm dialog. |
| UX-2 | Open | Low | `host/runtime/network_policy.py:66`, `host/runtime/admin_ui.html:267` | A wildcard rule `*.example.com` matches sub-domains but **not** the apex `example.com` (`domain_matches` requires `host != pattern[2:]`), and the UI's only hint is the placeholder text `api.example.com or *.example.com`. An operator who adds `*.example.com` expecting the bare domain to be covered will get surprise denials for `example.com`. State the apex exclusion near the domain field or in the preset-info popover. |
| UX-3 | Open | Low | `host/runtime/admin_ui.js:1100` | Editing the "Proposed policy" JSON textarea silently swallows parse errors (`loadPolicyFromJsonEditor` catches and ignores), so while typing invalid JSON the preset buttons/status reflect the last *valid* proposal. The proposal only re-validates on Replace. An operator can believe an edit is staged when it is not; a subtle inline "unparsed JSON" indicator would remove the ambiguity. |
| UX-4 | Open | Low | `host/runtime/network_policy.py:49`, `host/runtime/admin_ui.html:235` | On a fresh deploy the policy is empty, which is fail-closed deny-all: the agent has **no** internet access until the operator adds rules. The active-policy panel shows `{}` with no statement that an empty policy blocks all agent traffic. The default is the safe one, but a first-time operator may read `{}` as "unrestricted" rather than "blocked"; one line of copy would prevent the misread. |

### Coverage and confidence

- Checklist 1 (walk every control): covered for the network, home/runtime, and
  agent tabs. The four findings are the deltas I found between presented and
  actual behavior; the preset-info popovers (`PRESET_INFO`) do accurately list
  what enabling each provider/preset expands to, which is good.
- Checklist 2 (policy semantics): wildcard apex exclusion (UX-2) and the
  managed-provider expansion were checked against `domain_matches` and
  `expand_network_controls`; manual rules cannot shadow managed provider
  domains (config rejects them), which is communicated by the error path.
- Checklist 3 (lifecycle preserve/destroy): UX-1 is the significant gap. Reboot
  is confirmed and `initialize_state` fails running tasks on restart, but the
  reboot dialog likewise does not mention that running tasks will be failed —
  I folded this into UX-1's theme rather than filing it separately; worth
  stating in the reboot confirm too.
- Checklist 4 (status vocabulary): `awaiting_login`/`error`/`deactivated`
  wording plus the runtime-guidance messages read accurately.
- Checklist 5 (deploy-time config): compared `example_config.json`/README to
  `config.py` validation at a glance; nothing surprising, but I did not do a
  field-by-field README-vs-validation diff — a dedicated pass there would
  strengthen coverage.
- Not done: no real-operator walkthrough; severities reflect my judgment of
  how likely a competent operator is to be surprised, not observed confusion.
## 2026-07-04 — GPT-5.5 — `f28b50e87b61`

Reviewer: GPT-5.5 (gpt-5.5)
Commit: `f28b50e87b61507db372d288d971487f55cb2121`
Methodology: static UI/code/doc comparison and grep sweeps. I compared
operator-facing README/API/architecture copy and admin UI labels against
`host/config.py`, `host/runtime/admin_ui.js`, `host/runtime/admin_api.py`,
`host/runtime/network_policy.py`, and lifecycle behavior. I did not run the UI
in a browser.

### What was reviewed

- `host/runtime/admin_ui.html` and `host/runtime/admin_ui.js`: every tab,
  task control, provider login prompt, network-policy preset, manual domain
  rule form, active/proposed policy text, reboot/kill confirmations, health
  vocabulary, file/process views, and usage/account labels.
- `README.md`, `example_config.json`, `docs/api/AdminAPI.md`, and
  `docs/architecture/*.md`: deploy config tables, lifecycle descriptions,
  network policy API semantics, runtime status, reboot/restart behavior,
  storage/lifecycle promises, and managed provider descriptions.
- `host/config.py`, `host/runtime/network_policy.py`,
  `host/runtime/admin_api.py`, and `host/runtime/orchestrator.py`: actual
  validation, provider expansion, task lifecycle, policy replacement effects,
  and reboot recovery.

### Findings

| ID | Status | Severity | Location | Summary |
| --- | --- | --- | --- | --- |
| UX-001 | Open | Medium | `host/runtime/admin_ui.html:151` | The first-task UI presents an ordinary "Agent chat" composer and "Create task" button, but it does not tell the operator at the point of use that tasks run in autonomous auto-approve mode. The README states "No permission prompts" (`README.md:18`), but an operator who reaches the UI through an existing deploy can start a task without seeing that posture. Add concise copy near the composer or runtime panel that the agent can execute commands without per-command prompts and is constrained by the active network policy. |
| UX-002 | Open | Medium | `host/runtime/admin_ui.js:526` | The reboot confirmation says only "Reboot the host machine?", while the actual startup recovery marks every task that was `running` as `failed` (`host/runtime/admin_api.py:1239`). An operator may reboot expecting in-flight work to resume because queued tasks and data survive. Mention in the confirmation that running tasks will fail and queued tasks/data survive. |
| UX-003 | Open | Low | `host/runtime/admin_ui.html:95` | The network tab is labeled "Internet Access and Tools" and includes buttons like "Add GitHub" (`host/runtime/admin_ui.html:252`), but the current presets only add network domain/method rules; they do not configure credentials, repo scopes, or tools. The info popover lists domains, so this is not a hidden security behavior, but the primary labels can still overpromise. Rename the tab/presets or add a short "network access only" note until managed tools/apps exist. |

### Coverage and confidence

Screen/control walk: I reviewed Home health, runtime login, provider usage,
Agent chat, Agent processes, Agent workspace, Agent audit log, Network audit
log, and the Network policy builder. I compared visible labels and
confirmations with the code paths they trigger, including task creation,
steering, cancel, kill, reboot, policy replacement, preset expansion, manual
domain rules, and OAuth login flows.

Network policy editor: I checked exact/wildcard matching and method/path guard
semantics in `host/config.py` and `network_policy.py` against the active policy
textarea, proposal status text, preset popovers, partial-overlap handling, and
manual rule placeholders. Managed OpenAI/Claude expansion is described in the
popover; GitHub/Python/npm are raw preset rules, captured in UX-003.

Lifecycle actions: I checked reboot, kill, cancel, deploy/upgrade/recover/
reconfigure/start/stop README tables, data-volume preservation copy, and
startup recovery. Reboot's effect on running tasks is the main mismatch.

Status/health vocabulary: `ok`, `degraded`, `active`, `error`,
`awaiting_login`, `deactivated`, usage "not reported", and runtime guidance
were checked against health/runtime code. I did not find a state label that
directly contradicts behavior.

Deploy-time config: `example_config.json` and README config fields match
`host/config.py` validation for agent name, AWS region, credential env names,
operator connection modes, SSH key shapes, Cloudflare hostname, and tunnel
token env names. Auto-approve posture is present in README but not where the
operator creates a task, captured in UX-001.
