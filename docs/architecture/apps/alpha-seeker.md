# Alpha Seeker

Alpha Seeker is a durable financial-research workspace. It helps one human
maintain a live portfolio view, a watchlist of asymmetric ideas, cited research,
and prediction-market context. It never places trades.

The workspace starts with a useful default mandate and measurement. The agent
works on the human's request immediately; it asks only for details needed for
the current research. The human can change the mandate or measurement later.

## Product surfaces

- **Positions.** A current read-only view of Interactive Brokers positions and
  account totals, with source and UTC read time.
- **Watchlist.** One row per idea with thesis, price, target, catalysts, risks,
  conviction, and review status.
- **Research.** Dated, cited notes that explain changes rather than silently
  replacing the reasoning behind an idea.
- **Prediction markets.** Relevant Polymarket probabilities and history as a
  research signal, never as a trading surface.
- **Daily brief.** A pre-market schedule refreshes positions, revisits active
  ideas, checks relevant prediction markets, and appends a concise research note.

## Tools and boundary

Interactive Brokers and Polymarket are required read-only sources. Brave Search
is a should-have source for news and public context; the workspace represents
that priority as `good_to_have`, the shared workspace priority below
`must_have`. Every figure names its source and UTC timestamp.

No order-placement tool exists. The agent may propose an entry, exit, or hedge
inside the watchlist or research, but it never claims that a trade was placed.

## Artifacts

The four primary artifacts are purpose-built financial views, not generic prose:
positions is a sourced holdings table, watchlist is an editable thesis table,
research is a dated evidence timeline, and prediction markets is a probability
table. They use the shared typed view-block vocabulary so the human can inspect
and edit them directly while their meaning remains Alpha Seeker-specific.

For the shared workspace behavior behind goals, messages, artifacts, schedules,
memories, tools, agent runs, and authentication, see [Workspace Kit](workspace-kit.md).
