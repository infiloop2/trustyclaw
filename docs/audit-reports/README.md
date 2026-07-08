# Audit Reports

This folder holds recurring AI/human audit reports for TrustyClaw, one document
per audit axis. Each axis document states a fixed audit question and threat
model, followed by each reviewer's current audit report.

| Axis | Document |
| --- | --- |
| Security: agent isolation from other user data | [01-security-agent-isolation.md](01-security-agent-isolation.md) |
| Security: network proxy policy enforcement | [02-security-network-policy.md](02-security-network-policy.md) |
| Security: admin UI browser surface | [03-security-admin-ui.md](03-security-admin-ui.md) |
| Product UX: settings clarity, no surprises | [04-ux-settings-clarity.md](04-ux-settings-clarity.md) |
| Reliability: resource isolation and recovery | [05-reliability.md](05-reliability.md) |

## How a sweep works

1. Check out a clean tree at a known commit. All findings and line references
   are against that commit.
2. Pick one axis document. Read its audit question, threat model, and scope
   before reading any code. The threat model is binding: findings outside the
   stated scope belong in a different axis or are out of scope entirely.
3. Review the code. Reading prior entries in the document is encouraged, but
   form your own findings first so earlier sweeps do not anchor you.
4. Add your report at the **top** of the document's *Audit entries* section
   using the template below. If you (the same model) already have a report in
   the document, **replace it** with the new one — the document holds only
   each reviewer's current report, and git history keeps the old versions.
   Never edit or delete another reviewer's report.
5. A resweep is just a fresh report: findings still present at your commit go
   in the new table, and anything from your old report that is absent from
   the new one is thereby recorded as resolved or withdrawn. There is no
   separate resolution tracking.

## Entry template

```markdown
## <YYYY-MM-DD> — <model name> — `<commit hash>`

Reviewer: <model name and version, e.g. "Claude Fable 5 (claude-fable-5)";
for humans, name or handle>
Commit: `<full or 12-char hash of the reviewed tree>`
Methodology: <static code reading / grep sweeps / ran the system / wrote or
ran tests or PoCs — list all that apply, with a sentence on each>

### What was reviewed

Enumerate the concrete surface you examined: files, entry points, config
paths, listeners, helpers. Be specific enough that a later reviewer can tell
what your sweep does and does not vouch for.

### Findings

| ID | Status | Severity | Location | Summary |
| --- | --- | --- | --- | --- |
| <AXIS>-NNN | Open | High | `path/file.py:123` | Defect and its concrete failure scenario. |

If there are no findings, state that explicitly. The table is the complete
findings output — no per-finding subsections. The Summary cell must carry the
concrete failure scenario (what triggers it, what goes wrong) in a sentence
or two, plus a suggested fix when obvious; a finding whose Summary has no
concrete scenario is not a finding.

Finding IDs use the axis prefix (`ISO`, `NET`, `UI`, `UX`, `REL`) and a
sequence number, numbered sequentially within your report.

Finding status is the current triage state for the finding:

| Status | Meaning |
| --- | --- |
| Open | The finding is still believed to apply to the reviewed tree and needs follow-up. |
| Fixed | The finding has been fixed in the reviewed tree. Keep this only when a resweep explicitly verifies the fix. |
| Wontfix | The finding is accepted but will not be fixed. Use the Summary or Coverage section to explain why. |

### Coverage and confidence

How you know what you did not miss. List the checks you performed against the
axis's scope checklist, the surface you deliberately did not review and why,
and any areas where your confidence is low. "I reviewed everything carefully"
is not acceptable; an inventory is.
```

## Severity scale

| Severity | Meaning | Anchor examples |
| --- | --- | --- |
| Critical | The axis's core guarantee is broken and exploitable in a default or documented configuration. | Agent reads another Unix user's secrets; proxy passes traffic to a host the policy denies; admin UI lets a third-party page act with the operator's credentials. |
| High | Core guarantee broken, but only under an unusual-yet-plausible configuration, race, or precondition. | Policy bypass requiring a specific rule shape an operator could reasonably write; UI setting whose actual effect contradicts its label. |
| Medium | A real weakness that needs a second bug or an unlikely precondition to break the guarantee, or meaningfully weakens defense in depth. | A missing validation the next layer currently catches; unbounded growth that takes months to matter. |
| Low | Hardening gap or deviation from stated design with no identified path to breaking the guarantee. | Overly broad file mode on a non-secret; confusing but technically accurate UI copy. |
| Info | Observation worth recording; not a defect. | Documentation drift; suggested test coverage. |

## Ground rules

- Entries are self-contained: a reader must be able to evaluate an entry
  without your session transcript.
- Report what you observed, not what the documentation says should happen.
  Where `docs/architecture/` disagrees with the code, that is itself a finding
  (usually Info or Low, higher if the docs promise a guarantee the code lacks).
- Do not pad. Five verified findings beat twenty speculative ones; a clean
  sweep with a rigorous coverage section is a useful result.
