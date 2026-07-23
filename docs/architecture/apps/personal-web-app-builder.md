# Personal Web App Builder

Personal Web App Builder is an installed app for creating one durable personal
web app through conversation. The agent owns the generated interface and its
structured data model. The human sees the generated app as the primary surface
and opens a compact, dismissible agent chat to build, change, or ask questions
about it.

This product is independent of Mission Pursuit and Workspace Kit. It has no
mission, goal, measurement, schedule, artifact, memory, tool inventory, or
background pursuit state. Its complete durable domain is one app bundle, one
JSON document, one fixed agent chat configuration, and references to that
chat's host tasks.

## Product Contract

The generated app has three authored layers:

- HTML describes the semantic interface.
- CSS supplies layout, typography, color, responsive behavior, animation, and
  other visual presentation accepted by the trusted CSS sanitizer.
- JavaScript supplies computation and event handling through a frozen `app`
  capability object.

The structured JSON document is the generated app's backend data. It may contain
any JSON object shape within the encoded size limit. The UI can read and mutate
that data through typed paths. The agent can replace the data, the interface, or
both in one revision-checked action.

The app canvas fills the product surface. Agent chat is a small floating drawer
over the canvas and can be dismissed at any time. Closing chat does not stop an
active task or change generated app state.

## Security Boundary

Agent-authored JavaScript never runs in a realm with a DOM. It runs in a
short-lived dedicated Web Worker created by audited app code. The trusted app
frame owns the real DOM, sanitizes every render, translates user events into
plain data, validates worker messages, and performs typed backend calls.

This boundary is intentionally different from placing agent HTML and script in
a sandboxed iframe. A normal iframe with script can navigate itself, submit
forms, load subresources through markup and CSS, and use an expanding set of
browser network APIs. Removing `allow-same-origin` protects the parent origin,
but does not make the child a zero-network execution environment. CSP blocks
many request classes, but browser navigation directives are not a complete,
portable JavaScript execution boundary.

A dedicated worker has no `window`, `document`, anchors, forms, media elements,
top-level browsing context, cookies, or DOM storage. Personal Web App Builder
adds four reinforcing layers:

1. The app iframe remains opaque-origin and sandboxed. Its CSP keeps
   `connect-src 'none'`, `frame-src 'none'`, `form-action 'none'`, no inline
   scripts, and no parent credential.
2. Only this manifest opts into `worker-src blob:`. Other app CSPs remain
   unchanged.
3. The trusted bootstrap removes and seals network, code-loading, nested-worker,
   cross-context, and storage globals before generated code executes. Dynamic
   import syntax is rejected when a bundle is written.
4. Generated code receives only the frozen `app` object. The trusted owner
   validates every message, bounds message rate and size, and terminates the
   worker after initialization or one event turn. A worker that does not finish
   within three seconds is terminated.

One event turn may have only one durable mutation in flight and at most 16
mutations total. The frame also caps total worker messages per second and per
turn. A generated handler that exceeds a cap is terminated instead of queuing
unbounded backend work.

CSP is the authoritative network and code-loading boundary. Removing the global
`Function` binding does not remove `Function.prototype.constructor`, and the
worker retains `WebAssembly`. The app frame therefore keeps `unsafe-eval` and
`wasm-unsafe-eval` absent from `script-src`; its blob worker inherits that
policy and `connect-src 'none'`. The global lockdown keeps common browser
capabilities absent even before CSP evaluates a request, but it is defense in
depth rather than a substitute for those directives. Worker termination bounds
a generated infinite loop to one short-lived worker turn. The browser process
itself remains outside the generated code's control.

## Trusted Rendering

The renderer parses generated HTML into an inert template, copies allowed nodes
into a new document fragment, and inserts that fragment into a Shadow DOM. It
never inserts the original parsed tree. The element allowlist covers HTML text
semantics, landmarks, disclosure, lists, tables, ruby annotations, details,
buttons, labels, datalists, meters, progress, and common form controls. Safe
relationship, constraint, accessibility, language, and table attributes
preserve native browser semantics without adding a request or execution sink.

The renderer drops all anchors, images, audio, video, sources, links, metadata,
scripts, styles, iframes, objects, and embeds. It discards event attributes,
`href`, `src`, form targets and actions, style attributes, and every unrecognized
attribute. Buttons receive `type="button"`; input types come from a fixed inert
control allowlist. Generated markup therefore has no browser request or
navigation sink.

The CSS renderer parses the stylesheet through the browser CSS object model and
re-emits only style rules, media groups with bounded conditions, and keyframes.
Its visual language includes custom properties, gradients, backgrounds, grid,
flexbox, typography, filters, clipping shapes, transforms, animation, and
scroll snapping. It drops supports and import rules, font faces, namespaces,
document rules, unknown at-rules, and values containing URL, image-set,
cross-fade, element-image, or paint-worklet functions. The result is scoped to
the generated app's Shadow DOM. The trusted host is a paint-contained stacking
context, and the sanitizer rejects host-targeting and escaped selectors. Fixed
generated content therefore remains inside the canvas and below the trusted
header and chat drawer.

The static iframe shell, chat drawer, bridge, worker bootstrap, HTML sanitizer,
and CSS sanitizer are audited release assets. The agent cannot replace or style
that trusted chrome.

## Generated JavaScript API

Generated code registers event handlers during worker startup. The worker is
recreated from the current stored JavaScript for every event, receives a
structured clone of current data, runs one matching handler, and is terminated.
Durable app state lives in the backend JSON document, not worker memory.

The frozen global API is:

```text
app.onLoad(handler)
app.on(action, handler)
app.data()
app.render(html, css)
app.set(path, value)
app.delete(path)
app.append(path, value)
app.askAgent(message)
app.notify(message, level)
```

`app.onLoad` registers one renderer. The trusted frame invokes it only during a
load turn, after the worker has received the current durable data, and then
terminates the worker. The handler may render and notify, but the frame rejects
data mutations and agent requests outside a genuine user-event turn. Generated
apps render from `app.data()` in this handler so runtime mutations remain
visible after a reload or a later agent revision.

`app.on` binds a bounded action name to one handler. Generated HTML exposes an
action with `data-action="name"`. A click or change on that element becomes a
plain `{action, value, checked, fields}` event. Controls marked with
`data-field="name"` contribute values to `fields`. No DOM node, Event object,
selector API, or browser global crosses into the worker.

Buttons and non-control action elements dispatch from click. Inputs, selects,
and textareas ignore the preliminary click and dispatch only from their native
change event, after the checked state or selected value has changed.

`app.data()` returns a structured clone. `app.set`, `app.delete`, and
`app.append` accept a path array of string object keys and non-negative integer
array indexes. They call the fixed runtime action endpoint and resolve with the
new durable data. Every mutation includes the current revision, so concurrent
agent, user, or worker changes fail with a conflict instead of overwriting one
another.

`app.render` requests another pass through both trusted sanitizers. `app.notify`
shows bounded plain text in the trusted header. Neither method creates a URL or
calls a backend chosen by generated code.

Background timers and persistent local worker state are not part of this
contract. An app that needs state stores it in the JSON document. This trade
keeps CPU lifetime bounded and gives every interaction the same convergence
path: recreate from the durable revision, render on load or handle one event,
then tear down.

## App Buttons That Ask The Agent

Generated JavaScript can call `app.askAgent(message)` while handling a genuine
generated-app user event. This lets an agent-authored button calculate a useful
instruction from current structured data instead of storing a fixed prompt in
markup. The trusted frame ignores requests sent during worker initialization,
accepts at most one request per event turn, and bounds the encoded message.

The generated-app user action is the authorization to start the task. The frame
immediately sends the bounded message to the fixed builder thread. An existing
chat reuses its session configuration. The first task uses the runtime, model,
and effort selected in the trusted app bar, even when the chat drawer is
closed. The app stores that configuration independently of visible task
history. Those controls remain visible and become read-only after the session
is established, including after old host tasks are pruned. Trusted header
status reports whether the task started.

Generated JavaScript cannot synthesize the initial trusted user event, choose a
backend route, choose session configuration, or call the parent bridge. An
accepted `app.askAgent` instruction has the same authority as the human typing
that instruction in builder chat. The task runs with the builder agent's normal
tools and egress, subject to the host's network policy and approval controls.
Those host controls are the security boundary; the real-event gate, one-request
limit, and message-size cap only constrain how the task starts. `app.askAgent`
grants direct task-start authority from a real app interaction, not general host
API or browser network authority. Human chat uses the fixed `/messages` route
and generated interaction callbacks use the fixed
`/runtime/agent-requests` route. The backend prepends `Requested by user:` or
`Requested by app:` to the task input. This trusted first line gives the agent
and chat history durable provenance without creating a second thread or
changing task authority.

## Backend State And Routes

The app schema stores only explicit columns for `revision`, `html`, `css`,
`javascript`, `data_json`, and `updated_at`. `data_json` is opaque only because
its schema is genuinely authored by the agent for the personal app. The backend
parses and validates it as a JSON object on every write. The host is the sole
authority for the app-scoped `builder` thread, its tasks, provider session,
runtime, model, and effort. The app does not duplicate task references or
session configuration in its own database.

Browser routes provide session options, separate state and conversation reads,
separate human and generated-app message creation, task stop controls, and
typed runtime data actions. Both message routes create tasks on the one
`builder` thread. The conversation read asks the host for that thread's newest
20 tasks, derives the UI's fixed session display from the newest task, and
bounds each message before it crosses the app-backend proxy. The first UI
message supplies runtime, model, and effort; later messages omit them. The host
remains authoritative for whether the thread is new and whether supplied
configuration matches it. An empty retained history is simply first-run state.
Retained chat growth cannot make the builder surface unavailable.
Keeping state and conversation separate prevents a maximum generated bundle
from consuming the chat history's response budget. Browser routes require the
host's app proxy marker. Agent routes are limited to reading state and applying
one typed action. They require the kernel-attributed app proxy marker and the
fixed `builder` app thread. Neither route namespace can call the other.

All agent writes require `expected_revision`. `replace_app` changes HTML, CSS,
JavaScript, and data atomically. `replace_ui` keeps data unchanged.
`replace_data` keeps the UI bundle unchanged. Agent `set`, `delete`, and
`append` actions use the same typed path mutation implementation as generated
UI controls, so a local data change does not require a whole-document rewrite.
Unknown actions and extra fields are rejected.

Encoded limits are 128 KiB HTML, 64 KiB CSS, 128 KiB JavaScript, and 256 KiB
data. Runtime paths contain 1 through 16 bounded segments. Request bodies,
worker messages, notifications, and agent-button messages have independent
caps. The complete serialized state is capped below the host proxy's response
limit. Limits use encoded bytes where representation size matters.

## Verification

Unit tests pin manifest opt-in, the split route boundary, revision conflicts,
exact agent actions, encoded caps, typed data paths, the load-render contract,
and forbidden dynamic imports. App platform tests prove that other apps do not
gain blob worker permission.

Desktop browser smoke coverage renders a deliberately hostile bundle containing
foreign SVG and MathML, templates, noscript and unknown elements, external
image, link, form, CSS import, CSS image, supports and oversized media rules,
fetch, importScripts, and WebSocket attempts. It asserts that the node-by-node
rebuild rechecks promoted children, no hostile browser request occurs, forbidden
elements and attributes are absent, richer semantic elements and safe visual
CSS survive, and escaped resource functions remain blocked. It also verifies
worker-backed data mutation, durable rendering after a full page reload, fixed
app-bar agent settings, the dismissible chat drawer, and the exact agent
instruction started by a generated button without an additional confirmation.
Mobile coverage verifies the full canvas, app-bar controls, and dismissible
drawer without horizontal overflow.
