# Philosophy

I made TrustyClaw because of a set of beliefs and hypotheses about running
AI agents. They also guide its design and evolution.

- **Rigor at the boundary, freedom inside.** The host, its tools, and the
  internet connection should get proper software engineering: review,
  audits, and understood data policies. Then the agent should have free rein
  inside.
- **Private by default.** AI should be private by default; any data egress
  should be intentionally configured by the user, with approval gating or
  explicit trust in a third party.
- **Always on, toward a measurable goal.** An agent should run continuously
  in an infinite loop toward a measurable goal; human intervention should
  be the exception, not turn-taking.
- **Maximal permission, asynchronous approvals.** AI should run with
  maximal permission by default. Sensitive actions like making payments
  and sending emails should require approval, but approval should be a
  queue, not a modal. The AI should achieve maximum runtime until human
  intervention is really needed.
- **Rich visual UX.** Human-agent interaction should happen through rich,
  interactive visual experiences, not a CLI.
- **Outcomes, not traces.** Users should only care about outcomes; internal
  traces should exist for developers to debug and optimize, not for users
  to babysit.
- **No model lock-in.** There should be no lock-in to a single model
  provider; users should be able to configure multiple models and auth
  sources with limits, and AI should automatically balance across them.
- **Restartable to a clean state.** AI makes mistakes and its internal
  state can get corrupted; a restart should reliably return it to a clean
  state where it starts working again.
- **Memory that humans can correct.** Persistent memory should be
  append-only for the AI, easy for the user to understand, and manually
  correctable.
