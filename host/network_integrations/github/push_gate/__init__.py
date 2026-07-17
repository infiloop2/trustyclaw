"""The ``.github`` push-approval gate, a component of the GitHub integration.

One vertical feature across three privilege domains:

- ``engine`` (proxy): inspects a buffered ``git-receive-pack`` body against a
  quarantine mirror, quarantines held objects, and synthesizes the
  report-status answer. Invoked by the GitHub guard's ``gate_response`` hook.
- ``pending`` (admin service): operator approve/reject of held pushes.
- ``approve`` (root helper): replays approved objects to GitHub — root has
  egress and reads the proxy-owned mirror; installed as the
  ``approve-github-push`` sudo helper.

This is the one deliberate exception to "integrations own no storage": the
engine writes ``pending_pushes`` rows and the on-disk quarantine mirror,
under the proxy's existing role and uid grants.
"""

from host.network_integrations.github.push_gate.engine import (
    GateError,
    GateResult,
    inspect,
    new_push_id,
)

__all__ = ["GateError", "GateResult", "inspect", "new_push_id"]
