# Input Config

## Deploy and Operator Reconfiguration Config

```json
{
  "agent_name": "trustyclaw-dev-agent",
  "aws_region": "us-east-1",
  "aws_access_key_id_env": "AWS_ACCESS_KEY_ID",
  "aws_secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
  "operator_connections": [
    {
      "mode": "ssh",
      "ssh_public_key": "ssh-ed25519 AAAA..."
    },
    {
      "mode": "cloudflare_access",
      "hostname": "trustyclaw.example.com",
      "tunnel_token_env": "TRUSTYCLAW_CLOUDFLARE_TUNNEL_TOKEN"
    }
  ]
}
```

| Field | Required | Type | Behavior |
| --- | --- | --- | --- |
| `agent_name` | Yes | string | Stable host name. Must be 1-50 characters and contain only letters, numbers, hyphen (`-`), and underscore (`_`). |
| `aws_region` | Yes | string | AWS region where the EC2 host is deployed. |
| `aws_access_key_id_env` | Yes | string | Name of the environment variable containing the AWS access key id. |
| `aws_secret_access_key_env` | Yes | string | Name of the environment variable containing the AWS secret access key. |
| `operator_connections` | Yes for `deploy` and `reconfigure` | array | One or more operator access endpoints. At most one endpoint per mode is currently allowed. |
| `operator_connections[].mode` | Yes | enum | `ssh` or `cloudflare_access`. |
| `operator_connections[].ssh_public_key` | Yes when mode is `ssh` | string | SSH public key installed for persistent operator access. This is the key content, not a file path. |
| `operator_connections[].hostname` | Yes when mode is `cloudflare_access` | string | Exact Cloudflare-protected hostname that routes to the admin UI/API, for example `trustyclaw.example.com`. Wildcards are rejected. |
| `operator_connections[].tunnel_token_env` | Yes when mode is `cloudflare_access` | string | Name of the local environment variable containing the Cloudflare Tunnel token. The token is encrypted in admin state so upgrade, recover, and reconfigure can recreate the tunnel service. |

## Upgrade, Recover, and Power Config

```json
{
  "agent_name": "trustyclaw-dev-agent",
  "aws_region": "us-east-1",
  "aws_access_key_id_env": "AWS_ACCESS_KEY_ID",
  "aws_secret_access_key_env": "AWS_SECRET_ACCESS_KEY"
}
```

`upgrade`, `recover`, `start`, and `stop` intentionally omit
`operator_connections`. Passing operator endpoints to these commands is a
configuration error. `upgrade` and `recover` preserve the endpoints stored in
admin state. `recover` recreates a missing host from preserved volumes without
changing the admin password or operator access. `start` and `stop` only change
EC2 instance power state.

`reconfigure` uses the deploy/reconfigure config shape above and
always takes the full desired `operator_connections` list. It installs a new
generated admin password unless `--admin-password-env` is supplied.
`reconfigure` requires an existing TrustyClaw instance; use `recover` first if
the host is missing.

SSH ingress is kept only when the final stored endpoint list contains an `ssh`
connection. When only `cloudflare_access` is configured, deploy uses a
single-use SSH key during provisioning, removes it after bootstrap, and revokes
EC2 security-group SSH ingress before returning success.

Deployment config does not accept network policy. A fresh host starts with an
empty runtime network policy, which gives the agent no website or managed AI
provider access. A preserved admin volume keeps its stored policy (in the
admin-state database) across redeploys.

Runtime network policy is documented separately in
[`NetworkControls.md`](NetworkControls.md).
