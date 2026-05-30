# Summer League — Advanced Metrics Methodology (SL-calibrated)

**Date:** 2026-05-30
**Companion to:** `docs/summer_league_stat_inventory.md` (what we can compute) and
`docs/summer_league_api_probe_findings.md` (data we have).

## Decision

We **do** compute the full composite suite (PER, ORtg/DRtg, Win Shares, BPM/VORP) for Summer
League — but **recalibrated to the Summer League itself**, not to the NBA. Wherever a formula
references a "league average," that average is recomputed from the **SL `(year, venue)` pool** the
player actually competed in. The result is a self-contained, internally-consistent rate metric:
*"how did this player produce relative to the average player in this exact Summer League."* Small
samples make these noisy — accepted, surfaced (never hidden) with sample-size context.

This is deliberately a **reusable pattern**, not a one-off: the same "raw box → league context →
metric" pipeline is what a future **college-basketball** product reuses, with a different context.

## The core abstraction: `LeagueContext`

Every league-relative metric becomes a **pure function of four inputs**:

```
metric = f(player_totals, team_totals, opponent_totals, league_context)
```

A **`LeagueContext`** row is computed per `(competition, season, venue)` — for us
`("summer_league", 2024, "vegas")` — by aggregating every game in that pool:

| Context field | Aggregated from | Used by |
|---|---|---|
| `lg_pace` (poss / 48) | all team-games | PER, WS, BPM per-100, ORtg |
| `lg_pts`, `lg_fga`, `lg_fta`, `lg_orb`, `lg_drb`, `lg_trb`, `lg_ast`, `lg_fg`, `lg_ft`, `lg_tov`, `lg_pts_per_poss`, `lg_ppg` | league sums | PER factor/VOP/DRB%, WS marginal constants |
| `lg_avg_team_rating` (pts/100) | mean team ORtg | BPM team-adjustment centering |
| `lg_aPER` (the standardization scalar) | minute-weighted mean aPER | PER → 15-scale |
| `position_baselines` | positional means of each box rate | BPM position/role, percentile context |
| `replacement_level` | convention (see §BPM) | VORP |

The metric code is **competition-agnostic**; only the context rows differ. Store contexts in a
`metric_contexts` table keyed by `(competition, season, venue)` so college reuses the schema and
the formula code verbatim.

> **Build-from-totals, not from per-game means.** Aggregate season/league context from *summed
> totals*, then divide — never average per-game ratios. Tiny SL samples make per-game averaging
> badly biased.

---

## PER (Player Efficiency Rating) — fully recalibratable, clean

PER is **already** entirely league-relative; recalibration is just feeding SL context. From the
bbref/Hollinger formula, the league-dependent terms are:

```
factor = (2/3) - (0.5 * (lg_AST/lg_FG)) / (2 * (lg_FG/lg_FT))
VOP    = lg_PTS / (lg_FGA - lg_ORB + lg_TOV + 0.44*lg_FTA)
DRB%   = (lg_TRB - lg_ORB) / lg_TRB
```

`uPER` is then computed per player from their box line + team AST/FG using `factor`, `VOP`, `DRB%`.
Then:

```
pace_adj = lg_Pace / team_Pace          # team_Pace from SL games; or estimated 2*lg_PPG/(team_PPG+opp_PPG)
aPER     = pace_adj * uPER
PER      = aPER * (15 / lg_aPER)        # standardize so SL-average = 15
```

**SL recalibration:** all `lg_*` come from the `(year, venue)` `LeagueContext`; `lg_aPER` is the
minute-weighted mean aPER **within that pool**. So **SL-PER 15.0 = average player in that Summer
League**, not the NBA. ✅ Fully sound, Tier 1, all eras 2010+. *Caveat surfaced: tiny GP/MIN.*

---

## ORtg / DRtg — absolute, computed; relativized via context

Individual Offensive Rating (Dean Oliver) is **points produced per 100 individual possessions** —
an *absolute* quantity, not league-relative, so no recalibration needed to compute it. We have
every input (player line + team line + opponent line). The machinery:

```
ScPoss, FGxPoss, FTxPoss → TotPoss = ScPoss + FGxPoss + FTxPoss + TOV
PProd (points produced)
ORtg = 100 * PProd / TotPoss
```
(full FG_Part / AST_Part / FT_Part / ORB_Part expansion per the bbref Ratings article — all box-derivable.)

DRtg blends individual stops (STL, BLK, DREB) with the **team** defensive rating and a league-average
stop value. **SL recalibration:** the league-average stop value and the team DRtg baseline come from
the SL context. To make ratings *interpretable*, we present them against **SL-average ORtg/DRtg**
(per year/venue) — a "+6.2 vs SL avg" framing rather than a bare 112. ✅ Tier 1.

---

## Win Shares — recalibrate the league constants, keep the structural coefficients

WS = Offensive WS + Defensive WS, both built on **marginal** production vs a league baseline:

```
marginal_offense        = points_produced - 0.92 * (lg_pts_per_poss) * (off_possessions)
marginal_defense        = (player_MP/team_MP) * team_def_poss * (1.08*lg_pts_per_poss - DRtg/100)   # structural form
marginal_points_per_win = 0.32 * (lg_PPG) * (team_pace / lg_pace)
OWS = marginal_offense / marginal_points_per_win ;  DWS analogous ;  WS = OWS + DWS ;  WS/48 = 48*WS/MP
```

**SL recalibration:** `lg_pts_per_poss`, `lg_PPG`, `lg_pace` all from the SL `LeagueContext`;
`team_pace` from the SL team-season.

**Structural constants `0.92` and `0.32` are kept as-is.** They are not "NBA league averages" —
`0.92` is the replacement-offense fraction and `0.32` derives from the points-to-wins relationship;
both are roughly sport-universal. We **document** that these two coefficients are inherited from
Oliver's NBA derivation and are the one place WS is not fully SL-native. (Optional refinement: derive
points-to-wins from the SL pool's own Pythagorean fit — a later iteration, not launch.) ✅ Tier 1,
flagged.

---

## BPM / VORP — the honest version

BPM 2.0 is a **regression** whose coefficients were fit against NBA RAPM. We **cannot refit them for
SL** (no SL RAPM target exists, and never will at this sample). So we are explicit about what is and
isn't SL-native:

**Kept (NBA-derived, structural):** the box-stat regression coefficients and the
position/offensive-role constants. These encode "how much a unit of each box stat is worth" — a
basketball-universal weighting we inherit and label as such.

**Recalibrated to SL context:**
1. **Per-100 basis** uses **SL pace** (`lg_pace` from context) — stats are translated to per-100 *SL*
   team possessions.
2. **Team adjustment / centering.** Raw GmBPM is re-centered so that, within each SL `(year, venue)`,
   the minute-weighted mean player BPM = **0.0** and each team's minute-weighted player BPMs sum to
   that **team's efficiency relative to the SL-average team** (`lg_avg_team_rating` from context).
   The bbref team-adjustment constant (which "acts as the regression intercept," ≈ −8 in the NBA)
   is thus **re-solved against SL team ratings** — this is the key recalibration that makes
   "0.0 = average *Summer League* player."
3. **Position / offensive-role estimate.** BPM needs a 1–5 position and an offensive role. We
   approximate from `START_POSITION` + box rates (assist & usage share); for 2019+ we can refine
   from PBP. Documented as an estimate.

**VORP — adapt the proration (do NOT keep `82/82`).**
```
NBA:  VORP = (BPM - (-2.0)) * min% * (team_games / 82)
SL :  VORP_sl = (BPM - replacement) * poss% * (team_games / sl_team_games)   # i.e. per-SL-run, no 82 proration
```
- **Replacement level −2.0** is a scale convention (points/100 below average); we keep −2.0 as the
  default but expose it as a context parameter (`replacement_level`) so it can be tuned.
- **Drop the 82-game proration** — meaningless for a 3–7 game SL. Express VORP **per SL run** (or
  per-100-team-poss). Skip the `×2.7` wins-over-replacement conversion (NBA-calibrated); if "SL
  wins" is ever wanted, derive points-to-wins from the SL Pythagorean instead.

**Labeling:** present as **"BPM (SL-calibrated, box estimate)"** with a tooltip stating coefficients
are NBA-derived and the baseline/scale are Summer-League-native. ✅ computable; honest caveats.

---

## GmSc (Game Score) — no calibration needed

```
GmSc = PTS + 0.4*FG - 0.7*FGA - 0.4*(FTA-FT) + 0.7*ORB + 0.3*DRB + STL + 0.7*AST + 0.7*BLK - 0.4*PF - TOV
```
Per-game, intuitive, no league constants — the lowest-risk "one number" for SL. ✅ all eras.

---

## Small-sample treatment (applies to every rate metric)

SL samples are 3–7 games. Two safeguards, both surfaced not hidden:
- **Sample-size context everywhere** (GP · MIN pill; italicize rate stats under a minutes
  threshold) — already in the spec's visual plan.
- **Optional stabilized variant:** empirical-Bayes / James-Stein shrinkage of each player's rate
  toward the **positional mean of its `LeagueContext`**, with shrinkage ∝ 1/minutes. Lets us show a
  "stabilized" toggle beside the raw rate. This same shrinkage machinery is even more valuable for
  early-season college data — another reason to build it into the shared metric layer now.

---

## Beyond bbref — bbref is the floor, not the ceiling

Our data supports metrics bbref's standard player tables don't publish. Prioritized by
value × feasibility:

### Computable now from data we already pull
| Metric | Source | Tier | Why it beats bbref |
|---|---|---|---|
| **Four Factors** (eFG%, TOV%, ORB%, FT/FGA) at player & team | box | 1 | Oliver's framework; bbref shows team-only |
| **PIE** | advancedv2 (API) | 1.5 | NBA's all-in-one, free; SL-noisy, labeled |
| **Scoring profile** — % pts in paint / FB / off-TOV / FT, **% of FG assisted (PCT_UAST_FGM)** | scoringv2 (API) | 2019+ | shot-creation & scoring-context columns bbref lacks per-game |
| **Shot-quality vs zone expectation** — points-per-shot above the `(year,venue)` zone average | shotchartdetail + LeagueAverages | 2 (2015+) | "shot selection / shot-making above expected" — a real differentiator, fully SL-relative |
| **Opponent-adjusted net rating (SRS-style)** for teams & players | box, iterative solve over the SL pool | 1 | SL schedules are unbalanced; adjusting for opponent quality is high-value — **and is the headline metric for college** (SOS matters enormously) |

### PBP-enabled (Tier 3, 2019+)
| Metric | Note |
|---|---|
| On/off & lineup ratings, WOWY | parse substitution events |
| **Assisted-points created** / playmaking load | from PBP assist links (PLAYER2) |
| Clutch splits | last-5-min, ±5 |

### DraftGuru moat (built on the above)
SL-vs-consensus delta · cohort percentile within draft class · multi-year SL trajectory · career SL
totals — all computed on top of the SL-calibrated metrics.

> **Out of scope (no data, any era):** tracking-derived stats (defensive matchups, contested-shot
> rate, real potential-assists). Don't build affordances for data that doesn't exist.

---

## Schema implications (carry to the `summer_league_*` design)

The recalibration requires three aggregate levels persisted or rebuildable:
1. `summer_league_player_game_logs` — raw box (source of truth).
2. **`summer_league_team_seasons`** — team & opponent totals per `(team, year, venue)` (every
   rate-stat denominator + ORtg/DRtg baseline).
3. **`metric_contexts`** — the `LeagueContext` per `(competition, year, venue)` (§ core abstraction).
   Generalized name on purpose so college reuses it.

Season/advanced stats stay **materialized/rebuildable**, not base tables (per the spec) — so we can
retune formulas (shrinkage, replacement level, points-to-wins) without backfilling.

## Open decisions / next steps
1. Confirm **labeling** copy for NBA-derived-coefficient metrics (BPM/VORP, WS constants).
2. Decide whether the **stabilized (shrunk)** variant ships at launch or is a fast-follow toggle.
3. Pin **Tier-2/Tier-3 era floors** (small probe of 2013/2016/2018) so per-era metric availability
   is exact.
4. Prototype the **opponent-adjusted rating** solver on 2024 Vegas as the flagship "beyond-bbref"
   metric and SOS dry-run for college.
