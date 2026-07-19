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
# NOTE: `/` (32) is the measured off-window ceiling after the homepage read-path
# reductions. Keep this ratchet aligned with the dev baseline; production-like
# verification remains a follow-up because this work intentionally profiles the
# dev database first.
# `/consensus` was 80: the per-source overlay loop re-ran the whole source-detail
# pipeline — rebuilding the full consensus board — once per contributing source.
# Replaced with a single batched `get_source_overlays` pass that reuses the
# already-built board, bringing it to 43 here (and a larger win in prod, where
# the loop cost scaled with the number of sources).
ROUTE_BUDGETS: dict[str, int] = {
    # `/` HAS TWO REGIMES -- this 32 covers only the OFF-WINDOW one:
    #   * OFF-WINDOW (this budget, the year-round default): the Summer League
    #     Desk (#508/#548) fires ONE `events` lookup by key, finds no active
    #     event, and short-circuits to `None` before touching any other desk
    #     table. `representative_dataset` seeds no events/T1-T4 rows, so THIS
    #     test only ever measures this path -- 1 Desk query, well inside
    #     #548's 5-Desk-query-per-state contract (see
    #     `tests/integration/perf/test_desk_state_resolution_budget.py`, which
    #     also separately proves the OTHER honest off-window shape -- a
    #     dormant event whose `events` row already exists post-tick -- at 3).
    #   * IN-WINDOW (during a live SL event, e.g. Vegas 2026 Jul 9-19): `/`
    #     renders the full Desk. That composite is budgeted SEPARATELY in
    #     `DESK_HOME_PAGE_BUDGETS` below and asserted by
    #     `test_desk_home_inwindow_budget.py` (which seeds an active event) --
    #     NOT here, because this fixture can't put the Desk in-window.
    # #548 tightened the Desk's OWN added query cost (state resolution + the
    # one snapshot read) to <=5 for every state (Off-window/Preview/Live/
    # Recap/Wind-down) -- see `test_desk_state_resolution_budget.py`, which
    # measures that cost in isolation via `get_desk_view_from_snapshot`
    # directly rather than through the whole `/` route. This 32 and
    # `DESK_HOME_PAGE_BUDGETS`'s numbers still include the repo's known,
    # pre-existing `/` N+1 (see the module note above) -- these are regression
    # ratchets on the WHOLE route, not a claim the page is N+1-free.
    "/": 32,
    # Class Tracker tab-switch fragment (#567 JS fetch-and-swap): the SAME
    # single indexed snapshot read `/` makes for the Desk's tracker section,
    # nothing else — no consensus/news/hero queries. Off-window (this
    # fixture), `_resolve_window_state` short-circuits on its one `events`
    # lookup before touching any snapshot table, mirroring `/`'s off-window
    # regime above.
    "/desk/tracker": 1,
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
    # get_player_metric_seasons for the advanced table (1, indexed player_id) +
    # get_competition_id_for_player_year query on summer_league_player_seasons (1).
    # Shot-chart queries only fire when a SummerLeaguePlayerSeason row exists;
    # the perf dataset seeds game logs but no season rows, so the budget is 4.
    "/players/{slug}/summer-league/{year}": 4,
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
    # Counting modes fire venues + years + the aggregate; unpinned thresholds
    # walk the adaptive gate ladder (2+GP/60+MIN → 1/20 → 1/0) in the
    # aggregate's HAVING clause, re-running it once per empty rung — worst
    # case 5 on a thin/early scope. Advanced mode fetches its scope once and
    # walks the ladder in Python (competition list + has-rows probe + rows ≤ 3).
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

# Summer League Desk tick -- wall-clock duration budget (#629), per the
# project's two-minute Desk-tick target (docs/plans/
# summer-league-cron-desk-starvation-spec.md): the tick must stay well inside
# that window even under writer-lock contention (bounded to
# DEFAULT_WRITER_LOCK_MAX_WAIT_SECONDS=30s per wait, see
# scripts/sl_desk_tick.py). Asserted by
# tests/integration/perf/test_desk_tick_query_growth.py against a
# synthetic/no-network fixture, so this is a CI-hardware regression ratchet
# (catches an accidentally-reintroduced per-player/per-slot loop turning into
# real wall-clock cost), not a literal prod SLA measurement -- prod's actual
# two-minute budget also has to absorb real NBA Stats network I/O this
# fixture never exercises.
DESK_TICK_DURATION_BUDGET_MS = 120_000

# Admin route budgets (authentication-gated; tested separately via
# test_stubs_tab.py which sets up an admin session before rendering).
# Kept here as a single source-of-truth reference for the max query count.
ADMIN_ROUTE_BUDGETS: dict[str, int] = {
    "/admin/players/stubs": 10,
}

# Summer League Desk read service (#508) -- per-state query budgets for
# `app.services.summer_league.desk_read.get_desk_payload`, asserted directly by
# `tests/integration/test_sl_desk_home.py` (not by the generic, single-dataset
# `test_route_query_budgets.py` above -- that harness's seeded dataset never
# puts the Desk into an active state, so it only ever exercises "off_window").
#
# These are deliberately NOT folded into `ROUTE_BUDGETS["/"]`: doing so would
# either force that budget absurdly high (to cover the richest state) or hide
# a real per-state regression behind a single number that's only ever measured
# against one (off-window) fixture. Each number below is the EXACT count
# `tests/integration/test_sl_desk_home.py` measured against a minimal
# one/two-game, one/two-player fixture; it does not scale with the number of
# tracked players or games (no per-player, no per-game queries -- see
# `desk_read.py`'s module docstring), so this is a regression ratchet on
# *section count*, not on data volume.
DESK_HOME_QUERY_BUDGETS: dict[str, int] = {
    # Event lookup only, then short-circuit before touching any other desk
    # table -- the cheapest path by design.
    "off_window": 1,
    # Measured 16: state resolution (event + resolve_calendar_facts' own
    # event/competitions/game_dates/today lookups = 5) + baseline_version (1)
    # + freshness (1) + today's T4 slate (1) + games (1) + team entries (1) +
    # hero's T3 subject lookup (1) + Class Tracker's 5 batched queries
    # (roster/players/seasons/teams/grades -- #543: reads persisted T2 grades
    # instead of T1 baselines, same query count).
    "preview": 16,
    # Measured 17: preview's queries + the live board's one batched
    # top-performer query.
    "live": 17,
    # Measured 17: state resolution/baseline/freshness (7) + ledger_date (1)
    # + ledger game logs/players/baselines/facts (4) + Class Tracker (5).
    "recap": 17,
    # Measured 13 (raised from 11 by #544 "zero-signal days retain their
    # schedule"): state resolution/baseline/freshness (7) + T4 slate (1) +
    # games (1) + team entries (1) -- the quiet path now fetches/builds the
    # slate rows same as a signal-bearing day (only the HERO skips the
    # game-based build) -- + quiet-slate hero's T2 + players_master lookup
    # (2) + Class Tracker on an empty roster short-circuits after 1 query
    # (roster probe returns nothing -- no players/seasons/teams/grades
    # queries).
    "quiet_slate": 13,
}

# Summer League Desk -- FULL in-window `/` PAGE budgets (#508 follow-up), per
# resolved Desk state. Distinct from `DESK_HOME_QUERY_BUDGETS` above (which
# budgets `get_desk_payload` in ISOLATION): these are the whole `/` render --
# consensus hero, trending, news, podcasts, film room AND the active Desk --
# and are asserted by `test_desk_home_inwindow_budget.py`, which layers an
# active SL event on top of the full `representative_dataset`.
#
# Why this exists: `ROUTE_BUDGETS["/"]` (32) is measured against
# `representative_dataset`, which seeds NO events/T1-T4 rows, so it only ever
# exercises the Desk's OFF-WINDOW short-circuit (one `events` lookup, then
# `None`). During the SL event itself (Vegas 2026: Jul 9-19) `/` is in-window
# and renders the full Desk -- and nothing budgeted that composite until this
# dict. The two regimes are:
#   * OFF-WINDOW  -> `ROUTE_BUDGETS["/"] = 32` (the year-round default).
#   * IN-WINDOW   -> these numbers (only during a live SL event).
#
# SNAPSHOT-BACKED (#551, launch-readiness item 10): the in-window `/` render no
# longer live-assembles the Desk. `app.routes.ui.home` now calls
# `desk_read.get_desk_view_from_snapshot`, which resolves the current
# lifecycle/daily state fresh and then loads ONE already-materialized
# `event_desk_render_snapshots` row -- the whole payload + player/matchup/
# tracker-team view-context comes back decoded from that row's JSON columns,
# with NO per-player/per-game/grade/storyline enrichment afterward.
#
# The Desk-only work reduced the in-window page ceiling from 55 to 35 in this
# pass by removing duplicated consensus and enrichment reads from the homepage.
# Earlier #548 work had tightened the Desk contribution itself by removing a
# genuine duplicate
# query `_resolve_window_state` used to issue: it fetched the `events` row
# itself, then called the framework's generic `resolve_calendar_facts`, which
# turned around and re-fetched that SAME `events` row (plus a redundant full
# `summer_league_competitions` row-fetch it also didn't need) via its own
# internal `resolve_target_competitions` call, purely to re-derive
# `competition_ids` that `_resolve_window_state` already had in hand from the
# `events` row's own `calendar_ref`. `_resolve_window_state` now derives
# `competition_ids` once, up front, and calls the new
# `registry.calendar_facts_for_competition_ids` directly -- 2 queries
# (game_dates + today's schedule/statuses) instead of the old path's 4
# (redundant `events` refetch + competitions fetch + those same 2). Old
# allowances REMOVED: the prior page ceiling was 55 (state resolution 3 + 1
# snapshot read = 4 Desk queries atop the 51-query non-Desk `/` base), down from
# the pre-#551
# live-assembly total of 71. The Desk's own added cost per in-window request
# is now state resolution (3: events lookup + 2 calendar-fact reads) + 1
# snapshot read, plus one batched current-slate read for Live = 5 -- still
# inside #548's <=5-Desk-query-per-state contract for EVERY state
# (Off-window/Preview/Live/Recap/Wind-down), proven directly (in isolation from
# the rest of `/`) by
# `tests/integration/perf/test_desk_state_resolution_budget.py`. It does NOT
# scale with tracked players or games -- the snapshot read is a single row
# fetch regardless.
#
# NOTE: like `ROUTE_BUDGETS["/"]`, these totals are measured dev ceilings and
# include the remaining non-Desk homepage reads. They are not the final #561
# target of 15 statements; the remaining reductions require a larger homepage
# read-model pass and production-like validation.
DESK_HOME_PAGE_BUDGETS: dict[str, int] = {
    # Measured 36 for Live (one higher than the snapshot-only 35 after the
    # shared consensus/trending reads) because current game, box-line, and
    # player-identity facts are refreshed for the live slate on the request path.
    # off-window `/` baseline (32, which already includes the single off-window `events`
    # lookup) - 1 (that lookup) + 4 (state resolution 3 + one snapshot read).
    # Live adds one current-slate lookup; Recap remains snapshot-only.
    "live": 36,
    # Measured 35: Recap remains snapshot-only; its per-state assembly
    # differences live entirely at tick-materialization time, off the request
    # path.
    "recap": 35,
}
