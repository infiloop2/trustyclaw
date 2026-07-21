"""Standalone CLIs invoked as root through the sudo helper scripts.

Deliberately import nothing from the rest of the host package; each is a
single-purpose command with arguments validated by its helper wrapper. The
one exception is ``aws_account``, whose SigV4 signing comes from
``host.runtime.core.aws_sigv4`` because the network proxy's Bedrock guard
must share the exact same canonicalization.
"""
