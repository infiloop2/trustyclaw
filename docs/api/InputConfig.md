# Input Config

## Top-Level Object

```json
{
  "agent_name": "trustyclaw-dev-agent",
  "aws_region": "us-east-1",
  "aws_access_key_id_env": "AWS_ACCESS_KEY_ID",
  "aws_secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
  "ssh_public_key": "ssh-ed25519 AAAA...",
  "network_controls": {
    "ssh_port_opened": true,
    "managed_ai_provider_network_access": {
      "openai": true,
      "claude": true
    },
    "allowed_network_access": {}
  }
}
```

| Field | Required | Type | Behavior |
| --- | --- | --- | --- |
| `agent_name` | Yes | string | Stable host name. Must be 1-50 characters and contain only letters, numbers, hyphen (`-`), and underscore (`_`). The deploy command uses it to identify the EC2 machine. If it already exists, deploy prompts before deleting and recreating it. |
| `aws_region` | Yes | string | AWS region where the EC2 host is deployed. |
| `aws_access_key_id_env` | Yes | string | Name of the environment variable containing the AWS access key id. |
| `aws_secret_access_key_env` | Yes | string | Name of the environment variable containing the AWS secret access key. |
| `ssh_public_key` | Yes | string | SSH public key installed for operator access. This is the key content, not a file path. |
| `network_controls` | Yes | object | Network control configuration. |

## Network Controls

By default, all network access for the agent is closed. The host opens network access
incrementally through `network_controls`.

Network controls govern the agent and host service users, not root-owned host
bootstrap and maintenance work. At the host firewall, root (uid 0) has outbound
access for package installation, security updates, and ordinary root-owned
system traffic. The dedicated `trustyclaw-proxy` uid has outbound access only so
it can make policy-approved upstream connections on behalf of the agent. The
host does not install or configure the AWS SSM agent, and snapd is stopped and
masked during bootstrap. Root and proxy egress are still bounded by the EC2
security group, which only permits outbound TCP `80`, TCP `443`, and UDP `123`
(NTP) to any address. The agent runs as a non-root user with no sudo, so it
reaches root's broader path only if it first escalates to root.

`ssh_port_opened` controls inbound SSH access on port `22`. It must be `true` because
SSH tunneling is currently the only supported way to access the localhost admin
API/UI.

Normal HTTP requests are configured per domain in `allowed_network_access`.
`allow_http_methods` opens outbound HTTP over TCP port `80` and HTTPS over TCP port
`443` for the configured domain, subject to the listed methods and path restrictions.
The agent cannot resolve DNS names itself; a domain is resolved on the host's
controlled path only while an allowed request to it is being proxied.

```json
{
  "network_controls": {
    "ssh_port_opened": true,
    "managed_ai_provider_network_access": {
      "openai": true
    },
    "allowed_network_access": {}
  }
}
```

| Field | Required | Type | Behavior |
| --- | --- | --- | --- |
| `ssh_port_opened` | Yes | boolean | Whether SSH port `22` is opened for operator access. Deploy config must set this to `true`; otherwise the host would have no supported admin access path. |
| `managed_ai_provider_network_access` | No | object | Managed AI provider network bundles. Set `openai: true` to enable Codex/OpenAI access and `claude: true` to enable Claude Code/Anthropic access. Missing keys or explicit `false` disable that provider; the related runtime starts and remains `deactivated` until policy enables it. |
| `allowed_network_access` | Yes | object | Map of domain rules. Keys are exact domains or wildcard suffix domains. Wildcards must start with `*.`, such as `*.example.com`; `*` matches any non-empty hostname prefix ending at that dot. Embedded globs and regex keys are not supported. |

When `managed_ai_provider_network_access.openai` is `true`, Codex tasks can run
after Codex OAuth login. The host automatically adds these managed domain rules:

```json
{
  "api.openai.com": {
    "allow_http_methods": ["POST"],
    "openai_account_guard": true,
    "openai_disable_live_web_search": true
  },
  "auth.openai.com": {
    "allow_http_methods": ["GET", "POST"]
  },
  "chatgpt.com": {
    "allow_http_methods": ["GET", "POST"],
    "openai_account_guard": true,
    "openai_disable_live_web_search": true
  }
}
```

Do not list `openai.com`, `chatgpt.com`, or their subdomains in
`allowed_network_access`; the parser rejects those rules because they are managed
by `managed_ai_provider_network_access.openai`. The OpenAI live web search guard
and account guard are always applied to the managed API/data-plane domains. The
host infers the OpenAI account id from Codex login status instead of accepting
it in config. OpenAI data-plane requests are denied until that inferred account
id is available; `auth.openai.com` stays available for login.

When `managed_ai_provider_network_access.claude` is `true`, Claude Code tasks
can run after Claude OAuth login. The host automatically adds these managed
domain rules:

```json
{
  "api.anthropic.com": {
    "allow_http_methods": ["GET", "POST"],
    "anthropic_account_guard": true
  },
  "platform.claude.com": {
    "allow_http_methods": ["GET", "POST"],
    "path_guards": ["^/v1/oauth(?:/.*)?$"]
  }
}
```

Do not list `anthropic.com`, `claude.ai`, `claude.com`, or their subdomains in
`allowed_network_access`; the parser rejects those rules because they are
provider-owned by Claude Code. The managed bundle currently opens only
`platform.claude.com` OAuth paths plus `api.anthropic.com`; `claude.ai` is not
opened unless a future Claude Code denial proves it is required. The host verifies
`claude auth status`, infers the Claude account metadata from the agent user's
Claude config, stores only account metadata plus a SHA-256 hash of the OAuth
access token, and denies `api.anthropic.com` data-plane requests until the
presented bearer token matches that stored hash. The unauthenticated `/api/hello`
readiness probe remains available for Claude Code startup.

## Domain Rule

Each value in `allowed_network_access` is a domain rule.

Domain keys must be exact DNS names such as `api.example.com` or wildcard DNS names
such as `*.example.com`. A wildcard must be the first character and must be followed
by `.`, then a concrete DNS suffix. It matches any non-empty prefix before that
suffix, including dots, so `*.example.com` matches `api.example.com` and
`a.b.example.com`, but not `example.com`. If both the apex domain and its subdomains
are needed, configure both `example.com` and `*.example.com`.

Domain keys are normalized to lowercase. Wildcard domain rules must not overlap:
for example, `*.example.com` and `*.api.example.com` cannot both be configured,
because `x.api.example.com` would match both. Exact domain rules may coexist with
wildcards and override them for that exact hostname.

Prefer exact domains over wildcards. A wildcard lets the agent have the host resolve
any subdomain it chooses (for example `<data>.example.com`), which leaks that label to
the domain's DNS as a low-bandwidth side channel. Only use a wildcard for a domain
whose nameservers you trust not to be an exfiltration sink.

```json
{
  "allow_http_methods": ["GET", "HEAD"],
  "path_guards": ["^/dist(?:/.*)?$"]
}
```

| Field | Required | Type | Behavior |
| --- | --- | --- | --- |
| `allow_http_methods` | Yes | enum array | HTTP methods allowed for proxied requests to this domain. Valid values are `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, and `DELETE`. An empty array keeps HTTP/HTTPS closed for this domain. |
| `path_guards` | No | string array | Python `re` regular expressions for allowed request targets, evaluated with `re.fullmatch` against the path plus query string when present. If omitted, paths are not restricted beyond the domain and method rule. |

WebSockets use the same domain rule. A WebSocket connection starts with an HTTP
`GET` request that includes an upgrade header, so `GET` must be present in
`allow_http_methods` and the handshake path must pass `path_guards` when path guards
are configured. For `wss://` URLs, proxy `CONNECT` handling is internal to the host
and is not listed in `allow_http_methods`. After the upgrade succeeds, WebSocket
frames continue on the approved connection; they are not separate HTTP requests.
On managed OpenAI domains with the live web search guard, each client-to-upstream
WebSocket message is additionally inspected with the same live web search guard as
HTTP request bodies; a violating message closes the connection.

Path guards use Python `re` syntax and must match the full request target path. For
example, `^/dist(?:/.*)?$` allows `/dist` and `/dist/index.js`. If query strings are
allowed, include them in the regex, such as `^/simple(?:/.*)?(?:\\?.*)?$`.

For Codex, the host also restricts the agent runtime to cached web search, so
the OpenAI proxy guard is the second layer rather than the only one.
