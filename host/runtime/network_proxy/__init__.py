"""The trustyclaw-proxy service process.

Owns the agent egress proxy on 127.0.0.1:PROXY_PORT: the only code that acts
on agent network requests, applying the fail-closed network policy before any
byte leaves the host.
"""
