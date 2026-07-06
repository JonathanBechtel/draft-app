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
# NOTE: `/` (52) is high relative to the seeded data volume and likely still
# contains N+1 / per-row query patterns; the guard freezes it at today's level
# so it cannot get worse (reducing it is a separate optimization — profile with
# scripts/explain_route.py).
# `/consensus` was 80: the per-source overlay loop re-ran the whole source-detail
# pipeline — rebuilding the full consensus board — once per contributing source.
# Replaced with a single batched `get_source_overlays` pass that reuses the
# already-built board, bringing it to 43 here (and a larger win in prod, where
# the loop cost scaled with the number of sources).
ROUTE_BUDGETS: dict[str, int] = {
    "/": 52,
    "/news": 8,
    "/podcasts": 5,
    # +1 over the prior 24 for the SL advanced-metrics read
    # (get_player_metric_seasons): one indexed lookup on
    # summer_league_player_seasons by player_id.
    # +1 over 25 for the career shot-zone query (get_player_shotchart_context):
    # one indexed lookup on summer_league_shot_events by player_id.  When the
    # player has no shot events (total_fga == 0) the function returns None
    # immediately without issuing the shot-diet follow-up query.
    "/players/{slug}": 26,
    "/players/{slug}/summer-league": 2,
    # Per-season page: resolve_player_ref (1) + get_player_game_logs (1) +
    # get_competition_id_for_player_year query on summer_league_player_seasons (1).
    # Shot-chart queries only fire when a SummerLeaguePlayerSeason row exists;
    # the perf dataset seeds game logs but no season rows, so the budget is 3.
    "/players/{slug}/summer-league/{year}": 3,
    "/consensus": 43,
    # Hub: combine-year coverage + SL-year coverage, one indexed read each.
    "/stats/": 2,
    # +2 over the base 8: the landing renders two leader boards (all-time +
    # latest season) whose adaptive gate falls back to 1+ GP when the standard
    # cut matches nobody — one extra aggregate each, worst case (thin/early data).
    "/stats/summer-league": 10,
    "/stats/summer-league/games": 5,
    # Box score: header query (1) + player lines (1) + team totals (1) +
    # shot-zone aggregation (1) + game-flow PBP events (1) = 5 indexed reads.
    # The perf dataset seeds no shot events, so total_fga=0 and the shot-dot
    # follow-up query is skipped (budget would be 6 on a game with both shots
    # and PBP). The PBP query always fires; it returns empty → no chart rendered.
    "/stats/summer-league/{year}/games/{game_id}": 5,
    # Both counting (aggregate + years) and advanced (competition list +
    # per-competition rows) modes fire 2 indexed queries. Unpinned thresholds
    # walk the adaptive gate ladder (2+GP/60+MIN → 1/20 → 1/0), re-running the
    # aggregate once per empty rung — worst case +2 on a thin/early scope.
    "/stats/summer-league/leaders": 5,
    # +1 each: the season/venue mini leader boards retry once at 1+ GP when the
    # standard gate matches nobody (early-competition fallback).
    "/stats/summer-league/{year}": 8,
    "/stats/summer-league/{year}/{venue}": 8,
    # Header + schedule + stats roster + announced roster (A4 pre-event preview,
    # one indexed read on summer_league_participation by team_entry_id) = 4.
    "/stats/summer-league/{year}/{venue}/{team}": 4,
    # Franchise history: header + entries + games + top-performers + career
    # player aggregates = 5 indexed reads.
    "/stats/summer-league/teams/{team}": 5,
    # Explorer: 7 facet lookups (years/venues/draft-classes/positions/countries/teams/round-types)
    # + 1 COUNT(*) subquery + 1 aggregate rows query. SQL-side pagination (replacing the old
    # Python _paginate(), which fetched every row into memory) requires a separate COUNT to
    # report the total without materializing the full result set — a deliberate +1 over the
    # prior all-rows-in-Python approach.
    # +1 for _fetch_adv_counts: the N-of-M banner (#406) needs eligible_n + total_m over
    # SummerLeagueMetricContext, folded into a single grouped count query.
    "/stats/summer-league/explorer": 10,
}

# Admin route budgets (authentication-gated; tested separately via
# test_stubs_tab.py which sets up an admin session before rendering).
# Kept here as a single source-of-truth reference for the max query count.
ADMIN_ROUTE_BUDGETS: dict[str, int] = {
    "/admin/players/stubs": 10,
}
