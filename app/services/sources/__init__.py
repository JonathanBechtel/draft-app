"""Per-source adapters — one subpackage per data spoke.

Each source (Summer League, FIBA, AAU, college, NBA) is an *adapter* whose only job is to
translate its raw feed into canonical assertions on the shared backbone. No source keeps a
parallel store; everything downstream is generic and source-blind. That is principle P3 in
``docs/plans/north-star-architecture.md``.

It is deliberately empty today. Phase 4 of ``docs/plans/summer-league-remediation-roadmap.md``
reorganizes the existing Summer League services into ``sources/summer_league/``.

**Contract (enforced by import-linter, ``.importlinter`` ``spokes-are-mutually-independent``):**
sibling spokes may not import one another. Two spokes that need the same thing must get it from
the hub, not from each other. The contract is vacuous at one spoke and that is exactly why it is
installed now — spoke #2 inherits the constraint instead of discovering it later. See
``docs/plans/programmatic-code-discipline.md`` §3.1.
"""
