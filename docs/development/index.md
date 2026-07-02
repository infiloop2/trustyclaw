# Development

This folder contains contributor and maintainer workflows for TrustyClaw. For
runtime design and trust boundaries, start with
[Architecture](../architecture/index.md).

## Sections

| Doc | Contents |
| --- | --- |
| [Code layout](layout.md) | Repository layout and important runtime modules. |
| [CI testing](ci-testing.md) | Static type checks, unit tests, no-network CI, and local admin UI smoke. |
| [Fresh AWS smoke](fresh-aws-smoke.md) | Full deploy-from-scratch live AWS validation and one-time setup. |
| [Persistent AWS stage](persistent-aws-stage.md) | Long-lived staging host, provider-login checks, and start/stop workflows. |

This guide covers how the code is laid out, how to run the tests, and how to
set up the live AWS smoke and stage checks. For what the system does and how it
is structured, read [Architecture](../architecture/index.md) first.
