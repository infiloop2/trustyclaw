"""Network integrations: one package per integration.

A network integration is one provider-specific slice of policy plus the
hand-written guard code that decides requests for its domains. Each
integration lives in its own package here with two modules:

- ``manifest.py`` (pure): the integration's static identity, owned domain
  apexes, typed config parser, and denial catalog. It is importable from
  config/CLI context and must not import ``host.runtime``.
- ``guard.py`` (runtime): the exact host, route, and provider-specific
  decisions the proxy dispatches to. It runs inside the proxy process and may
  read proxy-visible state.

Unlike bundled tools and apps, integrations are not discovered at runtime.
The registry in ``registry.py`` and guards in ``runtime.py`` are hand-written
because this code runs with the proxy's privileges and sees requests including
bearer credentials. Adding an integration is a reviewed edit, never a drop-in.
Unit tests discover every package manifest and require an explicit registry
and guard entry, unique identity, unique denial codes, and disjoint managed
apexes.

Integrations may add proxy-readable tables when their guard needs state. The
admin-owned migration system creates those tables and grants narrow access.
The GitHub push gate (``github/push_gate``), for example, quarantines held
pushes and enqueues approval rows under the proxy's existing grants.

The ``custom`` integration is the catch-all. Its config is the operator's
``network_integrations.custom.domains`` map, and it applies the same direct
guard contract to hosts outside every managed apex. No cross-integration
domain-rule representation exists.
"""
