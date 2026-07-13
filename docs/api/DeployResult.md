# Lifecycle Result

Each lifecycle command prints the result path it wrote. The default is
`<agent_name>-<mode>.json`, where mode is `deploy`, `upgrade`, `recover`,
`reconfigure`, `start`, or `stop`. `--result-file <path>` overrides the path
and may overwrite an existing file.

## Provisioning result

`deploy`, `upgrade`, `recover`, and `reconfigure` replace or create a host and
return this shape:

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

| Field | Presence | Behavior |
| --- | --- | --- |
| `agent_name`, `instance_id`, `region` | Always | Input host name and the created/replacement EC2 instance identity. |
| `public_dns` | Always | EC2 public DNS name used for SSH access when an SSH endpoint is configured. |
| `ssh_user` | Always | `trustyclaw-operator`. |
| `admin_ui_local_url` | Always | Local URL after forwarding the admin port: `http://127.0.0.1:7443`. |
| `admin_volume_id`, `agent_volume_id` | Always | Durable EBS volume ids attached to the host. |
| `version` | Always | Repository `VERSION` installed by this provisioning command. |
| `operator_connections` | `deploy`, `reconfigure` | Public summary of the replacement endpoint list. Tunnel tokens and SSH key material are omitted. Upgrade/recover preserve the stored list and omit this field. |
| `admin_password` | `deploy`, `reconfigure` | Cleartext admin password, read from `--admin-password-env` when supplied or generated otherwise. Upgrade/recover preserve the password and omit this field. |

## Power result

`start` and `stop` do not run bootstrap or install a version. They report the
existing instance's power transition:

```json
{
  "agent_name": "trustyclaw-dev-agent",
  "instance_id": "i-0123456789abcdef0",
  "region": "us-east-1",
  "operation": "start",
  "initial_state": "stopped",
  "state": "running",
  "public_dns": "ec2-203-0-113-10.compute-1.amazonaws.com",
  "public_ip": "203.0.113.10",
  "ssh_user": "trustyclaw-operator",
  "admin_ui_local_url": "http://127.0.0.1:7443",
  "admin_volume_id": "vol-0123456789abcdef0",
  "agent_volume_id": "vol-0fedcba9876543210"
}
```

| Field | Presence | Behavior |
| --- | --- | --- |
| `agent_name`, `instance_id`, `region` | Always | Input host name and existing EC2 instance identity. |
| `operation` | Always | `start` or `stop`. |
| `initial_state`, `state` | Always | EC2 state before the command and after the requested transition. |
| `public_dns`, `public_ip` | When AWS reports a non-empty value | Current public address metadata. A stopped instance may omit either field. |
| `ssh_user`, `admin_ui_local_url` | Always | Operator SSH identity and the local forwarded admin URL. |
| `admin_volume_id`, `agent_volume_id` | When the tagged volume is found | Durable volume ids associated with the host. Valid power operations require both volumes, so normal results contain both. |

Power results never contain `version`, `operator_connections`, or
`admin_password` because the commands do not change or read those values.

## Secret handling

Only deploy and reconfigure result files contain `admin_password`; keep them
private. With SSH access, the matching private SSH key is also required. With
Cloudflare Access, the operator must pass the Access identity policy and then
enter the TrustyClaw admin password. Lifecycle result files are created mode
`0600`.
