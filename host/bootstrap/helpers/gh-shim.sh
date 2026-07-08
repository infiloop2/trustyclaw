#!/bin/sh
# /usr/local/bin/gh: run the real gh with a fixed placeholder GH_TOKEN. gh
# refuses to make authenticated API calls without a token, but the actual
# credential is injected by the network proxy on GitHub domains (any
# agent-supplied Authorization is stripped there), so the placeholder never
# reaches GitHub and the agent never holds the real token.
GH_TOKEN="trustyclaw-proxy-injected" exec /usr/bin/gh "$@"
