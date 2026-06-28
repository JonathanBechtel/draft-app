# Plan: Fully Roster the 2026 Summer League (all venues, every player)

**Status:** Planning — drafted 2026-06-28
**Branch (to create):** `feature/summer-league-2026-roster`
**Owner:** Jonathan

## Goal

Have the app **fully rostered and enriched** for the 2026 NBA Summer League across
**all three venues**, before and during the event:

- Every team's roster present **before tip-off** (announced rosters), then kept in
  sync as games are played.
- **Every player** carrying a stylized image (from a reference headshot where one
  exists), a bio (height/weight/birth/draft/school), and college production stats
  where a source exists.

### Scope decisions (locked 2026-06-28)

| Decision | Choice |
| --- | --- |
| Approach | **Pre-event roster scraper + ingest-as-played** |
| Venues | **All three** — California Classic (LeagueID 13), Salt Lake City (16), Las Vegas (15) |
| Enrichment depth | **Every player** (incl. two-way / undrafted; internationals fall to manual review where no source exists) |
| Delivery | **Incremental, closest-competition-first** — ship one venue end-to-end before the next, sequenced by tip date (satellites → Vegas) |
| Roster cadence | **Repeated refresh** — announced rosters fill in fluidly (late adds, cuts, two-way moves); the roster scraper/loader is idempotent and re-run on a cadence, not once |

### 2026 calendar (drives ops timeline)

- **California Classic (13)** + **Salt Lake City (16):** ~July 4–8 (satellite events, subset of teams)
- **Las Vegas (15):** **July 9–19**, all 30 teams, 76 games (semis Jul 18, final Jul 19)

> Today is **2026-06-28** — ~6–11 days of runway. Enrichment of the known 2026
> rookie class can start **now**; live stats ingestion begins when games are played.

## Current state (what already works — reuse, don't rebuild)

A proven 3-stage backbone exists (runbook: `docs/summer_league_backbone_runbook.md`,
merged PR #343):

1. **Fetch** — `scripts/fetch_summer_league_raw.py --year 2026 --league-id 15`
   pulls NBA Stats JSON (curl_cffi chrome impersonation) into `data/raw/...`.
2. **Normalize** — `scripts/normalize_summer_league.py` → `SummerLeagueCompetition /
   TeamEntry / Game / SourcePlayer / PlayerGameLog` (all in
   `app/schemas/summer_league.py`).
3. **Resolve** — `scripts/resolve_summer_league_players.py --create-stubs` runs the
   cascade (external-id → exact → alias → vector-candidate → stub) and backfills
   `player_id` onto every game log.
   (`app/services/summer_league/player_resolution.py`)

**Enrichment rails already exist:**

- **Images:** `PlayerMaster.reference_image_url` / `reference_image_s3_key` feed a
  Gemini vision→stylize flow (`app/services/image_generation.py`). Bulk via
  `scripts/generate_player_images.py --batch submit` (50/job). Reference images are
  **already first-class** — the only gap is *sourcing* a headshot per player.
- **Bio:** `scripts/bbref_bio_scraper.py` + `scripts/ingest_player_bios.py` →
  `players_master` + `player_status`.
- **College stats:** `scripts/scrape_college_stats.py` →
  `player_college_stats` (per-season NCAA production; keyed off a BBRef external id).

## Key findings that shape the plan

1. **No pre-event roster source exists.** The whole pipeline derives rosters from
   box scores; there is **no roster table** and the app cannot show a 2026 team
   before that team plays a game. → **New work required** (Workstream A).
2. **Every SL source player carries `nba_stats_person_id`** (stable anchor,
   `app/schemas/summer_league.py:347`). This unlocks two big levers:
   - **Free reference images:** NBA headshot CDN
     `https://cdn.nba.com/headshots/nba/latest/1040x760/{person_id}.png`. No image
     scraping needed for any player with an NBA person id.
   - **Deterministic resolution:** if `player_external_ids(system='nba_stats')` is
     populated for the rookie class, resolution is exact (not fuzzy). Current
     auto-resolution is only ~63%; seeding nba_stats external ids lifts that.
3. **2026 rookies likely already have `PlayerMaster` rows** from the draft-recap
   ingest (June 23). They need their `nba_stats_person_id` external id + reference
   image backfilled, not re-creation.

---

## Strategic fit: the journey-graph backbone

This work is **not a standalone SL feature** — it is the live proving ground for the
Global Player-Journey Graph (`docs/plans/global-player-journey-graph.md`). That design
explicitly designates Summer League as the first spoke to retrofit onto the backbone:

- §12 critical path, step 5: *"Retrofit Summer League onto participation; prove the
  complete path (queries, aggregation, resolution) **before** building the international
  spoke."*
- §15: *"Summer League — the first spoke retrofitted onto participation and the proof of
  the complete path."*

**Why 2026 SL seeding is strategically high-leverage:**

1. **A vertical slice of the journey for the marquee cohort.** This populates *multiple
   lifecycle stages of the same 2026 rookies at once*: college production (C4 = past
   affiliation), draft (already ingested = a transaction), SL participation (A/B =
   present), NBA next. It validates identity stitching against *known-correct answers*
   for the cohort with peak user interest — the cheapest proof of the backbone (§12).
2. **The defensible asset compounds.** Every player resolved here (external-id seed C5,
   alias, review-queue decision A3) is a permanent identity assertion reused by every
   future spoke. The resolution-review queue we reuse is exactly what §6 says to
   generalize into the global identity-action audit.
3. **The fluid roster refresh IS the backbone's hardest primitive in miniature.**
   Announced → added/cut → actually-played is the recorded-time-vs-effective-time
   correction problem of §5b ("affiliations are assertions, not append-only intervals").

### Decision: born-canonical, no later rewrite (Workstream 0 first)

Confirmed direction: bake the backbone framing into the July tickets and **avoid a future
restructuring migration of 2026 SL data entirely.** "No migration" here means: additive
forward migrations (new tables / nullable columns) are fine; what we refuse to incur later
is a *rewrite/backfill of 2026 SL rows*. Only two structural commitments are
irreversible-if-wrong and must be born correct now (Workstream 0, Tier 0); everything else
is either already preserved (backfillable) or strictly additive. See **Workstream 0** for
the full prerequisite set; the SL workstreams below write *through* those primitives.

---

## Workstream 0 — Born-canonical foundation (PREREQUISITE, do before A writes data)

Principle: the only commitments that can't be undone later are **(1) the bridge
grain/FK shape** and **(2) append-only assertion history with bitemporal stamps**. Build
those now; defer or backfill the rest.

> **Concrete SQLModel definitions + migration plan:**
> `docs/plans/summer-league-2026-workstream0-schema.md` (ticket-ready). Key modeling
> decision: split the **append-only assertion stream** (`player_affiliation`, universal)
> from the **stable stat bridge** (`summer_league_participation`, SL spoke) — game logs FK
> the stable bridge; roster churn supersedes assertions.

### Tier 0 — irreducible (blocks Workstream A)

- **0a. `participation` bridge table.** Minimal grain
  `(canonical_player_id, team_entry_id, stint_no, first_date, last_date, status)`. **Both**
  2026 roster entries and 2026 player game logs carry a `participation_id` FK. Pre-2026
  SL logs stay null (backfill later = additive). Resolves the §4/§7b inline-grain mismatch
  for all new data — without this, building participation later forces a rewrite.
- **0b. Roster membership as an append-only affiliation assertion.** Immutable rows with
  `supersedes_id` / `retracted_at`, `recorded_at` (when learned), `effective_*` (when
  true), `status` (announced/active/cut). Never overwrite — overwritten history is
  unrecoverable, so this must be correct from the first pull. **Replaces** the earlier
  mutable `SummerLeagueRosterEntry` design; the roster diff (A2) writes new assertions, not
  in-place edits.

### Tier 1 — build now if time allows; else safe to add later additively

Backfillable without a rewrite because SL already retains raw snapshots
(`SummerLeagueRawRun` / `SummerLeagueRawFile` + `data/raw/...`):

- **0c. `assertion_evidence` provenance link (§10).** Points each affiliation/participation
  at its supporting source record (reuse the SL raw run/file as source_document/record).
  Lets announced-roster vs. box-score conflicts coexist as two evidences on one fact.
- **0d. Thin `player_identity_action` audit (§6).** Generalizes
  `summer_league_player_resolution_reviews`. SL seeding mostly *creates* resolutions
  (already logged), so recommended-not-blocking.

### Deferred — additive by construction, NOT prerequisites

Org→team/program split (team_entry gains a parent FK later), generic competition/edition/
game model, lifecycle/timeline/connection projections, measurement generalization. None
touch 2026 SL rows when they land.

> **Timeline tradeoff:** Tier 0 is a real schema build against a ~6-day runway to the
> July 4 satellite. If the date gets tight, ship **Tier 0 only** now and defer Tier 1 —
> that still guarantees no rewrite of 2026 SL data, since provenance/audit backfill from
> retained raw snapshots.

---

## Workstream A — Pre-event roster ingestion (writes through Workstream 0)

Goal: load announced rosters for all three venues **now**, resolve players, and
expose them, before any games are played. All roster/participation writes go through the
Workstream 0 primitives (0a participation, 0b affiliation assertions).

### A0 — Roster source-discovery spike ✅ RESOLVED (2026-06-28)

**Chosen source: NBA.com SL roster pages, which embed `commonteamroster` JSON in the
page's `__NEXT_DATA__` blob — and it carries the NBA `PERSON_ID`.** This is the best
case: deterministic resolution *and* free bio fields, no TLS impersonation needed.

- **Per-player fields:** `PLAYER_ID` (= PERSON_ID), `NUM` (jersey), `POSITION`,
  `HEIGHT`, `WEIGHT`, `BIRTH_DATE`, `AGE`, `EXP`, `SCHOOL`, `PLAYER_SLUG`,
  `HOW_ACQUIRED`, `LeagueID`, `TeamID`, `SEASON`.
- **URLs:** venue landing pages enumerate every team + TeamID —
  `nba.com/2026-summer-league-vegas-roster` (30 teams), `…-california-roster` (7),
  `…-slc-roster`. Per-team: `nba.com/summer-league/2026/{las-vegas|california|
  salt-lake-city}/team/{TeamID}/{slug}`. Parse `props.pageProps.roster` from
  `<script id="__NEXT_DATA__">`. Plain `curl` / normal UA → HTTP 200.
- **Timing:** 2026 pages are **live now but rosters are empty** (`roster: []`);
  they fill per team as announced (~Jul 1+). → **poll daily from ~Jul 1.**
- **Pure-JSON alternative:** `stats.nba.com/stats/commonteamroster?TeamID=<id>
  &Season=2026&LeagueID={13|15|16}` via the existing `NBAStatsClient`
  (`app/services/summer_league/nba_stats_client.py` + `endpoints.py`) — identical
  payload, but adds the curl_cffi path. **Pick the `__NEXT_DATA__` HTML scrape** for
  zero-impersonation robustness; keep the stats endpoint as the in-house fallback.
- **Fallback (name-only):** RealGM — Cloudflare-gated, RealGM ids not PERSON_ids.

**Strategic bonus:** because the feed carries `HEIGHT`/`WEIGHT`/`BIRTH_DATE`/`SCHOOL`,
roster ingestion (A2) *also* seeds the bio fields Workstream C3 needs and the `school`
that C4 college-stats keys off — so resolution, headshots (C1), and bio largely fall out
of one fetch. Spike detail in the task result / this section.

### A1 — Roster storage = affiliation assertions (per Workstream 0b)

Rosters are stored as the append-only affiliation assertions from **0b** (not a separate
mutable roster table). Each assertion captures
`(participation_id → team_entry, source_player_id, nba_stats_person_id, raw_player_name,
jersey_number, position, status, recorded_at, effective_*, supersedes_id, retracted_at,
source)`. A refresh detects late adds (new assertion), still-present players (no-op or
re-affirm), and drops (supersede with `status='cut'`, never delete). Migration is the
0a/0b new-table pattern in `app/schemas/`.

### A2 — Roster scraper + loader (idempotent, refreshable)

- `scripts/fetch_summer_league_rosters.py` — fetch the three NBA.com venue landing
  pages, regex out team links + TeamIDs, fetch each team page, parse
  `__NEXT_DATA__.props.pageProps.roster`, and write one raw JSON snapshot per run.
  Safe to run repeatedly. `PLAYER_ID` keys directly into `SummerLeagueSourcePlayer.
  nba_stats_person_id` → external-id resolution + headshots are deterministic.
- Loader **upserts** (never wipes): creates `SummerLeagueCompetition` rows for
  `2026/13`, `2026/16`, `2026/15`, `SummerLeagueTeamEntry` per team, and upserts
  `SummerLeagueRosterEntry` per player on `(competition_id, team_entry_id,
  raw_player_name)`. New names → insert; seen names → bump `last_seen_at`; names that
  vanished from the source → `status='cut'`.
- Reuse `SummerLeagueSourcePlayer` (keyed on `nba_stats_person_id`) when the source
  provides ids; otherwise create source players on name only.
- Emit a per-run **roster-diff report** (added / unchanged / cut counts per team) so
  churn is visible and enrichment (Workstream C) can target only the *new* names.

### A3 — Resolve roster players to canonical

Reuse the existing resolution cascade against roster source players (not just box
scores). `--create-stubs` for unmatched. Feed the vector-candidate backlog into the
existing `SummerLeaguePlayerResolutionReview` admin queue.

### A4 — Public roster/preview surface (product call)

Show team rosters before games tip off (an empty-stats "preview" state on the
existing SL pages, or a dedicated `/summer-league/2026/rosters` view). Scope TBD —
see Open Questions.

---

## Workstream B — As-played stats ingestion (operationalize the existing pipeline)

### B1 — Daily ingest job for 2026, all venues

Wrap fetch→normalize→resolve into one operator command per venue and run it daily
across the event window (CA Classic/SLC ~Jul 4–8, Vegas Jul 9–19). Use
`scripts/backfill_summer_league_backbone.py` (already orchestrates the 3 stages) with
`--year 2026 --league-id {13,16,15} --create-stubs`. Idempotent re-runs pick up new
games each day. **2026 player game logs must populate `participation_id` (0a)** —
extend the normalizer to upsert a participation per `(player, team_entry)` and reference
it, instead of only inlining `player_id`.

### B2 — Reconcile pre-event rosters with box-score players

Box-score source players and roster source players share `nba_stats_person_id` →
dedupe is automatic via the unique constraint. Add a check that flags roster entries
that never appear in a box score (DNP/cut) and box-score players missing from the
announced roster (late adds).

### B3 — Metrics + QA

Run `scripts/rebuild_sl_metrics.py` and `scripts/qa_summer_league_backbone.py
--slice 2026/15 --slice 2026/13 --slice 2026/16` after each major ingest. Target
**0 blocking findings** (accepted-warning classes per the runbook).

---

## Workstream C — Player enrichment, every player

### C1 — Reference-image sourcing at scale (the high-leverage step)

For every SL source player with `nba_stats_person_id`, set
`PlayerMaster.reference_image_url` to the NBA headshot CDN URL. New script
`scripts/backfill_nba_headshots.py` (resolve → write `reference_image_url`,
validating the URL returns an image; players without NBA ids or with 404s go to a
fallback list for manual/college-headshot sourcing).

### C2 — Bulk stylized image generation

`scripts/generate_player_images.py --batch submit` over the 2026 SL cohort
(`--missing-only`), then `--batch retrieve`. Reference image flows through the
existing vision→stylize path automatically.

### C3 — Bio ingestion

Run `bbref_bio_scraper.py` + `ingest_player_bios.py` over the SL player set that has
a BBRef external id (rookies + anyone with NBA experience). Internationals without a
BBRef page → manual-review list.

### C4 — College stats

`scripts/scrape_college_stats.py --only-missing` for resolved players with `school`
+ BBRef id. Internationals / non-NCAA → no source (expected; flagged, not failed).

### C5 — Seed `nba_stats` external ids for the rookie class

Backfill `player_external_ids(system='nba_stats')` for 2026 rookies (already in
`players_master` from draft-recap) so A3/B1 resolution is deterministic and C1
headshots attach cleanly. Likely a small mapping script keyed off the roster source.

---

## Workstream D — Ops, scheduling, prod

- **D1 Scheduling:** decide cron vs. manual daily runs for Jul 4–19 (a `/schedule`
  cloud routine, or a daily reminder to run B1). Dev first.
- **D2 Prod replication:** promote dev → prod once a venue's data is QA-clean
  (mind the Neon pooler `search_path` gotcha noted in memory).
- **D3 Monitoring:** resolution-rate dashboard per competition (target: push past the
  current ~63% via C5 + manual review).

---

## Suggested execution order (incremental, closest-competition-first)

Build the rails once, then run them venue-by-venue in tip-date order. The **first
satellite to tip is the pilot** — take it fully end-to-end (roster → enrich →
live stats → QA) before turning on the next venue; each subsequent venue is mostly a
re-run of the same commands with a new `--league-id`.

1. **Now (Jun 28–Jul 3) — foundation + rails + pilot prep:** **Workstream 0 Tier 0
   (0a participation + 0b affiliation assertions) FIRST** (Tier 1 0c/0d if time) → A0
   spike → A1/A2 idempotent loader (writes assertions) → A3 resolve → C5 external-id seed
   → C1 headshots → C2 images → C3 bio → C4 college. Apply to the **earliest-tipping venue
   first** (CA Classic / SLC). Prove the full chain — through the canonical primitives —
   on one comp.
2. **Refresh cadence (continuous):** re-run A2 loader + targeted C1–C4 over **only
   the new names** from each roster diff, daily (or more often near tip-off), as
   rosters fill in. This is the standing "incremental roster refresh."
3. **Jul 4–8:** B1 live stats for CA Classic (13) + SLC (16); A4 preview → live
   transition; nightly QA (B3).
4. **Jul 9–19:** turn on Vegas (15) — same pipeline, new league id; daily ingest +
   refresh + QA + enrichment sweeps.
5. **Post-event:** prod replication (D2), resolution-rate cleanup, manual review of
   internationals.

## Risks & open questions

- **OQ1 (A0):** Does a clean announced-roster source exist that carries NBA
  `person_id`? If only names are available, A3 resolution + C1 headshots get harder
  for non-NBA players. **This spike gates Workstream A.**
- **OQ2 (A4):** How visible should pre-event rosters be — full public preview pages,
  or admin-only until games start? (Product/UX call.)
- **OQ3:** `2026` raw availability — confirm `fetch_summer_league_raw.py` pulls
  `2026/{13,16,15}` once games are played (probe floor was historical; modern years
  proven through 2024/2025).
- **OQ4:** Volume — "every player" across 3 venues is ~400+ players, many stubs and
  internationals. Manual-review backlog will be real; D3 dashboard keeps it visible.
- **OQ5:** Cost — Gemini batch image generation over 400+ players. Confirm budget;
  `--missing-only` + batch (50/job, ~50% cheaper) contains it.

## Definition of done

- Competitions `2026/13`, `2026/16`, `2026/15` exist with all teams as
  `SummerLeagueTeamEntry` and announced rosters as `SummerLeagueRosterEntry`.
- Live game logs ingested through the event; QA harness reports 0 blocking findings
  per venue.
- Every resolvable player has a stylized image (reference-sourced where possible),
  bio, and college stats where a source exists; the unresolved/no-source remainder
  is enumerated in a manual-review list, not silently dropped.
- 2026 SL data is born on the canonical primitives (Workstream 0 Tier 0): roster +
  game logs reference `participation_id`; roster history is append-only affiliation
  assertions with bitemporal stamps. No future restructuring/backfill rewrite of 2026
  rows is required (remaining backbone pieces land additively).
- Dev validated; prod replicated and QA-clean.
- Repo checks green (precommit, mypy, unit/integration, coverage.diff) for all new
  code (new table, scrapers, headshot backfill).
</content>
</invoke>
