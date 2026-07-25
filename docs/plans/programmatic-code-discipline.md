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

**Rule, with a ratchet — this part matters:**

- **New** files capped at a hard limit (~500 lines).
- **Existing** oversized files are recorded in a baseline file and may only **shrink**. Growth
  fails the hook.

A flat limit on a legacy codebase blocks all work and gets disabled within a week. A ratchet
converts a wall into a gradient, and every touch of a god file becomes a small improvement.

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

**Mechanism:** `import-linter` contracts (or Ruff's `flake8-tidy-imports` banned-api per-module):

| Contract | Enforces |
|---|---|
| `app/domain/` ↛ `app/schemas/` | domain vocabulary stays ORM-free (doc #4 §5b rule 3) |
| `app/services/stats/` ↛ any spoke | the engine stays source-agnostic |
| `app/services/event_desk/` ↛ `app.schemas.summer_league` | a "generic" framework must actually be generic |
| `sources/<spoke>/` ↛ `sources/<other spoke>/` | spokes stay independent |

The third contract is the important one: it converts "this framework is secretly coupled" from an
archaeological discovery into a failing build. **Add it as a warning first** — it currently fails
everywhere — then fix toward it as doc #3's work proceeds.

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

1. **1.1 unscoped delete** — highest value, ~60 lines, few existing violations.
2. **2.1 / 2.2 runtime network-in-transaction guards** — the direct fix for the failure that is
   still causing pain. Warning-only in production, hard failure in dev/test.
3. **1.4 file-size ratchet** — cheap, immediately stops the god files growing.
4. **1.3 stat constants** — land *with* doc #2's consolidation so it protects the cleanup.
5. **3.1 import contracts** — warning-first, tighten as doc #3 decouples the Desk.
6. **1.6 Ruff complexity rules** — baseline via `per-file-ignores`, enforce on new code.
7. **1.2 transaction body weight**, **2.3 duration budgets**, **3.2 parity tests** — as the
   corresponding refactors land.

**Every rule ships with:** the failure it descends from (in its docstring), an escape hatch
requiring a justifying comment, and a baseline so it never blocks unrelated work.
