# DB Transaction Refactor Plan (Request-Bounded First)

Last updated: 2026-01-25

## Summary / Goal

DraftGuru’s **request-bounded application code** (FastAPI routes + services) should consistently use the preferred transaction convention:

- Prefer explicit transaction scopes (e.g., `async with db.begin(): ...`).
- Avoid scattered `await db.commit()` / `await db.rollback()` in request code.

Today, many explicit commits exist primarily because the **integration test fixture pre-opens a transaction + nested savepoint**, which historically made `db.begin()` raise `InvalidRequestError`. We want the inverse relationship: **production app semantics lead**, and tests change to match.

**Priority order:**

1. Request-bounded app code (`app/routes/**`, `app/services/**`)
2. Integration test harness + tests (`tests/integration/**`)
3. Non-request code (CLI/scripts) — last priority (`app/cli/**`, `scripts/**`)

---

## Status (2026-01-25)

Completed:

- Integration test harness updated to be production-like:
  - Isolation via `TRUNCATE ... RESTART IDENTITY CASCADE` per test
  - Fresh DB session per request in `app_client`
  - Schema isolation via a per-session test schema + schema-only `search_path`
- Request-bounded application code refactored to `async with db.begin(): ...`:
  - Routes: `app/routes/news.py`, `app/routes/admin/news_sources.py`
  - Services: `app/services/admin_auth_service.py`, `app/services/news_ingestion_service.py`, `app/services/image_generation.py`
  - AuthZ: `app/services/staff_authz.py` (reads wrapped to avoid “autobegin then db.begin()”)
- Regression tests added:
  - Policy guard for request-bounded `.commit()` / `.rollback()` calls
  - Image generation failure durability test (persist failure metadata then raise)
- Documentation follow-ups completed:
  - `docs/admin_panel.md`
  - `docs/admin_auth_system_plan.md`

Remaining:

- Non-request code (`app/cli/**`, `scripts/**`) still contains explicit commits/rollbacks and is deferred to the last phase.

---

## Current Situation (Root Cause)

Historically, `tests/integration/conftest.py` started a transaction and a nested savepoint per test. This enabled tests to call `commit()` without losing rollback-based isolation at test end.

However, that conflicts with “app code starts its own transaction with `async with db.begin()`” because the session is already inside a transaction/savepoint.

This is explicitly documented as technical debt in:

- `docs/admin_panel.md` (Database Transaction Pattern)
- `docs/admin_auth_system_plan.md` (InvalidRequestError when using `db.begin()` under the test fixture)

This has now been addressed by switching the integration harness to prod-like isolation (TRUNCATE + fresh sessions per request) so request-bounded app code can use `db.begin()` normally.

---

## Inventory: Explicit `commit()` / `rollback()` Call Sites

Note: line numbers below were captured during planning and may drift as files change.

### Request-bounded app code (must refactor)

This inventory is now “clean” for request-bounded code (`app/routes/**`, `app/services/**`) and enforced via a unit policy test.

### Integration tests (expected to change to match prod)

Many tests explicitly call `await db_session.commit()` today. After we make requests use fresh sessions, committing setup data becomes *more* important (setup done in `db_session` must be visible to the request session).

Examples:

- `tests/integration/test_news.py:52`, `tests/integration/test_news.py:106`, `tests/integration/test_news.py:395`
- `tests/integration/test_similarity.py:115`
- `tests/integration/auth_helpers.py:86`, `tests/integration/auth_helpers.py:135`
- plus additional commits across `tests/integration/**`

### Non-request code (last priority; defer)

CLI:

- `app/cli/compute_metrics.py:680` (commit), `app/cli/compute_metrics.py:685` (rollback), `app/cli/compute_metrics.py:688` (rollback)
- `app/cli/compute_similarity.py:375` (commit)
- `app/cli/split_advanced_snapshots.py:124`, `app/cli/split_advanced_snapshots.py:186` (commit)

Scripts:

- `scripts/seed_news_sources.py:128` (commit)
- `scripts/ingest_player_bios.py:484` (rollback), `scripts/ingest_player_bios.py:487` (commit), `scripts/ingest_player_bios.py:489` (rollback)
- `scripts/generate_player_images.py:447`, `scripts/generate_player_images.py:771` (commit + refresh), plus multiple per-loop commits/rollbacks

---

## Target App Semantics (Request-Bounded Code)

### General rules

- No “implicit autobegin then later `db.begin()`”: open transaction scopes **before** any DB I/O.
- Prefer:
  - `async with db.begin():`
    - perform the operation’s DB reads/writes
    - allow the context to commit/rollback automatically
- Avoid direct `await db.commit()` / `await db.rollback()` in request paths.

### Special cases to preserve

**News ingestion (`app/services/news_ingestion_service.py`)**

- Preserve the current behavior: do **not** hold a DB transaction/connection while doing network/AI work.
- Structure as:
  - short read transaction scope → exit
  - network/AI work (no DB transaction held)
  - short write transaction scope → exit

**Image generation failure persistence (`app/services/image_generation.py:643`)**

- Current behavior intentionally persists job failure metadata and then raises.
- With `db.begin()`, raising inside the scope will roll back; the refactor must preserve:
  - commit failure metadata first (successful scope exit)
  - raise after persistence is durable

---

## Plan Part 1: Make Integration Tests Prod-Like (TRUNCATE + Fresh Sessions)

We agreed that production app semantics lead, and tests adapt.

### 1) Replace rollback/savepoint isolation with TRUNCATE isolation

Update `tests/integration/conftest.py`:

- Remove per-test outer transaction and nested savepoint machinery.
- At the start of each test, run:
  - `TRUNCATE <all tables in test_schema> RESTART IDENTITY CASCADE`

Implementation detail: gather table names dynamically (e.g., `pg_tables`/`information_schema`) for the generated `test_schema`.

### 2) Ensure `search_path` is stable for all sessions

- Avoid `SET LOCAL search_path ...` (LOCAL is transaction-scoped).
- Prefer setting `search_path` in a way that applies to:
  - the test engine/connection defaults, or
  - each new session/connection (session-level `SET search_path TO ...`).

Implementation notes (completed):

- Do not rely on driver/engine-level `server_settings`/`connect_args` for `search_path` (it was ignored in this environment).
- Instead, set `search_path` via SQL:
  - During schema/table creation (`engine.begin()` + `SET search_path TO "<schema>"`).
  - At the start of every session (`SET search_path TO "<schema>"`), then `commit()` to end the implicit transaction so app code can open `db.begin()` scopes.
- Use a schema-only search path (no `public` fallback) to prevent accidental reads/writes against `public` if it already contains tables/rows from previous runs.

### 3) Make requests use fresh DB sessions (prod-like)

Update the `app_client` fixture override of `get_session` so that it yields a **new session per request**, rather than yielding the shared `db_session`.

### 4) Expected test changes after switching to fresh request sessions

- Any setup done via `db_session.add(...)` + `flush()` must be followed by `commit()` if the test then calls an endpoint and expects the request to see the data.
- Tests that keep ORM instances around across multiple sessions may need to re-query or refresh.

---

## Plan Part 2: Refactor Request-Bounded App Code (Routes/Services)

Order is chosen to minimize risk and keep changes reviewable.

### Phase 1 — Services first (high leverage)

1) `app/services/admin_auth_service.py`
   - Convert each write operation to `async with db.begin(): ...`.
   - Keep `last_seen_at` write throttling behavior intact.

2) `app/services/news_ingestion_service.py`
   - Replace “commit after SELECT to end implicit txn” with explicit short read scopes.
   - Replace explicit commit/rollback retry logic with scope-per-attempt transactions.

3) `app/services/image_generation.py`
   - Ensure “persist failure state then raise” is preserved.

### Phase 2 — Routes

- `app/routes/news.py` (create source; ingestion retry path)
- `app/routes/admin/news_sources.py` (create/update/delete)

Implementation notes (completed):

- `app/services/staff_authz.py` was also updated to wrap reads in `db.begin()` to avoid “autobegin then db.begin()” conflicts in dependency execution.

---

## Regression Guards / Tests to Add

- Add a unit “policy test” that fails if new explicit `.commit(` / `.rollback(` calls appear under request-bounded code:
  - recommended scope: `app/routes/**` + `app/services/**`
  - exclude `app/cli/**` and `scripts/**` for now (last priority)
- Add/adjust an integration test covering `image_generation` failure durability (tied to `app/services/image_generation.py:643`).

Implementation notes (completed):

- Unit policy test: `tests/unit/test_transaction_policy.py`
- Integration durability test: `tests/integration/test_image_generation_transactions.py`

---

## Documentation Follow-ups

After the prod-like test fixture is in place and request code is refactored, update docs that currently justify explicit commits as test-driven necessity:

- `docs/admin_panel.md` (Database Transaction Pattern section)
- `docs/admin_auth_system_plan.md` (note about `db.begin()` failure under tests)

Status: completed.

---

## How to Re-run the Inventory (for future sessions)

- Full repo:
  - `rg -n "\\.commit\\(" -g"*.py" .`
  - `rg -n "\\.rollback\\(" -g"*.py" .`
- Request-bounded app code only:
  - `rg -n "\\.commit\\(" app/routes app/services -g"*.py"`
  - `rg -n "\\.rollback\\(" app/routes app/services -g"*.py"`
