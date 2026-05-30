# Summer League — Stat Inventory (what we can compute)

**Date:** 2026-05-30
**Inputs:** the raw NBA Stats API fields confirmed in `docs/summer_league_api_probe_findings.md`
(snapshots in `scripts/data/sl_probe/`).
**Reference:** Basketball-Reference glossary (formulas reproduced below) — the bbref player tables
(Per Game · Totals · Per 36 · Per 100 · Advanced · Shooting · Play-by-Play) are the target catalog.

This maps **every bbref stat → can we compute it from the data we have, and at what tier.** The
headline: bbref's "Advanced" table is built from **box-score totals (player + team + opponent)**,
not tracking or even play-by-play — so the great majority is computable from **Tier 1 box data**
for all eras 2010+. Play-by-play (Tier 3, 2019+) is only required for on/off, lineups, true
possessions, and clutch — none of which bbref publishes at the per-game level anyway.

---

## 1. Raw inputs we actually have

Confirmed field lists per endpoint (from the probe snapshots):

| Source | Grain | Key fields | Tier / era |
|---|---|---|---|
| `leaguegamelog` | team-per-game | FGM/A, FG3M/A, FTM/A, OREB, DREB, REB, AST, STL, BLK, TOV, PF, PTS, PLUS_MINUS, MIN, GAME_ID, GAME_DATE, MATCHUP, WL | **Tier 1**, 2010+ |
| `boxscoretraditionalv2` → PlayerStats | player-per-game | PLAYER_ID, MIN, FGM/A, FG3M/A, FTM/A, OREB, DREB, REB, AST, STL, BLK, TO, PF, PTS, PLUS_MINUS, START_POSITION | **Tier 1**, 2010+ |
| `boxscoretraditionalv2` → TeamStats | team + starters/bench | same totals at team & starter/bench split | Tier 1 |
| `boxscoreadvancedv2` → PlayerStats | player-per-game (**API-computed**) | OFF/DEF/NET_RATING, AST_PCT, AST_TOV, AST_RATIO, OREB/DREB/REB_PCT, TM_TOV_PCT, EFG_PCT, TS_PCT, USG_PCT, PACE, POSS, PIE | Tier "1.5" (2010+, see §6) |
| `boxscorescoringv2` → sqlPlayersScoring | player-per-game (**API-computed**) | PCT_FGA_2PT/3PT, PCT_PTS_{2PT,2PT_MR,3PT,FB,FT,OFF_TOV,PAINT}, PCT_AST/UAST_{2PM,3PM,FGM} | 2019+ |
| `shotchartdetail` → Shot_Chart_Detail | shot-per-event | LOC_X/Y, SHOT_DISTANCE, SHOT_ZONE_BASIC/AREA/RANGE, SHOT_TYPE, SHOT_MADE_FLAG, ACTION_TYPE, PERIOD, time | **Tier 2**, 2015+ |
| `shotchartdetail` → LeagueAverages | zone | FGA, FGM, FG_PCT by zone | per season/venue |
| `playbyplayv2` → PlayByPlay | event | EVENTMSGTYPE/ACTIONTYPE, PERIOD, PCTIMESTRING, SCORE, SCOREMARGIN, PLAYER1/2/3_ID + team | **Tier 3**, 2019+ |

**Critical enabler:** each game yields **two team rows**, so for any player their team totals *and*
opponent totals are both available. Every bbref rate-stat denominator (Opp DRB, Opp FGA, Opp 3PA,
Opp possessions, Tm MP) is therefore derivable from box data alone. Season-level inputs come from
summing a player's `(year, venue)` games; league context (Lg Pace, Lg PTS, Lg totals) from summing
all games in a `(year, venue)`.

---

## 2. Group A — Basic box (Per Game · Totals · Per 36)

bbref tables: **Per Game, Totals, Per 36 Minutes.** Trivial from box totals.

| Stats | Formula | Status |
|---|---|---|
| G, GS, MP, FG, FGA, FG%, 3P, 3PA, 3P%, 2P, 2PA, 2P%, FT, FTA, FT%, ORB, DRB, TRB, AST, STL, BLK, TOV, PF, PTS | direct sums; 2P = FG−3P; %s from made/att | ✅ **all eras (2010+)** |
| Per Game | total / G | ✅ |
| Per 36 | 36 × (stat / MP) | ✅ |
| GS (games started) | from START_POSITION ≠ '' | ✅ (per-game box) |

Note: **MP must be parsed** from `"34:51"` (MM:SS) strings, not the integer MIN in some sets.

---

## 3. Group B — Shooting efficiency (no opponent/possession context)

| Stat | bbref formula | Inputs | Status |
|---|---|---|---|
| eFG% | (FG + 0.5·3P) / FGA | player box | ✅ all eras |
| TS% | PTS / (2·TSA), TSA = FGA + 0.44·FTA | player box | ✅ all eras |
| 3PAr | 3PA / FGA | player box | ✅ all eras |
| FTr | FTA / FGA | player box | ✅ all eras |
| AST/TO, STL+BLK, etc. | ratios | player box | ✅ |

These need only the player's own line — no league context (context is only needed to *rank*/percentile them, which we do per `(year, venue)`).

---

## 4. Group C — Advanced rate stats (need team + opponent totals)

bbref **Advanced** table. All are *estimates from box totals*; every input is in our data
(player line + team line + opponent line + Tm MP). Formulas verbatim from the glossary:

| Stat | bbref formula | Status |
|---|---|---|
| ORB% | 100 · (ORB · (Tm MP/5)) / (MP · (Tm ORB + Opp DRB)) | ✅ Tier 1 |
| DRB% | 100 · (DRB · (Tm MP/5)) / (MP · (Tm DRB + Opp ORB)) | ✅ Tier 1 |
| TRB% | 100 · (TRB · (Tm MP/5)) / (MP · (Tm TRB + Opp TRB)) | ✅ Tier 1 |
| AST% | 100 · AST / (((MP/(Tm MP/5)) · Tm FG) − FG) | ✅ Tier 1 |
| STL% | 100 · (STL · (Tm MP/5)) / (MP · Opp Poss) | ✅ Tier 1 (Opp Poss estimated, §5) |
| BLK% | 100 · (BLK · (Tm MP/5)) / (MP · (Opp FGA − Opp 3PA)) | ✅ Tier 1 |
| TOV% | 100 · TOV / (FGA + 0.44·FTA + TOV) | ✅ Tier 1 |
| USG% | 100 · ((FGA + 0.44·FTA + TOV) · (Tm MP/5)) / (MP · (Tm FGA + 0.44·Tm FTA + Tm TOV)) | ✅ Tier 1 |

All of these are **also served pre-computed** by `boxscoreadvancedv2` (AST_PCT, OREB/DREB/REB_PCT,
TM_TOV_PCT, USG_PCT, EFG_PCT, TS_PCT) — see §6 for the build-vs-ingest call.

---

## 5. Group D — Possession / pace / efficiency ratings

bbref **Per 100 Possessions** + ORtg/DRtg, all built on the *estimated possession* formula.

| Stat | bbref definition | Status |
|---|---|---|
| Possessions (team) | 0.5·((Tm FGA + 0.4·Tm FTA − 1.07·(Tm ORB/(Tm ORB+Opp DRB))·(Tm FGA−Tm FG) + Tm TOV) + Opp equivalent) | ✅ estimated from box (Tier 1) |
| Pace | 48 · ((Tm Poss + Opp Poss) / (2·(Tm MP/5))) | ✅ Tier 1 |
| Per 100 | 100 × (stat / estimated Poss) | ✅ Tier 1 |
| ORtg / DRtg (Dean Oliver) | individual pts produced / pts allowed per 100 | ✅ computable (complex), Tier 1 |
| **True** possessions / pace | counted from PBP | ⚠️ **Tier 3 only (2019+)** — more accurate than estimate |

The **API also returns `POSS` and `PACE` directly** in `boxscoreadvancedv2`. For 2019+ these reflect
real play-by-play; pre-2019 they fall back to estimates. Recommendation: store the bbref-estimated
possessions as the canonical cross-era value and keep the API `POSS` alongside for validation.

---

## 6. Group H — API-native stats (bonus, not on bbref)

The API hands us extras bbref doesn't publish; cheap to ingest, useful on detail pages:

| Stat | Source | Note |
|---|---|---|
| PIE (Player Impact Estimate) | advancedv2 | NBA's all-in-one; SL-noisy, label as such |
| OFF/DEF/NET_RATING (per game) | advancedv2 | per-game team ratings |
| AST_RATIO, AST_TOV | advancedv2 | |
| Scoring breakdown: PCT_PTS_PAINT, PCT_PTS_FB, PCT_PTS_OFF_TOV, PCT_PTS_FT, PCT_AST_FGM / PCT_UAST_FGM | scoringv2 | **2019+** — "% of made FG assisted", paint scoring, etc. Equivalent to bbref Shooting-table "% assisted" columns without needing shot data |

**Build-vs-ingest:** compute the bbref Advanced stats ourselves from totals (consistent across all
eras, transparent formulas, recomputable when we tune them — matches the spec's "season aggregates
as materialized/rebuildable, not base tables"). Ingest the API-computed values into the raw store
too, as a free correctness check and to backfill the API-only extras (PIE, scoring %s).

---

## 7. Group F — Shooting splits & shot location

bbref **Shooting** table (FG% by distance, % of FGA by distance, % assisted, dunks, corner-3s).

| Capability | Source | Status |
|---|---|---|
| FG% by zone, % of FGA by zone, makes/attempts by zone | `shotchartdetail` (SHOT_ZONE_*, SHOT_DISTANCE, SHOT_MADE_FLAG) | ✅ **Tier 2, 2015+** |
| Shot charts (LOC_X/Y) | shotchartdetail | ✅ 2015+ |
| Corner-3 / above-break split, distance buckets | shotchartdetail SHOT_ZONE_AREA/RANGE | ✅ 2015+ |
| League-average FG% by zone (for relative shading) | shotchartdetail LeagueAverages | ✅ per season/venue |
| % of made FG assisted | scoringv2 PCT_AST_FGM (2019+) **or** PBP | ✅ 2019+ via scoringv2 |
| Dunks / heaves / specific action types | shotchartdetail ACTION_TYPE | ⚠️ partial — ACTION_TYPE is granular but not a clean dunk flag; needs mapping |

Pre-2015: no shot detail → Shooting table unavailable (degrade gracefully).

---

## 8. Group G — On/off, lineups, clutch (PBP-only)

Requires `playbyplayv2` (**Tier 3, 2019+**). bbref surfaces a subset on its Play-by-Play table.

| Capability | Status |
|---|---|
| On-court / off-court splits, WOWY | ⚠️ Tier 3, 2019+ (parse lineups from PBP substitution events) |
| Lineup +/-, lineup ORtg/DRtg | ⚠️ Tier 3, 2019+ |
| Real possessions / real pace / clutch (last-5-min, ±5) | ⚠️ Tier 3, 2019+ |
| Position estimate (% at each position), bbref PB P table | ⚠️ hard — PBP has no position; would require lineup inference. Likely **skip** |

PBP lineup reconstruction is non-trivial (the feed gives substitution events, not on-court state) —
treat as a later enhancement, not launch scope.

---

## 9. Composite metrics — compute, recalibrated to Summer League

bbref **Advanced** table tail. **Decision (2026-05-30): we compute the full suite, recalibrated to
the SL `(year, venue)` pool** rather than borrowing NBA constants — noisy but honest and
self-contained. Full method (per-metric recalibration, the reusable `LeagueContext` abstraction,
BPM's NBA-derived-coefficient caveat, VORP proration fix, small-sample shrinkage) lives in
**`docs/summer_league_advanced_metrics_methodology.md`**.

| Stat | Inputs available? | Approach |
|---|---|---|
| GmSc (Game Score) | ✅ player box: PTS + 0.4·FG − 0.7·FGA − 0.4·(FTA−FT) + 0.7·ORB + 0.3·DRB + STL + 0.7·AST + 0.7·BLK − 0.4·PF − TOV | ✅ compute as-is (no league constants) |
| PER | ✅ Lg context per (year, venue) | ✅ recalibrate: SL-average = 15 within the pool |
| ORtg / DRtg | ✅ box + team + opp | ✅ compute (absolute); present vs SL-average |
| OWS / DWS / WS / WS/48 | ✅ box + Lg context | ✅ recalibrate Lg constants; keep structural 0.92/0.32 (documented) |
| BPM / OBPM / DBPM | ✅ box regression | ✅ keep NBA coefficients, re-center team-adjustment + per-100 on SL context; label "SL-calibrated, box estimate" |
| VORP | from BPM | ✅ adapt proration to SL games (drop 82-game / ×2.7 wins); replacement level a context param |

The trustworthy core is still GmSc + TS%/eFG% + rate stats; the composites ship **with sample-size
context always visible** and an optional stabilized (shrunk) variant.

---

## 10. DraftGuru-specific derived (the moat) — all computable

Built from the SL stats above + data we already own:

| Stat | Inputs | Status |
|---|---|---|
| SL performance vs pre-draft consensus (rank delta) | computed SL composite + existing consensus rank | ✅ |
| Cohort percentile (rank within draft class's SL) | SL stats + draft_year | ✅ |
| Year-N SL trajectory | multi-season SL aggregation per player | ✅ |
| Career SL totals | sum across (year, venue) per player | ✅ |

---

## 11. Availability summary by era

| Era | Box / Per-36 / eFG / TS / rate stats / USG / est. pace | Shot splits (Tier 2) | PBP: on-off, true poss, clutch (Tier 3) |
|---|---|---|---|
| **2019 → present** | ✅ | ✅ | ✅ |
| **2015–2018** | ✅ | ✅ | ❌ (probe-confirm exact PBP floor) |
| **2010–2014** | ✅ (Vegas; Orlando box patchy — use team-level) | ❌ | ❌ |
| pre-2010 | ❌ (no data) | ❌ | ❌ |

---

## 12. Gaps / explicit non-computables

- **True possessions & pace pre-2019** — estimate only (bbref formula). Acceptable; label.
- **On/off, lineups, clutch pre-2019** — not available (no PBP).
- **Position estimates** (bbref PBP table) — needs lineup inference; recommend skip.
- **BPM/VORP/WS validity for SL** — computable but calibration-invalid; product decision pending.
- **Tracking stats** (defensive matchups, contested shots) — N/A any era (known).
- **Dunk/specific-action flags** — only via ACTION_TYPE string mapping; partial.

---

## 13. Recommended next steps

1. **Product decision:** which composite metrics ship (GmSc + rate stats core; BPM/VORP/WS
   omit-vs-badge). Feeds the toggle set (Per Game / Per 36 / Per 100 / Advanced) in the spec.
2. **Pin the Tier-2/Tier-3 floors** (2013/2016/2018 probe) so the per-era availability table is exact.
3. **Schema:** the inventory implies the column set for `summer_league_player_game_logs` (raw box)
   and the rebuildable season/advanced aggregates — draft `summer_league_*` tables from §1–§5.
4. Fold the §9 caveats into the spec's "Composite metrics — proceed with caution" section.
