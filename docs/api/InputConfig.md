# Input Config

## Top-Level Object

```json
{
  "agent_name": "trustyclaw-dev-agent",
  "aws_region": "us-east-1",
  "aws_access_key_id_env": "AWS_ACCESS_KEY_ID",
  "aws_secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
  "ssh_public_key": "ssh-ed25519 AAAA...",
  "ssh_port_opened": true
}
```

| Field | Required | Type | Behavior |
| --- | --- | --- | --- |
| `agent_name` | Yes | string | Stable host name. Must be 1-50 characters and contain only letters, numbers, hyphen (`-`), and underscore (`_`). The deploy command uses it to identify the EC2 machine. If it already exists, deploy prompts before deleting and recreating it. |
| `aws_region` | Yes | string | AWS region where the EC2 host is deployed. |
| `aws_access_key_id_env` | Yes | string | Name of the environment variable containing the AWS access key id. |
| `aws_secret_access_key_env` | Yes | string | Name of the environment variable containing the AWS secret access key. |
| `ssh_public_key` | Yes | string | SSH public key installed for operator access. This is the key content, not a file path. |
| `ssh_port_opened` | Yes | boolean | Must be `true`. SSH port `22` is opened for operator access because SSH tunneling is currently the only supported admin API/UI access path. Deploy rejects `false`. |

Deployment config does not accept network policy. A fresh host starts with an
empty runtime network policy, which gives the agent no website or managed AI
provider access. If the preserved admin volume already contains
`network_controls.json`, bootstrap reuses that policy on redeploy.

Runtime network policy is documented separately in
[`NetworkControls.md`](NetworkControls.md).
