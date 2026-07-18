"""Shared runtime libraries: storage, state, and policy primitives.

Nothing in this package binds or serves a socket. Modules here are imported
by the service packages; each socket surface is owned by exactly one service
package (see host/runtime/__init__.py).
"""
