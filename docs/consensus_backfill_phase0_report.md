# Consensus Historical Backfill — Phase 0 Sourcing Audit

**Goal:** retroactively backfill real 2026-cycle big boards so the consensus
feature ships looking "full" (real sparklines, movers, freshness, source
history) — not synthetic. Scope chosen: **fully real · weekly snapshots ·
same source roster as live**.

**Method:** `scripts/audit_board_archives.py` walks each configured
`news_sources` row's Substack public archive API (`/api/v1/archive`) across the
2026 cycle (floor 2025-09-01), recording title / date / paywall per post.
`scripts/curate_backfill_candidates.py` then filters to clean NBA-draft big
boards. Artifacts: `docs/consensus_backfill_manifest.json` (raw) and
`docs/consensus_backfill_candidates.json` (curated, the ingestion input).

## Recoverable boards: 24 free 2026 NBA draft big boards, 7 sources

| Source | Free boards | Span |
|---|---|---|
| No Ceilings | 7 (V.1–V.7) | Nov 12 → May 6 |
| Assisted Development | 4 | Sep 19 → May 29 |
| Ersin Demir | 4 (2.0–5.0) | Nov 14 → May 22 |
| Dizzle Dynasty | 4 (1.0–4.0) | Oct 31 → Mar 13 |
| Draft Stack | 2 | Nov 24, Mar 31 |
| Prospects & Concepts | 2 | Mar 28, May 6 |
| Floor and Ceiling | 1 | Nov 3 |

**Monthly coverage (distinct sources publishing):** Sep 1, Oct 3, Nov 5, Dec 3,
Jan 2, Feb 2, Mar 4, Apr 1, May 4 — every month of the cycle has fresh boards,
so weekly as-of snapshots will show real movement throughout.

## Excluded / unrecoverable (and why)

- **NBA Big Board** — real boards are **paywalled**; only a free *announcement*
  post exists. Recoverable only if we revisit D1 (partial extraction). Skipped.
- **NBA Draft Room** — **non-Substack** (WordPress; `/api/v1/archive` 404s).
  Needs a separate HTML scraper. Deferred.
- **Silver Bulletin** (Nate Silver) — not a draft source; 0 free boards. Drop.
- **The Box And One** — archive returned 0 cycle posts (inactive/empty). Drop.
- **Noise filtered out of the heuristic counts:** college *recruiting* rankings
  (Draft Stack), JUCO lists (Ersin Demir), dynasty-fantasy Top-450s + rookie
  rankings (Dizzle Dynasty, No Ceilings), announcements. These are NOT NBA
  draft big boards.

## One item flagged for manual review

- **No Ceilings 2026-05-13 "Constructing a Big Board from Scratch II"** — title
  contains "big board" but reads as a methodology article, not a ranked board.
  Auto-excluded; confirm before/against including.

## Notes for ingestion (Phase 2/3)

- Dizzle Dynasty is a *dynasty-fantasy* publication; its "Big Board X.0" posts
  are kept (it's already a live consensus source) but worth a sanity check that
  they rank NBA-draft prospects, not fantasy assets.
- These 24 boards × ~30–60 entries each is the resolution workload. Front-loaded:
  the 2026 prospect universe (~100–150 names) resolves once, then aliases make
  later boards mostly auto-resolve. Ambiguous → stays UNRESOLVED (never guess).
- Existing dev-DB boards for these sources (Draft Stack, Ersin Demir, Dizzle
  Dynasty already have rows) must be de-duped against on ingest by
  `(news_source_id, published_at)` so we don't double-insert the May boards.

---

# Build status (overnight, 2026-05-30)

Autonomous work stopped exactly at the phase that needs your judgment
(entity-resolution review + approval). Everything below is done, tested, and
left uncommitted in the worktree for your review.

## Done & verified

- **Phase 1 — as-of engine.** `recompute_consensus(..., as_of=...)` filters
  eligible boards to `published_at <= as_of`, stamps `computed_at = as_of`, and
  anchors `prev_rank` on the chronologically-preceding snapshot (so replaying
  history in any order yields correct deltas). `app/services/consensus_service.py`.
  2 new integration tests + the full file pass (13). `mypy app` clean, pinned
  pre-commit clean.
- **Phase 2 — ingestion harness.** `scripts/backfill_boards.py` synthesizes a
  NewsItem per candidate and reuses the live `extract_board` pipeline; idempotent
  (slug-keyed items, date-deduped boards); overrides board date with the
  authoritative archive date. **Proven on 2 boards** in the dev DB:
  - Ersin Demir 4.0 (2026-02-21) → board #10, 36 entries, **27 resolved / 9 unresolved**
  - No Ceilings V.1 (2025-11-12) → board #11, 60 entries, **57 resolved / 3 unresolved**
  Both PENDING. Resolution is running ~75–95% — much better than the prose-board
  baseline, because the player universe + aliases + embeddings are well-populated.
- **Phase 4 — history driver.** `scripts/generate_consensus_history.py` loops
  weekly `as_of` ceilings (earliest approved board → now) calling the as-of
  engine. Testable core (`generate_history`, `_weekly_ceilings`); 1 integration
  + 4 unit tests pass. Destructive ops (`--reset`, `--purge-synthetic`) opt-in.

## Left for you (needs judgment / approval)

1. **Bulk ingest** the remaining 22 boards (eyeball `--list` first; Dizzle
   Dynasty sanity check; the one flagged No Ceilings methodology post is already
   excluded):
   ```
   ENV_FILE=/Users/jonathan/draft-app/.env scripts/with-db-env.sh \
     conda run -n draftguru python scripts/backfill_boards.py
   ```
   (~22 Gemini extractions; a few $.) Re-runnable — already-ingested boards skip.
2. **Phase 3 — resolution sweep.** Work the admin review queue (the 2 proof
   boards already have 12 unresolved entries). Ambiguous → leave UNRESOLVED;
   mint stubs for genuinely-new prospects. Resolutions auto-grow aliases, so the
   queue shrinks fast across boards.
3. **Approve** the backfilled boards.
4. **Generate history** (clean weekly series, replacing the synthetic seed):
   ```
   ENV_FILE=/Users/jonathan/draft-app/.env scripts/with-db-env.sh \
     conda run -n draftguru python scripts/generate_consensus_history.py \
     --reset --purge-synthetic
   ```
5. **Verify** — `make visual`; confirm real sparklines/movers/freshness on the
   homepage, player-detail history chart, and source pages.

## Notes

- Nothing committed (per your workflow rule). Changed: `consensus_service.py`,
  2 test files, 4 new `scripts/`, this doc + 2 manifest JSONs. Say the word and
  I'll commit in logical chunks on a `feature/...` branch.
- Dev DB now has 2 extra PENDING boards (#10, #11) + 2 synthesized news_items.
  Fully reversible; consensus output untouched until you approve + regenerate.
- The `--reset` in step 4 deletes ALL 2026 snapshots incl. the 3 real
  late-May ones (#1/#13/#16) and the synthetic 8–12, then rebuilds a clean
  weekly series from approved boards. That's the intended clean-history path,
  but it's your call — drop `--reset` to append instead.
