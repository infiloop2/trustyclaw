"""Provisioning: the bootstrap template, its helpers, and deploy verification.

The shell pieces (bootstrap.sh, helpers/, agent-home/) are templates staged by
the lifecycle CLI; verify_deploy.py runs on the host at the end of bootstrap
and fails the deploy if the provisioned system state does not match what the
script was supposed to produce.
"""
