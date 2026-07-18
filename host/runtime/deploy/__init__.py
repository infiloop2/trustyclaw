"""Deploy-time CLIs run by bootstrap as trustyclaw-admin, never as daemons.

Invoked with `python3 -m host.runtime.deploy.<module>` while provisioning:
schema migrations and effective-config computation. No socket surface.
"""
