# Programmatic Code Discipline

**Status:** Design spec. Strategy document — no code changes proposed inline.

**Purpose:** encode the Summer League failures as **automated enforcement**, so the next
build-in-a-hurry cannot repeat them. Companion to `north-star-architecture.md` (the principles)
and `summer-league-desk-history.md` (the failures these rules descend from).

## The governing rule

> **Every rule here traces to a specific past failure.** Rules without a failure behind them
> become noise, and noise trains people to bypass the whole system. When adding a rule, name the
> incident.

## Existing precedent

The repo already does this well — two local pre-commit hooks in plain stdlib `ast`:

- `scripts/check_request_transaction_policy.py` (~60 lines) — bans `commit()`/`rollback()` in
  request-bounded code
- `scripts/check_route_conventions.py` — enforces `response_model`, `status_code`, DI patterns

Everything below extends that established pattern. No new tooling category is required except
where noted.

---

## Why AST is not enough for the transaction problem

**The most important finding in this doc.** The actual production defect — Gemini embedding calls
running inside the writer lock — was **not lexically nested** in the lock block. The call sat four
frames down:

```
prepare_source_player_resolution
  └── _collect_candidates
        └── find_candidate_players
              └── embed_text          ← the HTTP call
```

A checker walking the body of `async with db.begin():` sees `await resolve_players(db)` and cannot
know an HTTP call lives underneath. **AST catches the naive cases and is blind to the ones that
actually bite.**

Therefore the enforcement is tiered, with each tier matched to what it can genuinely detect:

| Tier | Mechanism | Catches | Cost |
|---|---|---|---|
| **1** | AST pre-commit checkers | syntactic, local patterns | low |
| **2** | Runtime guards (dev/test) | cross-frame, semantic violations | medium |
| **3** | Import contracts + budget tests | structural drift | low–medium |

---

## Tier 1 — AST checkers

Small, focused scripts in the existing style.

### 1.1 Unscoped delete (P2 enforcement) — **highest value, lowest effort**

**Failure:** the metrics rebuild full-wipes `SummerLeaguePlayerSeason` / `MetricContext` /
`MetricModel` every run (`metrics.py:1443-1446`), destroying the time axis and the auditable fit
history.

**Rule:** flag `delete(Model)` calls with no `.where(...)` clause.

**Allowlist:** test fixtures, explicit `--replace-run` correction paths, seed/demo scripts — each
requiring an inline `# discipline: unscoped-delete <reason>` comment so exceptions are visible in
review rather than silent.

This is the single most direct mechanical guard on P2, and it would have flagged the offender the
day it was written.

### 1.2 Transaction body weight

**Failure:** the mega-transaction (`summer_league_ingest_runner.py:1308-1348`) wraps a full table
wipe, 72-variant materialization, and an environment refresh in one `db.begin()` under the writer
lock.

**Rule:** inside `async with db.begin():` blocks, flag —

- lexically nested `await` on a denylist of I/O clients (httpx, `embedding_service`,
  `nba_stats_client`);
- unbounded `for`/`while` loops containing `await`;
- more than *N* statements in the block body (crude but catches mega-transactions).

**Known limit:** lexical only — see above. This is a cheap first net, not the real guard.

### 1.3 Stat-formula constants confined to the engine

**Failure:** the TS% free-throw coefficient `0.44` appears at ~8 sites across Python and
hand-written SQL; eFG%, TOV%, and Game Score are similarly scattered.

**Rule:** designated stat coefficients (`0.44`, and the Game Score weights) may appear **only**
under `app/services/stats/`. Additionally flag SQL string literals matching stat-aggregate
patterns (`SUM(fga)`, `2 * (fga`) outside that package.

This makes doc #2's consolidation *stick*. Without it, the eight copies regrow the next time
someone needs a formula in a query.

### 1.4 File-size ratchet

**Failure:** six central Desk files total ~7,677 lines; `explorer_service.py` is ~198KB. The
failure record names complexity beyond a reviewable unit as a root cause.

**Rule — diff-scoped, not absolute.** Measure the *change*, mirroring the repo's existing
`make coverage.diff` habit (≥80% patch coverage on changed `app/` lines). Diff-scoping means no
baseline file to maintain or merge-conflict: git already knows the before-size.

For each file touched by the change:

| Situation | Verdict |
|---|---|
| Ends **under** the threshold (~500 lines) | pass |
| Already **over** and the change makes it **larger** | **fail** |
| Already **over** and the change makes it **smaller or equal** | pass |
| **New** file over the threshold | fail |

This is the ratchet expressed as a delta, so it applies itself gradually and automatically: every
touch of a god file must leave it no worse, and most touches will naturally leave it better.

**Optional companion — a per-file delta cap** (no single change adds more than ~300 lines to one
file), independent of absolute size. This targets a named failure directly: the initial Desk merge
was 104 files and 35,018 insertions, which the failure record identifies as *"complexity grew
beyond a reviewable unit."* A delta cap forces that shape into reviewable pieces.

### Critical: do not block the decomposition this rule exists to encourage

Splitting `explorer_service.py` (198KB) into modules creates new files full of *moved* lines. A
naive delta check fails exactly the refactor we want. Design for it:

- **Evaluate net change across the whole changeset**, not per-file in isolation — a pure split is
  net-neutral and must pass.
- **Enable git rename/copy detection** so moves are not counted as additions.
- Provide an escape hatch (`# discipline: file-size <reason>`) requiring a justification visible
  in review.

Get this wrong and the rule becomes an argument *against* cleaning up god files.

### Where it runs

Both, with different strictness: **pre-commit warns** (fast feedback while context is fresh —
which is the whole point of a guardrail), **CI enforces** against the merge base, alongside
`coverage.diff` where the diff-scoped tooling already lives.

### 1.5 Watermark honesty *(lower confidence — evaluate before adopting)*

**Failure:** freshness fields advancing on `now()` rather than reflecting source currency.

**Rule:** fields named `*_as_of` / `*_refreshed_at` may not be directly assigned
`datetime.utcnow()` / `now()`.

Brittle and easy to sidestep. **The `Watermark` value object (doc #4 §5b) is the better
enforcement** — this rule is a stopgap if that lands late.

### 1.6 Free wins already available in Ruff

Currently `[tool.ruff.lint]` only extends `D` (pydocstyle), so there is substantial headroom:

- `C901` (mccabe complexity), `PLR0915` (too-many-statements), `PLR0913` (too-many-arguments) —
  all target the over-populated functions inside over-populated services.

**Adopt with a ratchet**, not all at once: enable, baseline existing violations via
`per-file-ignores`, and require new code to pass. Also note `tests` and `alembic` are currently
excluded from Ruff entirely — worth revisiting separately.

---

## Tier 2 — Runtime guards (the ones that catch the real bugs)

### 2.1 No network I/O while a transaction is open — **the direct fix**

**Failure:** external calls inside transactions and inside the writer lock; transactions timing
out because they stayed open across network round-trips.

**Mechanism:**

1. A `ContextVar` tracks transaction depth, set via SQLAlchemy `after_begin` /
   `after_transaction_end` events.
2. The HTTP layer (httpx event hook) and the embedding / NBA-stats clients check it before issuing
   a request.
3. In **dev and test**, a violation raises with the full call stack. In production it logs a
   warning with the stack, so latent paths surface without breaking users.

This catches violations **at any call depth**, which is exactly what AST cannot do — and it is
itself unit-testable (assert the guard fires for a deliberately nested call).

### 2.2 No network I/O while holding the writer lock

Same shape, with the ContextVar set in `write_lock.py` acquire/release. Distinct from 2.1 because
the lock can be held with no transaction open, and vice versa. This is the precise guard for the
July 19 starvation cause.

### 2.3 Transaction duration and statement budgets in tests

**Failure:** the venue phase held a lock ~88 minutes; the Desk needed ~38 seconds.

**Mechanism:** instrument integration tests to record max transaction lifetime and statements per
transaction; fail on regression past a baseline. Extends the existing budget habit in
`tests/integration/perf/budgets.py` from *route query counts* to *transaction weight*.

---

## Tier 3 — Structural contracts

### 3.1 Import contracts — **would have caught the "generic framework that isn't"**

**Failure:** every module in `app/services/event_desk/` imports `app.schemas.summer_league`, while
the package presents itself as a generic framework. Nothing made that contradiction visible.

**Mechanism:** `import-linter` (not currently a dependency; contracts live in `pyproject.toml` or
`.importlinter`, and the check belongs in `.github/workflows/run-tests-on-pr.yml`). Ruff's
`flake8-tidy-imports` banned-api can express simple per-module bans but not layering or
independence.

**Two properties make this adoptable immediately rather than "after cleanup":**

1. **`ignore_imports` is a built-in ratchet.** A contract can enumerate its current violations
   explicitly. New violations fail; listed ones are documented debt; the list can only shrink.
   This is what makes the `event_desk` contract usable *today* despite failing in every module —
   each line removed is measurable progress, and no new coupling can be added meanwhile.
2. **A contract written before its package exists starts green and never regresses.**
   `app/domain/` and `app/services/stats/` do not exist yet. Writing their contracts now means
   they are enforced from line one, with zero debt to pay down. **This is the cheapest
   architectural guarantee available** — take it before writing the code, not after.

### The contract set

| # | Contract | Type | Baseline today |
|---|---|---|---|
| 1 | `app.routes` → `app.services` → `app.schemas` → `app.domain` | layers | **needs measuring** — likely near-passing; run before enabling |
| 2 | `app.domain` ↛ `app.schemas`, `app.services`, `app.routes` | forbidden | **green** (package not yet created) — keeps the vocabulary ORM-free, doc #4 §5b rule 3 |
| 3 | `app.services.stats` ↛ `app.services.summer_league*`, `app.schemas.summer_league*` | forbidden | **green** (package not yet created) — the engine stays source-agnostic |
| 4 | `app.services.event_desk` ↛ `app.schemas.summer_league*`, `app.services.summer_league*` | forbidden | **fails broadly** — adopt with `ignore_imports` baseline; shrink as doc #3 decouples |
| 5 | `app.services.sources.*` mutually independent | independence | **vacuous at one spoke** — pre-install so spoke #2 inherits it |

Contract 4 is the one that matters most: it converts *"this framework is secretly coupled to one
spoke"* from an archaeological discovery into a failing build. Contract 5 is vacuous today and
that is precisely why to add it now — spoke #2 inherits the constraint instead of discovering it.

### Honest scope — what import contracts do and don't prevent

They prevent **coupling and layering drift**. They do **not** directly prevent *duplication*: the
stat formulas were duplicated by hand-writing SQL and re-deriving arithmetic, which requires no
import at all. That is what the constant-confinement rule (§1.3) is for.

There is a partial duplication mechanism worth using, though: **put shared primitives behind a
package boundary only the engine may cross.** A would-be duplicator then cannot reach the helper
and must re-derive from scratch — which is conspicuous in review, where copying an import is not.

### 3.2 Golden-number parity tests

**Failure:** the same statistic computed differently offline and at request time.

**Mechanism:** a fixture asserting engine value == stored column == Explorer cell == leaderboard
value. Doc #2 makes this the gate before deleting any duplicate formula, and it stays in CI
permanently as the guard against re-divergence.

### 3.3 Extend query budgets to scheduled jobs

Query-count budgets exist for routes (`tests/integration/perf/budgets.py`) and the Desk tick has
`test_desk_tick_query_growth.py`. Extend the habit to the remaining cron paths, where the
starvation actually occurred.

---

## What should NOT be a lint rule

Honesty about the ceiling — over-reaching produces bypass culture:

- **Freshness semantics** — better encoded as the `Watermark` type than pattern-matched.
- **"Is this the right abstraction?"** — judgment; that is what the doc #5 review questions are for.
- **God-file *cohesion*** — line count is a proxy, not the truth. A 400-line file doing three
  unrelated things is worse than a 700-line cohesive one. The ratchet buys pressure, not wisdom.
- **Architectural intent** — no checker will notice that a framework has one instance.

**Lint is a floor, not a ceiling.** It stops known failure modes from silently recurring under
deadline pressure; it does not produce good design.

---

## Adoption order

Sequenced by value-per-effort, and so nothing lands as a wall of violations.

1. **3.1 contracts 2, 3, 5** — write them *before* the packages exist. Near-zero effort, zero
   debt, permanent guarantee. Do this first precisely because it costs nothing.
2. **1.1 unscoped delete** — highest value per line, ~60 lines, few existing violations.
3. **2.1 / 2.2 runtime network-in-transaction guards** — the direct fix for the failure that is
   still causing pain. Warning-only in production, hard failure in dev/test.
4. **1.4 file-size ratchet** — cheap, immediately stops the god files growing.
5. **3.1 contract 4** — the `event_desk` decoupling ratchet, with its `ignore_imports` baseline.
6. **1.3 stat constants** — land *with* doc #2's consolidation so it protects the cleanup.
7. **1.6 Ruff complexity rules** — baseline via `per-file-ignores`, enforce on new code.
8. **1.2 transaction body weight**, **2.3 duration budgets**, **3.2 parity tests** — as the
   corresponding refactors land.

**Stop after 4 if bandwidth is short.** Those four cover most of the drift, and two rules that are
respected beat eight that get `# noqa`'d.

**Every rule ships with:** the failure it descends from (in its docstring), an escape hatch
requiring a justifying comment, and a baseline so it never blocks unrelated work.
