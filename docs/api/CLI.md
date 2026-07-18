# Lifecycle CLI

Lifecycle commands take arguments and standard environment variables; there
are no configuration files. Progress streams on stderr; stdout carries one
result JSON ([Deploy result](DeployResult.md)).

## Arguments

| Argument | Commands | Required | Behavior |
| --- | --- | --- | --- |
| `--agent-name <name>` | all | Yes | Stable host name. Must be 1-50 characters and contain only letters, numbers, hyphen (`-`), and underscore (`_`). Lifecycle commands use it to find the host and its preserved data volumes. |
| `--operator-ssh-public-key <key>` | `deploy`, `reconfigure` | At least one endpoint | OpenSSH `ssh-ed25519` or `ssh-rsa` public key content installed for persistent operator access. |
| `--operator-cloudflare-hostname <host>` | `deploy`, `reconfigure` | At least one endpoint | Exact Cloudflare-protected hostname that routes to the admin UI/API, for example `trustyclaw.example.com`. Wildcards are rejected. The tunnel token is read from `TRUSTYCLAW_CLOUDFLARE_TUNNEL_TOKEN` and encrypted in admin state so upgrade, recover, and reconfigure can recreate the tunnel service. |
| `--admin-password-sha256 <hex>` | `deploy`, `reconfigure` | Yes | SHA-256 hex digest of the admin password. The host stores only this hash; `python3 -m host.cli.generate_password` prints a generated password with its digest. |
| `--bootstrap-from-github [commit-sha]` | `deploy`, `upgrade`, `recover`, `reconfigure` | No | Provisions the instance from a pinned `infiloop2/trustyclaw` commit via EC2 user data instead of pushing the local checkout over SSH; without a value, the latest `main` commit is pinned. The pinned commit's `VERSION` is the operation's target and the CLI asks for confirmation. Pins older than `0.35.0` are rejected. |
| `--allow-upgrade` | `recover` | No | Allows no-instance recovery to advance preserved admin state from an older version to the target `VERSION`. |

## Environment variables

| Variable | Required | Behavior |
| --- | --- | --- |
| `AWS_REGION` | Yes | AWS region of the host; `AWS_DEFAULT_REGION` also works. The region is part of the agent's identity: its data volumes live there. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Yes | AWS credentials. |
| `AWS_SESSION_TOKEN` | For temporary credentials | Used exactly when set. A stale token next to fresh static keys fails closed at AWS with an authentication error; unset it for static-key runs. |
| `TRUSTYCLAW_CLOUDFLARE_TUNNEL_TOKEN` | With `--operator-cloudflare-hostname` | The Cloudflare Tunnel token. Secrets never ride in CLI arguments. |
