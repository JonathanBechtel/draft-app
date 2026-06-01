---
name: analyze-page-perf
description: Analyze a page's database/performance implications — check query-count budgets and profile the actual SQL a route fires. Use when changing a page's data loading ("new plumbing"), investigating a slow page, or verifying a change didn't add query cost.
allowed-tools: Bash, Read, Edit
---

# Analyze Page Performance

Two complementary tools guard page performance in this repo. Reach for the one
that fits, and know why each exists.

## TL;DR — which tool

- **Adding or changing a query?** → EXPLAIN it against prod-like data
  (`make explain ROUTE=<page>`) and confirm it uses an index, not a Seq Scan, on
  any large table. This is a standard step when writing a query, not just a
  break-glass for slow pages. See [Adding a new query](#adding-a-new-query--verify-the-index).
- **Did my change add query *count*?** → run the **query-budget tests**
  (`make perf`). Deterministic, runs against a seeded DB, catches N+1s and
  waterfall growth. The default check after touching a page's data loads.
- **Why is a page slow / what plans is it using?** → run
  **`make explain ROUTE=<page>`** against prod-like data for real timings and
  `EXPLAIN ANALYZE` plans.

## Adding a new query — verify the index

Whenever you add or change a query (typically in `app/services/`), confirm it is
properly indexed *before* you consider it done. The May-2026 incident's root
cause was an index that existed on the wrong column (`created_at`) while the new
query filtered another (`published_at`) — a Seq Scan on every render.

```bash
# Point EXPLAIN_DATABASE_URL at a Neon prod read-branch in .env first — index
# verdicts are only faithful at prod-like volume (see the caveat below).
make explain ROUTE=/consensus            # EXPLAIN ANALYZE every query the page fires
make explain ROUTE=/consensus ARGS="--top 5"
```

Steps:
1. Write the query. Find the page (route) that exercises it.
2. `make explain ROUTE=<that route>`. Locate your new query in the output.
3. Read its plan. Red flags the tool already surfaces:
   - **Seq Scan on a large table** → the filtered/joined column is not indexed
     (or an existing index does not cover it). Most common, most important.
   - **Row-estimate miss (>=10x)** → stale stats or a poorly-matched index.
   - **External sort / high heap fetches** → consider a covering or composite index.
4. If an index is missing, add it to the SQLModel table in `app/schemas/`
   (`Field(index=True)` for a single column, or an `Index(...)` in
   `__table_args__` for a composite) **and** generate an Alembic migration in the
   same change (see CLAUDE.md → Migration Workflow).
5. Re-run `make explain` and confirm the plan now shows an **Index Scan** /
   **Index Only Scan** instead of the Seq Scan.

> **Why prod-pointed, always:** Postgres correctly prefers a Seq Scan on a small
> table even when a perfect index exists, so an EXPLAIN against dev/test volume
> will show Seq Scans everywhere and tell you nothing about indexing. Only
> prod-like row counts make the planner's index choice meaningful. Set
> `EXPLAIN_DATABASE_URL` to a Neon prod read-only branch.
>
> If the new query is not reachable from a public route yet, temporarily expose
> it, or run the captured SQL directly under `EXPLAIN (ANALYZE, BUFFERS)` against
> the prod branch.

## The hard-won lessons (read before diagnosing "slow")

A May 2026 prod incident drove the design of these tools. The homepage was
rendering in ~3s. The instinct is "find the slow query" — that was wrong:

1. **Postgres executed every query in <3ms.** There was no slow query. The cost
   was the **count** — a 25-query *serial* waterfall — plus a logging-config
   footgun (`sql_echo=True` in prod, ~500 synchronous stdout writes per render).
2. **Separate wall-clock from Postgres execution time.** If a query's wall-clock
   is 175ms but its plan shows 2ms execution, the other 173ms is round-trip /
   logging / serialization overhead — not something an index fixes. The fix is
   *fewer round-trips* (batch, `selectinload`/`joinedload`, `asyncio.gather`
   independent loads) or removing the overhead.
3. **Query count is the signal that travels.** It is identical regardless of data
   volume, so it is testable locally; timing is not. Local/CI timing does not
   reproduce prod (network distance, hardware). That is why the automated guard
   counts queries and the timing tool is manual + prod-pointed.

So when a page is slow: suspect **count / waterfall** and **config overhead**
*before* query cost. Confirm with the count delta first, then plans.

## Tool 1 — Query-budget guard (automated)

Files live in `tests/integration/perf/`:
- `budgets.py` — `ROUTE_BUDGETS`: max queries per public route.
- `conftest.py` — `representative_dataset`: a non-empty seed so per-row loops fire.
- `test_route_query_budgets.py` — renders each route, fails if over budget.

Run it (needs the test DB; `make perf` wraps the env):

```bash
make perf
```

**When a budget test fails**, you are in one of two cases:
- **Accidental N+1 / extra serial query** → fix it (batch the load, add eager
  loading, parallelize independent awaits). Do *not* raise the budget.
- **A genuinely required new query** → raise `ROUTE_BUDGETS[<route>]` in
  `budgets.py` **in the same diff**. The bump makes the added per-request cost
  visible in review — that is the point, not a rubber stamp.

**Adding a new page to the guard:** add a `"/route": <budget>` entry to
`ROUTE_BUDGETS`, ensure `representative_dataset` seeds data that route needs,
run `make perf`, and set the budget to the reported count.

## Tool 2 — explain_route.py (manual deep-dive)

`scripts/explain_route.py` runs a route in-process, captures every SELECT it
fires, and runs `EXPLAIN (ANALYZE, BUFFERS, VERBOSE)` on each — ranked slowest
first, flagging Seq Scans, row-estimate misses, and external sorts.

```bash
# Add a Neon prod read-only branch to .env for realistic plans (dev volume won't
# reproduce prod):
#   EXPLAIN_DATABASE_URL='postgresql://<readonly>@<neon-host>/draftguru'
make explain ROUTE=/ ARGS="--no-plans"   # timing table only
make explain ROUTE=/ ARGS="--top 5"      # worst 5 queries + plans
# (make explain wraps scripts/with-db-env.sh + scripts/explain_route.py)
```

`EXPLAIN ANALYZE` executes the query — safe for SELECTs (the script rejects DML
and writable CTEs), but only point it at a DB you can afford full reads on.

## Recommended workflow for a page change

1. Make the change.
2. **Added/changed a query?** `make explain ROUTE=<page>` against the prod branch
   and confirm your query uses an index, not a Seq Scan. Add the index +
   migration if missing (see [Adding a new query](#adding-a-new-query--verify-the-index)).
3. `make perf` — did the route's query *count* move? If it rose unexpectedly,
   you added an N+1 or a serial query; fix or consciously bump the budget.
4. If the page is (or feels) slow, read **plan execution time vs wall-clock** in
   the `make explain` output before blaming any single query.
5. Report the perf implication explicitly (query-count delta; any new Seq Scan).
