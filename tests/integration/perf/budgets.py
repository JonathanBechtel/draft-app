"""Per-route query-count budgets for the public page surfaces.

HOW THIS WORKS (the "bump-the-number" protocol)
------------------------------------------------
Each entry is the maximum number of SQL statements a route may issue when
rendered against the representative dataset seeded in ``conftest.py``. The test
``test_route_query_budgets.py`` renders each route and fails if it exceeds its
budget.

A failing budget means your change added queries to that page. Before raising
the number, ask which case you are in:

  * Accidental N+1 / new serial query  -> FIX IT (batch the load, add
    ``selectinload``/``joinedload``, parallelize independent awaits, or move the
    query out of a per-row loop). Do not raise the budget.
  * A deliberate, necessary new query   -> RAISE the budget here, in the same
    diff. The bump is the point: it makes the added per-request cost visible in
    code review instead of letting page latency creep silently. Note why in the
    commit / PR.

These numbers are counts against the *seeded* dataset, not prod absolutes; their
value is regression detection on the delta. For real timing and query plans
against prod-like data, use ``scripts/explain_route.py`` (see the
``analyze-page-perf`` skill).

The history that motivated this guard: the homepage once shipped a 25-query
serial waterfall to prod. Postgres executed every query in <3ms — the cost was
the *count* (round-trips) plus a logging-config footgun, not slow SQL. Counting
queries is what would have caught the waterfall growing.
"""

from __future__ import annotations

# Route template -> max countable queries. ``{slug}`` is filled by the test from
# the seeded dataset.
# Numbers are the measured query count against the representative dataset as of
# this guard's introduction. They are a regression ratchet, not a target — if a
# refactor *lowers* a count, lower the budget too so the win is locked in.
#
# NOTE: `/` (52) and `/consensus` (80) are high relative to the seeded data
# volume and likely contain N+1 / per-row query patterns. The guard freezes them
# at today's level so they cannot get worse; reducing them is a separate
# optimization (profile with scripts/explain_route.py).
ROUTE_BUDGETS: dict[str, int] = {
    "/": 52,
    "/news": 8,
    "/podcasts": 5,
    "/players/{slug}": 24,
    "/players/{slug}/summer-league": 2,
    "/consensus": 80,
    "/stats/summer-league": 8,
    "/stats/summer-league/games": 5,
    "/stats/summer-league/{year}": 7,
    "/stats/summer-league/{year}/{venue}": 7,
    "/stats/summer-league/{year}/{venue}/{team}": 3,
}

# Admin route budgets (authentication-gated; tested separately via
# test_stubs_tab.py which sets up an admin session before rendering).
# Kept here as a single source-of-truth reference for the max query count.
ADMIN_ROUTE_BUDGETS: dict[str, int] = {
    "/admin/players/stubs": 10,
}
