"""Generic pipeline orchestration primitives — batching, locks, state, telemetry.

These modules are source-agnostic in role even though today Summer League is
their only caller: durable batch-completion tracking, transaction-scoped
writer locks, scheduled-writer coordination state, and structured timing
telemetry. A second source spoke reuses this layer rather than re-inventing
its own copy. See ``docs/plans/summer-league-journey-graph-alignment.md`` §4.
"""
