"""Standalone CLIs invoked as root through the sudo helper scripts.

Deliberately import nothing from the rest of the host package; each is a
single-purpose command with arguments validated by its helper wrapper.
"""
