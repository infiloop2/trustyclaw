# Control Planes

TrustyClaw has two operator-facing control planes with different authority.

## Operator plane

The operator plane is the local deployment environment plus AWS and configured
operator access. It has AWS credentials for the deploy IAM policy, any SSH
private key matching configured SSH endpoints, any Cloudflare Tunnel token used
for configured Cloudflare endpoints, and the local deploy or reconfigure result
file containing the cleartext admin password. This is the highest-authority
plane: it can create and destroy the EC2 instance, replace the root drive,
attach or delete preserved data drives, run bootstrap as root through temporary
SSH, inspect or repair host files, reconfigure the admin password and operator
endpoints, start or stop the EC2 instance, and recover from a broken or missing
host.

Deployments, upgrades, and recovery are therefore operator-plane actions. The
admin API intentionally does not own those flows; it cannot replace the root
drive, change AWS resources, rotate operator endpoints, or repair arbitrary
root-owned host code if that code is broken.

## Admin plane

The admin plane is the admin API/UI reached through SSH forwarding or
Cloudflare Access and authenticated with the admin bearer password. It is lower authority than the
operator plane and is meant for normal host operation after deploy. It can
create, steer, cancel, and inspect agent tasks; read task and network events;
manage runtime network policy; start provider login flows and read provider
account summaries; synchronize provider account pins into proxy state; inspect
health; and request a host reboot.

Admin-plane host controls cross privilege boundaries only through fixed
root-owned helpers. That gives the admin API enough authority for routine
operation, such as replacing network policy or rebooting, without giving it
general root access, AWS credentials, or the ability to mutate trusted
root-volume code.
