# Draft-results runbook (loading a completed draft)

How to load a real NBA draft's results into DraftGuru so the **draft recap**
(`/draft-recap`) and the **consensus mock board** (`/consensus`) read as a
complete two-round draft. Written so a future cycle (2027+) is a checklist, not
a re-derivation.

## The one source of truth

Actual picks live in a per-year text file, committed to the repo:

```
scripts/data/draft_results_<year>.txt
```

Format — one pick per line, tab-separated, comments (`#`) ignored:

```
<overall_pick>	<player name>	<team abbr?>
```

- **Player name** resolves against existing `players_master` rows via the shared
  matcher — no stubs are minted. An unresolved or ambiguous name is *reported*,
  not guessed (see `feedback_entity_resolution_philosophy`). Fix the spelling or
  add the player, then re-run.
- **Team abbr** is optional and means the *selecting team at the slot* (trades
  not reflected). Leave it blank when unknown — the chip renders as "—".
- **Round** is derived automatically: `round = 1 if pick <= 30 else 2`.

Round 1 is captured draft night; Round 2 the next day just appends lines 31–60
to the same file. Nothing else changes.

## Ingesting

The ingest is **idempotent** (upsert by `year + overall_pick`), so re-running or
re-pasting a growing list is safe.

```bash
# Preview first — parse + resolve, then roll back (no writes):
make draft-ingest DRY=1

# Commit to the DB (dev by default, via .env DATABASE_URL):
make draft-ingest                    # current cycle (draft_results_2026.txt)
make draft-ingest DRAFT_YEAR=2027    # a future year's file
```

The draft year is **inferred from the file name** (`draft_results_2027.txt` →
2027), so the deploy loop and future files need no `--draft-year` flag. Paste a
raw block on stdin instead of a file when live-tracking:

```bash
pbpaste | scripts/with-db-env.sh conda run -n draftguru --no-capture-output \
  python scripts/ingest_draft_results.py --draft-year 2027
```

Read the output: `UNRESOLVED` / `AMBIGUOUS` players and `UNKNOWN team`
abbreviations are listed for manual fixup. "All picks resolved cleanly" means
every row got a `player_id`.

## What updates automatically once picks are in

- **`/draft-recap`** — pick-by-pick board, Round 1 / Round 2 / Surprises tabs,
  the "2nd round" depth bucket, and the predicted-vs-actual scatter all populate
  from the `draft_results` rows. No code change per round.
- **`/consensus`** — the board already runs as deep as the source big boards go
  (60+); a **"Round 2" divider** appears automatically before pick 31. It is
  year-agnostic and keyed off consensus rank, so it needs no per-year work.

## Position sync (SL Explorer draft filters)

`draft_results` feeds the recap/consensus read layer only. The **Summer League
Explorer** draft-round/pick filters read `players_master.draft_*`, which is a
*separate* table. The `sync_draft_positions()` bridge (branch
`fix/draft-position-sync`) backfills `draft_results → players_master` and is
wired into the ingest so every load auto-syncs; a standalone
`scripts/sync_draft_positions.py` catch-up runner exists too. If you ingest on a
branch that predates that bridge, run the standalone sync afterward (or re-ingest
once it has merged) so newly drafted rookies surface in the Explorer.

## Deploy (staging + prod)

Both `fly-deploy-*.yml` workflows run a **"Sync draft results"** step after
deploy that loops every `scripts/data/draft_results_*.txt` and ingests it
(idempotent, year inferred per file). So shipping picks is:

1. Append/commit the picks to `scripts/data/draft_results_<year>.txt`.
2. Merge to `main`.
3. Re-dispatch the prod deploy (`gh workflow run fly-deploy-prod.yml --ref main`
   then approve the `draft-app-prod` gate). Names resolve against prod's player
   DB; unmatched picks are reported in the step log, not fatal.

## New-year checklist (2027 and beyond)

1. Create `scripts/data/draft_results_2027.txt` (header + picks).
2. `make draft-ingest DRAFT_YEAR=2027 DRY=1`, then without `DRY` once clean.
3. Bump `CONSENSUS_DRAFT_YEAR` in `app/routes/ui.py` to the new cycle.
4. Confirm `draft_pick_slots` is seeded for the year (needed for R2 team chips
   in the mock overlay).
5. Commit + deploy as above. Recap year-switcher and the consensus Round-2
   divider pick up the new year automatically.
