# Product Pitch: 2026 Summer League Roster Foundation

**Feature:** `summer-league-2026-roster-foundation` · **Status:** Pitch (Step 1 of chain)
· **Date:** 2026-06-28 · **Deadline:** before the July 4 satellite tip-off

> Scope: the deadline-critical first slice of "Fully Roster the 2026 Summer League" —
> **Workstream 0 (born-canonical foundation) + Workstream A (pre-event roster ingestion)**
> for the pilot (earliest-tipping) venue. Full plan:
> `docs/plans/summer-league-2026-full-roster.md`.

## Problem

The 2026 Summer League tips in ~6 days, and DraftGuru cannot show a single team's roster
until that team plays a game — the entire SL pipeline derives players *from box scores*,
with no roster table and no pre-event source. So at the moment of peak interest (the
just-drafted rookie class taking the floor), the app is empty exactly when fans arrive to
follow AJ Dybantsa, Cameron Boozer, et al. Worse, if we rush a tactical roster store to
fill the gap, we bake in a data shape that contradicts the longitudinal player-journey
backbone (`global-player-journey-graph.md`) and force a painful rewrite later.

## Audience

- **Primary (end users):** draft-following fans who want to see *who is on each team*
  before games start, and track the rookie class across the lifecycle.
- **Secondary (the platform itself):** every later spoke — as-played stats, image/bio/
  college enrichment, the journey graph — depends on this canonical foundation existing.

## Hypothesis

If 2026 SL rosters are loaded **before tip-off** from NBA.com (which carries the NBA
`PERSON_ID`), and stored on the **canonical participation + append-only affiliation grain**,
then (a) the app is populated and useful at peak interest, (b) player resolution and
headshots are deterministic rather than fuzzy, and (c) we prove the journey-graph backbone
on live data with **zero future restructuring migration** — turning a deadline scramble
into the first durable spoke.

## Core user/data flow

1. **Poll** the three NBA.com venue roster pages daily from ~July 1 (`__NEXT_DATA__` JSON,
   plain `curl`); rosters fill in fluidly as teams announce.
2. **Load** each rostered player as an idempotent, append-only **affiliation assertion**
   (announced → confirmed → cut, bitemporal) + a stable **participation** bridge row.
3. **Resolve** to a canonical player via the existing cascade — deterministic by
   `PERSON_ID` first, stub as last resort.
4. **Result:** every team's pilot-venue roster is present and correct, refreshes as it
   changes, and is born on the grain the whole backbone (and the eventual public roster UI)
   reads from.

## Headline features

- **NBA.com roster scraper** (`__NEXT_DATA__`/`commonteamroster`) — carries PERSON_ID,
  jersey, position, height, weight, birthdate, school; no TLS impersonation.
- **`player_affiliation`** — universal, append-only, bitemporal roster-assertion stream
  (corrections supersede, never overwrite — fluid rosters stay historically honest).
- **`summer_league_participation`** — stable `(player, team_entry, stint)` bridge; future
  game logs FK it (additive `participation_id` column).
- **Idempotent refresh loader + roster-diff report** — re-runnable; surfaces added/cut so
  downstream enrichment targets only new names.
- **Resolution reuse** — PERSON_ID external-id → exact → alias → vector-candidate → stub.

## Scope boundaries

- **In:** Workstream 0 Tier 0 (participation + affiliation assertions + the additive
  game-log column, one Alembic revision) and Workstream A (scraper, loader, resolution)
  for the **pilot venue only**.
- **Out (follow-up):** as-played stats ingest (B), image/bio/college enrichment (C),
  ops/scheduling/prod replication (D), the public roster preview UI (A4), the other two
  venues, and Workstream 0 Tier 1 (provenance evidence + identity-action audit).

## Success signal

Before the pilot venue tips off, every announced team roster is loaded and re-syncs on
daily refresh; ≥the great majority of players resolve deterministically via PERSON_ID;
and a maintainer can confirm the data sits on `participation` + append-only
`player_affiliation` with **no migration of these rows required** when the remaining
backbone lands.
