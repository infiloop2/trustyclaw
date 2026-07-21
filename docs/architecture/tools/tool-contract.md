# Tool Contract

This is the complete, host-neutral contract between a **tool package** and its
**host**: one document, because the two sides are one contract. How *this* host
(TrustyClaw) implements the contract is a separate document:
[`host-integration.md`](host-integration.md).

The Python protocols under `host/tools/` express this contract as code
(`manifest.py`, `tool.py`, `results.py`, `host_api.py`); this document is the
source of truth and the two must agree.

A tool package is pure tool logic: no UI, and the only state it owns is one OAuth
credential. Everything tool-specific — actions, input schemas, third-party API
calls, third-party auth — lives in the package. Everything deployment-specific —
where the credential lives, how config is supplied, how approvals are decided and
audited — is provided by the host behind the **host API**.

```
agent / chat / MCP gateway
        │  action calls
        ▼
host  (host API: credentials · config · approvals · staged assets)
        │  Tool.execute(action, input, api)
        ▼
tool package
        │  normal third-party API calls
        ▼
third-party APIs
```

Every `HostAPI` handed to a tool call is already **scoped to one tool on one
host**. Credentials, approval records, and staged assets are implicitly
partitioned by `tool_id`; a tool can never address another tool's data.

## The tool: `Tool`

```python
class Tool(Protocol):
    @property
    def manifest(self) -> ToolManifest: ...

    @property
    def credentials(self) -> CredentialFlow | None: ...

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult: ...

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult: ...
```

`credentials` is non-`None` exactly when `manifest.connection == "oauth"` and
`None` when `connection == "enable_only"`.

`JSONObject` and `JSONValue` are plain JSON, defined once and reused for every
value that crosses the tool/host boundary:

```python
JSONValue  = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
JSONObject = dict[str, JSONValue]
```

Every action input, action result, OAuth result, stored credential, and approval
payload is a JSON value of these types. String length and payload size limits are
measured in UTF-8 bytes.

## The manifest: `ToolManifest`

The manifest is static and declarative. The host reads it to expose actions to
agents (MCP listings, model tool configs), to show the operator which config a
tool declares and each action's data policy, and to name the config keys a tool
reads. Enablement is a host policy, not part of this contract; this host does not
gate it on config.

```python
ConnectionKind = Literal["oauth", "enable_only"]
ApprovalKind = Literal["direct", "operator"]

@dataclass(frozen=True)
class ActionSpec:
    id: str
    description: str
    data_policy: str
    input_schema: JSONObject
    output_schema: JSONObject = {}          # empty for approval-gated actions
    approval: ApprovalKind = "direct"

@dataclass(frozen=True)
class ConfigRequirement:
    key: str
    description: str                         # always a secret; stored write-only

@dataclass(frozen=True)
class SetupStep:
    title: str
    description: str
    link_url: str = ""
    link_label: str = ""
    image_path: str = ""
    image_alt: str = ""
    show_callback: bool = False              # render this host's OAuth callback URI in the step
    show_config: bool = False                # render the tool's config keys in the step

@dataclass(frozen=True)
class DataSummaryPoint:
    label: str
    text: str

@dataclass(frozen=True)
class DataSummaryLink:
    label: str
    url: str

@dataclass(frozen=True)
class DataSummaryCard:
    title: str
    description: str = ""
    points: tuple[DataSummaryPoint, ...] = ()
    links: tuple[DataSummaryLink, ...] = ()

@dataclass(frozen=True)
class DataSummary:
    # Exactly four cards: what leaves this host, where it can go, what the
    # third party can do with it, how long it retains it.
    cards: tuple[DataSummaryCard, DataSummaryCard, DataSummaryCard, DataSummaryCard]

@dataclass(frozen=True)
class ToolManifest:
    tool_id: str
    display_name: str
    description: str
    actions: tuple[ActionSpec, ...]
    connection: ConnectionKind
    data_summary: DataSummary
    config: tuple[ConfigRequirement, ...] = ()
    protections: tuple[str, ...] = ()
    setup_steps: tuple[SetupStep, ...] = ()
```

- **`tool_id`** matches `^[a-z][a-z0-9_]{0,63}$`, is globally stable once
  released, and keys the tool's credential/config/approval/audit partitions. The
  host rejects duplicate ids across loaded tools. A bundled tool's id must match
  its `host/tools/<tool_id>/` package directory, so changing the code field
  without deliberately renaming the package fails CI.
- **`ActionSpec.id`** matches `^[A-Za-z0-9._:-]{1,128}$` and is unique within the
  tool. `input_schema` declares the callable parameters (JSON Schema); it is
  required so agents can call the action over MCP. `output_schema` describes the
  JSON result of a *direct* action and may be empty for approval-gated actions,
  which return a user-visible message rather than a JSON result.
- **`ActionSpec.approval`** states the control structurally: `direct` executes
  immediately; `operator` queues the exact payload and waits for an approval.
- **`ActionSpec.data_policy`** is **per action**: one or two plain sentences on
  what the action does, exactly which request values leave the host for it, and
  whether approval happens before the third-party request. The combined
  operator-facing data story lives in `data_summary`; `data_policy` is exposed
  through the admin API per action.
- **`ConfigRequirement`** is a deployment value the host supplies (OAuth client
  id/secret, API key). All config is treated as a secret: the host stores it
  write-only and never returns the value. Config is scoped per tool — two tools
  that declare the same key name each hold their own value.
- **`protections`** are short operator-facing statements of the integration's
  real safeguards. The compact info popover and full Integration Guides entry render
  the same values.
- **`setup_steps`** are the ordered provider-side and TrustyClaw steps needed to
  connect the tool. Each may link to an authoritative provider page and to one
  local, audited PNG under `/guide-assets/` with descriptive alt text. Provider
  links use HTTPS. Bundled-tool images live with their owning tool or shared
  provider integration under `host/tools/`, while the admin API keeps their
  browser URLs stable. `show_callback` and `show_config` render this host's
  OAuth callback URI or the tool's configuration keys inside the step where the
  operator needs them. Empty means enablement is the only setup.
- **`data_summary`** is the operator-facing account of where the tool can send
  data: always four cards, in order — what leaves this host, where it can go,
  what the third party can do with it, and how long it retains it — each
  linking only to authoritative provider policies. It answers for the tool's
  own boundary; the active model provider's handling is documented once in that
  provider's own integration guide, not repeated per tool.

Bundled packages are discovered from directories under `host/tools/`; there is
no registry to edit. Adding a tool means adding one regular Python package with
a module-level `BUNDLED_TOOL`. CI rejects malformed directories, missing package
initializers, id mismatches, duplicate ids, and declared schemas outside the
exact subset the host enforces.

## Action execution

```
host resolves the enabled, configured tool
  └─ tool.execute(action, tool_input, api)
       ├─ direct JSON action   → call third-party APIs → ActionExecuted(result)
       ├─ direct file action   → open provider bytes   → StreamingAsset(open_stream)
       ├─ approval-gated action → validate, build the exact execution payload +
       │                          a redacted summary → api.approvals.request(...)
       │                        → ActionPendingApproval(approval_id, summary)
       └─ failure              → ActionFailed(error, reconnect_required)
```

The tool owns semantic validation (limits, formats, referenced-id checks) and
returns a specific `ActionFailed` when input is invalid. When the user approves,
the host calls `execute_approved(record, api)` at most once (see the lifecycle
below), handing the loaded record — its status is guaranteed `approved` and it
belongs to this tool. The tool **re-verifies the payload's preconditions** (the
bound account and any mutable third-party objects it references), executes the
stored payload exactly as proposed, and returns `ApprovalExecuted` or
`ActionFailed` — never `ActionPendingApproval`.

### Result types

```python
@dataclass(frozen=True)
class ActionExecuted:      result: JSONObject      # direct action, validated vs output_schema
@dataclass(frozen=True)
class OpenedStreamingAsset: filename: str; media_type: str; size_bytes: int; source: BinaryIO
@dataclass(frozen=True)
class StreamingAsset:      open_stream: Callable[[], AbstractContextManager[OpenedStreamingAsset]]
@dataclass(frozen=True)
class ActionPendingApproval: approval_id: str; summary: str = ""
@dataclass(frozen=True)
class ApprovalExecuted:    message: str            # approved action outcome, user-visible
@dataclass(frozen=True)
class ActionFailed:        error: str; reconnect_required: bool = False

ActionResult   = ActionExecuted | StreamingAsset | ActionPendingApproval | ActionFailed
ApprovalResult = ApprovalExecuted | ActionFailed                          # execute_approved(...)
```

- `ActionExecuted.result` is a JSON object validated against `output_schema`,
  shown to the agent. It must not include tokens, secrets, or raw provider bodies.
- `StreamingAsset` is the entire direct-action result, never a field inside an
  `ActionExecuted` JSON object. Entering `open_stream` yields one opened source
  plus its filename, media type, and exact encoded byte length. The host relays
  the bytes over its agent transport, and the agent-side adapter converts every
  stream into a durable workspace path. The two result variants make mixed
  JSON-and-binary responses unrepresentable. Expected open or transfer failures
  raise `StreamingAssetError` with a redacted agent-visible message.
- The outcome of an **approved** action is a single user-visible
  `ApprovalExecuted.message` string, not a JSON result validated against an
  `output_schema`. The host stores it as the approval's terminal `result` text
  (the failure error for a failed execution — the approval status says which),
  surfaces it to the operator in the admin UI, and returns it to the agent
  through `check_tool_approval` (as the terminal `execution_result` text) so
  the agent can learn the result and resume; because the agent can read it,
  the message must not include tokens, secrets, or raw provider bodies,
  exactly like a direct action's result.
- `ActionFailed.error` is user/agent visible and must be sanitized. Set
  `reconnect_required=True` only when the saved connection is missing, expired,
  revoked, or missing scopes.

## The host API: `HostAPI`

```python
class HostAPI(Protocol):
    @property
    def credentials(self) -> Credentials: ...          # oauth token store (typed)
    @property
    def config(self) -> Mapping[str, str]: ...          # this tool's declared config keys
    @property
    def approvals(self) -> Approvals: ...               # host-owned approval workflow
    @property
    def assets(self) -> Assets: ...                     # opaque, tool-scoped staged bytes
    @property
    def outbound(self) -> Outbound: ...                 # request-parameter guard
```

Host implementations validate every argument: invalid types, formats, or
size-limit violations raise `ValueError`; a missing config key raises `KeyError`.

### Outbound parameter guard

`Outbound` is the host-owned check tools apply to agent-controlled free-text
request values before building a request. It is deterministic and offline, and
denies by raising `ValueError` with a descriptive, value-free message the tool
surfaces verbatim so the agent can rephrase and retry.

```python
class Outbound(Protocol):
    def guard_request_parameter_string(self, value: str, *, allow_identifiers: bool = False) -> str: ...
```

It denies secret/credential shapes, one-time codes, personal and financial
identifiers, and encoded payloads, and returns the value unchanged on
success. `allow_identifiers=True` skips only the personal-identifier rules
(email, phone, card, SSN, digit runs, DOB, government id) for a query against
an account the operator already connected (a mailbox search), where a
personal identifier is legitimate search syntax (`from:alice@example.com`)
and the destination already holds that data; secret/credential shapes and
encoded payloads are still denied. A tool applies the guard to each
decoded semantic value it controls; the host runs the same rules over managed
network-integration request URLs. The rules, the data classes each covers, and
the trade-offs are specified in
[`outbound-request-filtering.md`](outbound-request-filtering.md).

### Staged assets

`Assets` is the narrow ingress exception to the action result contract. A
host-specific ingress can stream agent file bytes into host-owned storage and
return an opaque id; no caller pathname enters the tool process. The host scopes
every id to one tool and exposes metadata plus an already-open binary stream:

```python
@dataclass(frozen=True)
class AssetMetadata:
    asset_id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    expires_at: int

class Assets(Protocol):
    def describe(self, asset_id: str) -> AssetMetadata: ...
    def open(self, asset_id: str) -> AbstractContextManager[BinaryIO]: ...
    def delete(self, asset_id: str) -> None: ...
```

Tool packages receive neither a storage path nor cross-tool lookup.

An approval payload that references an input asset binds its filename, encoded
byte size, and SHA-256; execution verifies those values before data-out. Assets
are ephemeral input transport, not durable tool state. Provider output uses the
generic `StreamingAsset` result instead and never enters this staged-asset store.

### Credentials

OAuth tools are the only tools that persist state, and all they persist is one
connected-account credential. Instead of a generic key/value store, the host
exposes a purpose-built typed service — one `StoredCredential` per tool.
Enable-only tools never touch it.

```python
class ConnectionAccount(TypedDict):
    id: str            # stable provider account id (e.g. Google `sub`)
    label: str         # human-readable account (an email)
    scopes: list[str]  # granted OAuth scopes

class StoredCredential(TypedDict):
    account: ConnectionAccount   # fixed host-typed shape, non-secret, surfaced in the UI
    secret: JSONObject           # tool-defined provider token material; opaque to the host, encrypted at rest
    metadata: JSONObject         # tool-defined non-secret bookkeeping; opaque to the host

class Credentials(Protocol):
    def load(self) -> StoredCredential | None: ...
    def save(self, credential: StoredCredential) -> None: ...
    def clear(self) -> None: ...
```

Only `ConnectionAccount` has a fixed, host-typed shape: it is the explicit
structure every OAuth tool returns and the host stores and displays, and its `id`
binds an approval to the account that was connected when it was proposed. `secret`
and `metadata` are **tool-defined and opaque**: their contents are whatever the
tool needs (`secret` is the provider token material; `metadata` is non-secret
bookkeeping such as connect/refresh timestamps), and the host stores and returns
them verbatim without ever interpreting them. So their shapes differ per tool by
design; they are not declared in the manifest (which describes operator-supplied
config, not credential internals) and are intentionally `JSONObject` rather than
concrete fields. The host stores the whole `StoredCredential` encrypted at rest
and guarantees per-tool isolation.

### Config

`config` is a read-only `Mapping[str, str]` the host builds before the call,
containing only this tool's declared keys (config keys match
`^[A-Z][A-Z0-9_]{0,127}$`). Values are secrets — returned only to the tool
process for the current call, never logged, never returned by the host API/UI
(which report only whether a key is *set*). Missing keys raise `KeyError`.

### Approvals

Approvals are host-owned workflow records for a specific proposed action, not
tool storage and not free-form instructions.

```python
@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str          # host-assigned approval_<number>.<token>, ^[A-Za-z0-9._:-]+$
    action_id: str            # the manifest action id
    status: ApprovalStatus    # pending|approved|denied|expired|executed|failed
    payload: JSONObject       # exact JSON to execute; stored, returned verbatim (≤64 KiB)
    summary: str              # redacted, user-displayable (1–500 UTF-8 bytes)
    created_at: int
    decided_at: int = 0

class Approvals(Protocol):
    def request(self, *, action_id: str, summary: str, payload: JSONObject) -> ApprovalRecord: ...
```

The tool builds a redacted `summary` and the exact `payload` it will execute; the
host stores the record, assigns a tool-agnostic token-bearing `approval_id`,
presents the decision in its own UX, and on approval hands the loaded record
back to `execute_approved`. **The host is responsible for
storing the execution result and exposing a way for the agent to query an
approval's status and terminal result** (and, for direct actions, the result
object). How it exposes that — polling tokens, endpoints — is host surface, not
part of this contract; see [`host-integration.md`](host-integration.md).

### Approval lifecycle

```
pending ──► denied                 (user decision, terminal)
   │
   ├──────► expired                (host policy, terminal)
   │
   └──────► approved ──► executed  (execute_approved returned success)
                  │
                  └────► failed     (execute_approved returned failure)
```

Every approval is **single-use**. The host applies each transition as an atomic
conditional update from the expected prior status (so two concurrent decisions or
executions cannot both win), calls `execute_approved` at most once, and records
the terminal outcome. There are no retries: if execution fails — or the outcome
is unknown, e.g. a crash mid-call — the approval is spent, and repeating the
action requires a new proposal and approval. Whether and when pending approvals
expire is host policy.

## Credential flows (OAuth)

Third-party auth is tool-owned because it is provider-specific: scope selection,
authorization-URL construction, `state` verification, token exchange, refresh,
revocation, and account lookup all live in the tool. The host contributes only
the **tool-agnostic connect boundary** — fixed param/result shapes and the
callback plumbing — so new OAuth tools reuse it without inventing their own
connect API. This is meant to extend to future OAuth variants; a tool configures
only what it needs and declares any provider config it requires in the manifest.

```python
class OAuthStartConnectParams(TypedDict):     redirect_uri: str
class OAuthStartConnectResult(TypedDict):     authorization_url: str; state: str
class OAuthCompleteConnectParams(TypedDict):  code: str; state: str; redirect_uri: str
class OAuthCompleteConnectResult(TypedDict):  account: ConnectionAccount
class ConnectionStatus(TypedDict, total=False): connected: bool; account: ConnectionAccount

class CredentialFlow(Protocol):
    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult: ...
    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult: ...
    def disconnect(self, api: HostAPI) -> None: ...
    def connection_status(self, api: HostAPI) -> ConnectionStatus: ...
```

- **`start_connect`** returns the provider `authorization_url` the host redirects
  the operator to, plus `state`: the tool's opaque anti-forgery value. The tool
  mints it here and re-verifies it in `complete_connect`; the host round-trips it
  through the provider callback unchanged and never interprets it.
- **`complete_connect`** receives the standard authorization-code callback
  fields (host-fixed and validated before the tool sees them), verifies `state`,
  exchanges `code` for tokens, persists them via `api.credentials`, and returns
  the connected `account`.
- **`disconnect`** revokes tokens where possible and clears the credential.
- **`connection_status`** returns non-secret current state for the host UI.

The connect param/result shapes are a uniform host contract for all OAuth tools;
`ToolManifest` does not declare per-tool connect parameter schemas. Token refresh
happens inside the tool during action execution; expired or revoked credentials
surface as `ActionFailed(reconnect_required=True)`.

## Rules

1. **State only via `credentials`.** The only state a tool persists is its OAuth
   credential, through `api.credentials`. The host decides where and how it lives
   (partitioning, encryption); the tool decides what the `secret` is.
2. **No ambient access.** Tool code never reads environment secrets, token files,
   or databases directly — only the host API. Third-party HTTP calls are normal
   tool code.
3. **Approval-gated writes never execute inline.** They queue the exact payload
   and a redacted summary and run only through `execute_approved`.
4. **Per-action data policy is part of the contract.** Each action states what it
   handles and what leaves the host, with and without approval.
5. **Everything visible is sanitized.** Results, messages, summaries, and errors
   are user/agent visible: no tokens, secrets, or raw third-party payloads. Map
   unexpected exceptions to generic messages.
6. **Stateless across calls.** No in-process state between calls, so the same
   package works on this host or another interchangeably.
7. **Host-owned audit.** The host records every accepted call with its exact
   bounded arguments and outcome; tools do not write audit records. Approval
   decisions record the exact payload handed to `execute_approved`. Operator
   lifecycle events such as config and OAuth changes carry no action arguments.
8. **The tool verifies bound state before executing an approval.** The world can
   change between proposal and approval, so `execute_approved` must re-check the
   preconditions the tool captured at proposal time before touching third-party
   state: for OAuth tools, that the same account (`ConnectionAccount.id`) is still
   connected, and that any mutable object the payload references (a draft to send,
   a label or event to change) still matches what was proposed. On any mismatch it
   fails and asks for a new approval, so the user never approves one thing and
   gets another.
