# Deploy Result

Each lifecycle command prints the result path it wrote. By default, result
files are mode-specific: `deploy` writes `<agent_name>-deploy.json`, `upgrade`
writes `<agent_name>-upgrade.json`, `recover` writes
`<agent_name>-recover.json`, `reconfigure` writes
`<agent_name>-reconfigure.json`, `start` writes `<agent_name>-start.json`, and
`stop` writes `<agent_name>-stop.json`.
`--result-file <path>` overrides the path explicitly and may overwrite an
existing file.

## Result Object

```json
{
  "agent_name": "trustyclaw-dev-agent",
  "instance_id": "i-0123456789abcdef0",
  "region": "us-east-1",
  "public_dns": "ec2-203-0-113-10.compute-1.amazonaws.com",
  "ssh_user": "trustyclaw-operator",
  "admin_ui_local_url": "http://127.0.0.1:7443",
  "admin_volume_id": "vol-0123456789abcdef0",
  "agent_volume_id": "vol-0fedcba9876543210",
  "version": "x.y.z",
  "operator_connections": [
    {"mode": "ssh"},
    {"mode": "cloudflare_access", "hostname": "trustyclaw.example.com"}
  ],
  "admin_password": "generated-password"
}
```

| Field | Type | Behavior |
| --- | --- | --- |
| `agent_name` | string | Host name from the input config. |
| `instance_id` | string | EC2 instance id created or recreated by deploy. |
| `region` | string | AWS region where the instance was deployed. |
| `public_dns` | string | EC2 public DNS name used for SSH access. |
| `ssh_user` | string | SSH user for operator access. First version uses `trustyclaw-operator`. |
| `admin_ui_local_url` | string | Localhost URL for the admin UI after port forwarding. |
| `admin_volume_id` | string | EBS volume id for durable admin/API state and event logs. |
| `agent_volume_id` | string | EBS volume id for durable agent home and workspace state. |
| `version` | string | TrustyClaw repo `VERSION` installed by this command. |
| `operator_connections` | array | Public summary of operator endpoints installed from the input config. Present when the command takes replacement operator endpoints. Cloudflare tunnel tokens and SSH key material are omitted. |
| `admin_password` | string | Admin password for the UI and API. Present for `deploy` and `reconfigure`. For both commands it is read from `--admin-password-env` when supplied, otherwise generated. Omitted for commands that preserve the existing password. |

## Secret Handling

`admin_password` is printed only in deploy and reconfigure result files. It must
not be written to service logs.

The operator should treat deploy and reconfigure result files as sensitive
because they contain the admin password. With SSH access, the matching private
SSH key is also required. With Cloudflare Access, the user must pass Cloudflare
Access authentication and then enter the TrustyClaw admin password.
