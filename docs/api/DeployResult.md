# Deploy Result

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
| `admin_password` | string | Random password generated on first deploy for the localhost UI and API. |

## Secret Handling

`admin_password` is printed only in the deploy result file. It must not be written to
service logs.

The operator should treat the deploy result file as sensitive because it contains the
admin password. The password is only useful with SSH access to the host, which requires
the matching private SSH key.
