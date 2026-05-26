# Summer League Stats — Page Inventory & Layout Plan

Full-scope catalog of every page the SL feature introduces or modifies. Anchor doc for upcoming HTML mockups in `mockups/` and the eventual ticket set. Pruning happens later — this lists everything we'd build if time were unconstrained.

**Companion docs**

- Pitch: `docs/plans/summer-league-stats-pitch.md`
- Spec: `docs/summer_league_stats_plan.md`
- Existing visual language reference: `docs/style_guide.md`, `mockups/draftguru_homepage.html`, `mockups/draftguru_stats_homepage.html`, `mockups/draftguru_player.html`

---

## Page Inventory at a Glance

| # | Route | Purpose | Status |
|---|-------|---------|--------|
| 1 | `/stats` | Stats hub — combine + SL preview, room for future modules | Migrated (currently combine landing) |
| 2 | `/stats/combine` | Combine landing | Migrated from `/stats` |
| 3 | `/stats/combine/{metric_key}` | Per-metric leaderboard | URL-renamed, content unchanged |
| 4 | `/stats/combine/{draft_year}` | Combine year page | Existing |
| 5 | `/stats/summer-league` | SL landing | New |
| 6 | `/stats/summer-league/{year}` | Season hub (multi-venue) | New |
| 7 | `/stats/summer-league/{year}/{venue}` | Single venue within a season | New |
| 8 | `/stats/summer-league/{year}/{venue}/{team}` | Team-season page | New |
| 9 | `/stats/summer-league/{year}/games/{game_id}` | Box score + PBP splits | New |
| 10 | `/stats/summer-league/leaders` | Season + career leaderboards | New |
| 11 | `/stats/summer-league/explorer` | Faceted query builder | New |
| 12 | `/stats/summer-league/all-summer-league` | Historical SL awards / selections | New |
| 13 | `/stats/summer-league/teams/{team}` | Franchise SL history | New |
| 14 | `/stats/summer-league/{year}/draft-class` | Current class's SL performance, sorted by pick | New (moat) |
| 15 | `/stats/summer-league/model-validation` | Pre-draft consensus vs. SL composite | New (moat) |
| 16 | `/players/{slug}` (SL section) | New SL section on existing player page | Extension |
| 17 | `/players/{slug}/summer-league/{year}` | Per-game logs for one player-season | New |
| 18 | Share cards (PNG) | Social-share exports for SL views | New |
| 19 | Admin: SL roster-resolution queue | Internal review for ambiguous entity matches | New (admin) |

---

## Shared Components & Conventions

Defined once, referenced by each page below. These are the building blocks for mockups.

### Stat Tables (the workhorse)

Every stat table in the SL section uses the same chrome:

- **Sticky header row** and **sticky first column** (player or team name).
- **Per-mode toggle** above the table: `Per Game · Per 36 · Per 100 · Totals · Advanced`. **Universal default: Per Game.** Toggle persists in URL for shareable views.
- **Min-sample filter** on leaderboards and the Explorer: **default 60 total MIN + 2+ GP**; always-visible slider for power users to tighten or loosen.
- **Heat-shaded cells** (pale green top quartile, pale red bottom quartile) on commensurate stat columns.
- **Top-N markers** — gold/silver/bronze dot on top 3 in any leaderboard column.
- **Sample-size badge** — small grey "5 GP · 87 MIN" pill next to per-game / rate stats. Italicize rate-stat values with <30 total minutes.
- **Confidence dimming** — 90% opacity on rows where `data_quality` is `box_only` / `partial`; tooltip explains.
- **Cross-link discipline** — player name → player SL page; team name → team-season; game date → box score; season year → season hub; venue → venue-season.
- **Mobile** — sticky first column, horizontal scroll for stats, "Compact view" toggle reducing to PTS/REB/AST/+- only.

### Cross-Link Map (used everywhere)

| Element | Link target |
|---------|-------------|
| Player name | `/players/{slug}` (with SL anchor) or `/players/{slug}/summer-league/{year}` in SL context |
| Team name (in-season context) | `/stats/summer-league/{year}/{venue}/{team}` |
| Team name (cross-year context) | `/stats/summer-league/teams/{team}` |
| Game date or score | `/stats/summer-league/{year}/games/{game_id}` |
| Season year | `/stats/summer-league/{year}` |
| Venue name | `/stats/summer-league/{year}/{venue}` |
| Draft year on player chip | `/stats/summer-league/{year}/draft-class` |

### Sample-Size & Data-Quality Affordances

- **Sample-size badge** (described above) appears next to every player-rate stat.
- **Data quality badge** — small chip near page header on year/venue/team pages: `Full` (PBP), `Box only`, `Partial`. Linked to a one-paragraph methodology explainer.
- **Composite metric caveats** — BPM/PER/etc. show inline disclaimer chip ("calibrated to NBA RS; SL noise high") on hover/tap.

### Year Picker

Horizontal scrollable **scoreboard strip** — one tile per year, each showing a data-quality badge inline (`Full` / `Box only` / `Partial`). Swipeable on mobile, scrolls with arrows on desktop. Matches the existing scoreboard/ticker motif from the homepage. Used on every page that needs a year selector (landing, season hub, leaders, explorer, all-summer-league, model-validation).

### Δ-vs-Consensus Coloring

Δ-vs-consensus-rank columns (draft-class page, model-validation page) use the **canonical green/red palette as an arrow chip** (`↑+12` green pill / `↓−5` red pill), *not* as cell background. This keeps "green = good, red = bad" semantics consistent across the site while distinguishing visually from the heat-shaded top-quartile cells in adjacent columns by *form* (chip vs. fill).

### Live / Refresh Posture

SL pages are **daily-refresh, no live polling**. No "LIVE" chips, no auto-refreshing tickers, no WebSocket plumbing. Today's games show on a "Today" / "Yesterday" / dated grouping; box-score and aggregate pages reflect the most recent successful ingestion (small "Updated: 2026-07-12 10:14 PT" timestamp in the page footer). Consistent with DraftGuru's aggregator-not-broadcaster positioning and protects NBA.com rate limits.

### Mobile Compact-View Defaults

Each table type has an explicit 4-column compact set that survives when the user toggles "Compact view" on mobile. Full table available via "Show all stats."

| Table type | Compact columns | Notes |
|------------|-----------------|-------|
| Player game logs (per-player-season page) | Opp · MIN · PTS · REB+AST | Date in row header; opp more useful in compact context |
| Per-season SL table (on player page) | Year · GP · PTS · TS% | Career arc + efficiency |
| Per-stat leaderboard | Rank · Player · Team · Stat | Already narrow; no truncation needed |
| Draft-class table | Pick · Player · Composite · Δ | The whole point of the page — Δ must survive |
| Team-season roster | Player · MIN · PTS · +/− | "Who played, how impactful" |
| Box score (per team) | Player · MIN · PTS · +/− | Match team-roster pattern |
| Franchise SL history (by-season) | Year · Record · Top Performer | Venue rolls into year cell |
| All-Summer-League historical roll | Year · 1st-Team Lead | Click to expand each year's full team |

### Analytical Voice (cross-cutting content rule)

DraftGuru is **not** a game-recap site. NBA.com, ESPN, and the official Summer League site already win on scores, schedules, and standings. DraftGuru's reason to exist is the **analytical layer** on top.

**Apply on every section that has copy:**

- **Lead with analysis, not facts.** "Flagg's TS% matches Banchero's 2022 SL peak" beats "Cooper Flagg scored 32 points." If a card's headline could appear verbatim on NBA.com, rewrite it.
- **Default frames:** cohort comparison (same draft class), historical comparison (same draft pick / position across years), pre-draft → SL delta (consensus rank, USG%, college rate vs. SL rate).
- **De-emphasize utility surfaces.** Schedules, standings, and raw result lists exist for navigation but should be visually lighter than the analytical surfaces. Lead the reader from a result toward what it means.
- **Storylines, not highlights.** Use "Storylines," "Patterns," "What We're Watching," "Class in Context" — never "Notable Moments" or "Highlights" framing alone.
- **Sample size is part of the voice.** Inline `5 GP · 87 MIN` badges are non-negotiable on rate stats — they signal that DraftGuru respects the reader's intelligence, and they keep small-sample takes from being misleading.
- **Add analytical columns** alongside raw stat columns on leaderboards (Δ vs. consensus rank, cohort percentile, pre/post-draft USG% delta, pace-adjusted, etc.) so even the "leader" surfaces tell a story.

### Share Affordance

A `Share as PNG` button on every public SL page. Output specs covered in the Share Cards section below.

---

## 1. `/stats` — Stats Hub (Migrated)

**Purpose:** Top-level entry to all statistical sections. Currently the combine landing — convert into a hub that hosts combine, summer league, and future modules without further URL churn.

**Above the fold**
- Hero row with the current "headline stat moment" (e.g., during SL: top SL performer of the week; off-season: a notable combine record).
- Two-card row: **Combine** and **Summer League** preview cards, each with a one-line tagline, a small data sample (current leader or recent record), and a primary CTA into the section.

**Sections**
- Combine preview card → links to `/stats/combine`.
- SL preview card → links to `/stats/summer-league`.
- "Coming soon" placeholder row (visually neutral) reserved for G-League / rookie-NBA expansion. Hidden if empty.
- Footer link block — "See all leaderboards" → `/stats/summer-league/leaders` (during SL) or `/stats/combine/{metric_key}` rotation.

**Controls / interactions**
- None beyond hover micro-interactions on the preview cards.

**Mobile**
- Cards stack; hero compresses to a single line + small chart.

**Migration notes**
- All current `/stats` content moves to `/stats/combine`.
- Add redirect `/stats/{metric_key}` → `/stats/combine/{metric_key}` (belt-and-suspenders).
- Internal links to fix: `app/templates/stats/index.html:23`, `app/templates/stats/metric.html` (lines 16, 130, 286, 296, 304, 311).

---

## 2. `/stats/combine` — Combine Landing (Migrated)

**Purpose:** Existing combine landing page, now living at its proper URL. No content change; URL move only.

**Above the fold / sections** — unchanged from current `/stats`.

**Migration notes** — see `/stats` above.

---

## 3–4. Combine sub-pages

`/stats/combine/{metric_key}` and `/stats/combine/{draft_year}` — content unchanged; URLs already half-namespaced. Mockups not needed (already shipped).

---

## 5. `/stats/summer-league` — SL Landing

**Purpose:** Front door to the SL section. The page non-SL fans see first; should communicate "this is comprehensive, with the current year and a deep archive."

**Above the fold**
- Banner with the current season's status: **In-season** (today's games + yesterday's results), **Just wrapped** (top performers, awards), or **Off-season** (next SL countdown + last year's recap). No live indicators.
- Year picker (scoreboard strip — see shared components) — defaults to most recent season.
- Hero metric panel: 2–3 standout numbers for the most recent year (top scorer, top rookie, biggest performance gap vs. consensus).

**Sections**
- **Today / yesterday's results** — most recent 5–10 games as cards; updated on daily ingestion, not live.
- **Leaderboard preview** — top 5 in PTS / REB / AST / Stocks for current season; "See all leaders" CTA → `/stats/summer-league/leaders`.
- **Draft class preview** — the current year's draft class top 5 SL performers by composite, with picks visible. CTA → `/stats/summer-league/{year}/draft-class`.
- **Explore the archive** — visual year strip 2002–current, each year a tile with `Full` / `Box only` / `Partial` badge.
- **By venue** — small grid: Vegas, Salt Lake, California Classic, Orlando (historical) with year-range chips.
- **Power tools** — Explorer CTA + Leaders CTA + All-Summer-League CTA, three large cards.

**Controls**
- Year picker (persists to all sub-pages via query param fallback).

**Cross-links** — every player/team/year/game element in the above sections.

---

## 6. `/stats/summer-league/{year}` — Season Hub (Multi-Venue)

**Purpose:** Everything that happened in a given SL year, across all venues.

**Above the fold**
- Header: "2026 NBA Summer League" with venue chips (Salt Lake · California Classic · Vegas) — each chip links to the venue page.
- Data-quality badge at top right (Full / Box only / Partial).
- Date-range strip.

**Sections**
- **Venue summary cards** — one per venue this year: dates, teams, top performer, link out.
- **Schedule + results** — chronological game list with date, teams, score, MVP, box-score link.
- **Season leaderboards** — tabbed: PTS · REB · AST · STL · BLK · TS% · USG% (PBP-gated). Min-minutes filter applied; per-mode toggle defaults to Per Game.
- **Draft class CTA** — large card: "How did the 2026 class do?" → `/stats/summer-league/{year}/draft-class`.
- **All teams** — alphabetical roster of every team that played, each linking to `/stats/summer-league/{year}/{venue}/{team}`.
- **Notable moments** — auto-generated highlights (50+ point games, season-high efficiency lines, etc.) if data supports.

**Controls**
- Venue filter chips, leaderboard tabs, per-mode toggle.

---

## 7. `/stats/summer-league/{year}/{venue}` — Single Venue

**Purpose:** Drill into one tournament within a season (e.g., Vegas SL 2026).

**Above the fold**
- Header: venue name + year + dates.
- Standings table (if applicable — Vegas runs a championship bracket; Salt Lake and California Classic are exhibition-only, so a flat results list instead).
- Data-quality badge.

**Sections**
- **Standings / bracket** (Vegas only) — record table; bracket visual if data permits.
- **Schedule + results** — every game at this venue.
- **Venue leaders** — same leaderboard tabs as the season hub, scoped to this venue.
- **Teams** — list of all teams that played here this year.

---

## 8. `/stats/summer-league/{year}/{venue}/{team}` — Team-Season

**Purpose:** One team's SL run at one venue in one year. The unit of "team identity" in SL — rosters turn over completely year to year.

**Above the fold**
- Team header: franchise logo + "Lakers · Vegas 2026" + record + final placement.
- Quick stats strip: PPG / OPP PPG / pace / net rating.

**Sections**
- **Roster table** — every player who appeared, with games played + per-game line. Cross-links to player SL pages.
- **Schedule + results** — chronological, with box-score links.
- **Team stat splits** — wins vs. losses, home vs. away (if venue layout allows), pace and ORtg/DRtg if PBP available.
- **Notable lineups** (PBP-gated) — most-used 5-man units with minutes and net rating.

---

## 9. `/stats/summer-league/{year}/games/{game_id}` — Game Box Score

**Purpose:** The atomic unit. Full box score for one SL game, with PBP-derived enrichments where available.

**Above the fold**
- Header: matchup, score, date, venue.
- Both teams' line scores (Q1/Q2/Q3/Q4/total).

**Sections**
- **Team box scores** — side by side, the canonical "bbref game" table. Per-mode toggle (Per Game / Totals / Advanced) — Totals default.
- **Lineup combinations** (PBP only) — every 5-man unit with minutes, +/−, ORtg/DRtg.
- **On/Off splits** (PBP only) — team performance with each player on vs. off.
- **Shot chart** (Tier 2+ only) — by team and by player toggle.
- **Game flow** (PBP only) — score-differential line over time.
- **Top performers card** — auto-generated narrative chip ("Castle: 28 PTS, season high").

**Cross-links** — every player and team name; the season year header links back to the season hub.

---

## 10. `/stats/summer-league/leaders` — Season + Career Leaderboards

**Purpose:** The "Stathead-lite" page for SL. Single-stat top-N tables filterable by season(s).

**Above the fold**
- Stat picker (large): PTS · REB · AST · STL · BLK · 3PM · TS% · USG% · GameScore · etc.
- Scope tabs: **This Season** · **All-Time** · **Single Game** · **Per-N Mode**.
- Min-minutes filter and year range slider.

**Sections**
- **Top 50 table** — sortable, with player, team-season, year, stat columns, sample-size badges, top-N markers.
- **Filters sidebar** (desktop) / drawer (mobile) — draft class, draft round, position, age at SL, college, country.
- **Save query / Share URL** — chip near the top that copies the current canonical URL.

**Controls** — comprehensive; this is the dense page.

---

## 11. `/stats/summer-league/explorer` — Faceted Query Builder

**Purpose:** The power surface. Stathead-style query builder that produces shareable sortable tables. Most of the SEO long-tail value lives here.

**Above the fold**
- Query summary chip ("Players · 2020–2025 · Vegas · 20+ MPG · TS% > league avg") + Share URL button.
- The result table.

**Sections**
- **Query builder panel** — collapsible. Subject (players / teams / games), time (year range, venue chips), player filters (position, draft class, draft round, draft pick range, pre-draft consensus rank range, age at SL, college, country), stat filters (min/max sliders for any stat, "had a game with X+ points" predicate), sort (any column, asc/desc, secondary).
- **Result table** — full stat-table chrome (per-mode toggle, sticky header, heat shading), unlimited rows with pagination.
- **Save query** — copyable URL fully encodes state.
- **Export CSV** — secondary action (low priority; mention but defer if needed).

**Mobile**
- Query builder collapses to a single "Filters" button that opens a full-screen sheet; result table uses the standard mobile pattern.

---

## 12. `/stats/summer-league/all-summer-league` — Historical Awards

**Purpose:** Year-by-year "All-Summer-League" selections — the equivalent of All-NBA but for SL. Editorial-feeling but driven by methodology.

**Above the fold**
- Year picker (scoreboard strip).
- Methodology callout: "Position-balanced teams using a pure stats composite. See methodology."

**Sections**
- **First / Second / Third Team** for the selected year — five players each (2G, 2F, 1C per team), with stat line. Position constraint is enforced; pure stats composite picks the player within each position slot.
- **MVP** card + **Most Improved** (multi-year players) + **Best Rookie** sub-awards.
- **Historical roll** — every year's First Team in a compact table, defaulting open from current year.
- **Methodology** — short text section, fixed at bottom. Locks the rule: positions sourced from `PlayerMaster`, composite score formula linked, no team-success adjustment.

---

## 13. `/stats/summer-league/teams/{team}` — Franchise SL History

**Purpose:** Cross-year team page. Each franchise's full SL résumé.

**Above the fold**
- Team header: franchise logo, all-time SL record.

**Sections**
- **By season table** — one row per year-venue, with record, top performer, championship/exhibition status. Each row links to the team-season page.
- **Franchise leaders** — top-5 single-game and career-SL performances by players in this franchise's SL uniforms.
- **All players** — alphabetical list of every player who's appeared for this team in SL, with their years.

---

## 14. `/stats/summer-league/{year}/draft-class` — Draft-Class Performance (Moat Page)

**Purpose:** "Did the lottery deliver?" — sort the current draft class by pick, show their SL performance. The page Draft Twitter will share.

**Above the fold**
- Header: "2026 Draft Class · Summer League Performance".
- Headline chips: biggest over-performer, biggest under-performer, top rookie composite.

**Sections**
- **Main table** — one row per drafted player, sorted by pick. Columns: pick · player · team · GP · MIN · PTS/REB/AST · TS% · USG% · Cohort percentile · Composite · Δ vs. consensus rank.
- Heat-shading on stat columns (cohort percentile, composite); Δ rendered as green/red arrow chip per shared-components rule (no cell fill — avoids reading two things into the same color).
- **Undrafted standouts** — secondary table below: undrafted SL players with strong lines.
- **Trend lines** — small sparkline column showing per-game arc through the SL run for players with 3+ games.

**Controls**
- Per-mode toggle, min-games filter, "Show undrafted" toggle.

---

## 15. `/stats/summer-league/model-validation` — Consensus vs. Reality

**Purpose:** Annual update of how pre-draft consensus rank tracked with SL composite. The retrospective "moat" page.

**Above the fold**
- Year picker (scoreboard strip; defaults to most recent class with ≥1 SL season).
- Headline: "Pre-Draft Consensus vs. Summer League · 2025 Class".

**Sections**
- **Scatter chart** — x = pre-draft consensus rank, y = SL composite. Players labeled; outliers highlighted.
- **Biggest beats** — top-10 over-performers (consensus was wrong, this player popped).
- **Biggest misses** — top-10 under-performers.
- **Calibration table** — by consensus tier (top 5, 6–14, 15–30, 31–60, undrafted), the avg SL composite.

**Note:** mockup can use placeholder data.

---

## 16. `/players/{slug}` — Player Page (SL Section Added)

**Purpose:** Extend the existing player page with an SL section. Above the fold stays pre-draft (consensus, bio); SL appears as the next chapter immediately below.

**New section added**
- **Header strip** — "Summer League" + most-recent SL year chip + total games chip + composite chip.
- **Per-season table** — one row per SL year-venue, with per-game line + advanced toggle.
- **Career SL totals** row.
- **Cohort percentile chip** for the player's draft class.
- **Latest SL games** — last 3 games, each a card linking to the box score.
- **CTA to per-season page** — "See full 2026 logs" → `/players/{slug}/summer-league/{year}`.
- **Empty state** — for players with no SL appearances yet: tasteful "Hasn't played Summer League yet" placeholder rather than hiding the section entirely (to keep page layout stable).

---

## 17. `/players/{slug}/summer-league/{year}` — Player-Season Logs

**Purpose:** Every game from one player's SL season. The drill-down from the player page.

**Above the fold**
- Player chip + "Summer League · 2026 · Salt Lake + Vegas".
- Season totals strip.

**Sections**
- **Game logs table** — one row per game: date · opp · venue · MIN · PTS/REB/AST · FG · 3P · FT · STL · BLK · TOV · +/− · GameScore. Cross-link every opponent name and date.
- **Per-game charts** — small line charts of PTS, MIN, USG% (PBP) across the season.
- **Splits** — wins vs. losses, home vs. away (where venue layout supports), against pre-draft top-30 opponents.
- **Shot chart** (Tier 2+) — aggregated across the season.

---

## 18. Share Cards (PNG Export Specs)

Following the existing share-card pattern (`docs/shareable_image_export_plan.md`, `shareable_image_export_plan_svg.md`). Each card is a single SVG → PNG export with the retro analytics treatment.

- **Player SL Season Card** — one player's full SL year in a single card. Stat line, top performance, composite chip. Sourced from `/players/{slug}/summer-league/{year}`.
- **Game Box Score Card** — both teams' final box + headline performance. Sourced from `/stats/summer-league/{year}/games/{game_id}`.
- **Leaderboard Top-10 Card** — snapshot of a single-stat leaderboard. Sourced from `/stats/summer-league/leaders`.
- **Draft-Class Scorecard** — the moat page's main table top-15, with consensus Δ. Sourced from `/stats/summer-league/{year}/draft-class`.
- **Explorer Query Card** — snapshot of the top N rows of a custom Explorer query. Sourced from `/stats/summer-league/explorer`.
- **Model-Validation Card** — yearly retrospective scatter highlights. Sourced from `/stats/summer-league/model-validation`.

Each card uses the existing retro motifs (scanlines, pixel corners, accent color), Russo One headings, Azeret Mono for numbers, and ends with the canonical share footer (`/stats/summer-league/...` short URL + DraftGuru wordmark).

---

## 19. Admin: SL Roster-Resolution Queue

**Purpose:** Internal triage for ambiguous tier-2 matches from SL ingestion. Not user-facing.

**Above the fold**
- Counts strip: `Pending · Auto-matched today · Created stubs today`.

**Sections**
- **Pending queue table** — one row per ambiguous match. Columns: SL roster name · candidate `PlayerMaster` matches (top 3, with fuzzy score) · context (year, venue, team) · action buttons (`Link to X`, `Create stub`, `Skip / mark unresolved`).
- **Recently resolved** — last N decisions, with undo affordance.

**Note** — patterns follow existing admin areas (likely under `/admin/...`).

---

## Resolved Layout Decisions

Decisions made during the 2026-05-25 pre-mockup pass. All baked into the shared-components and per-page sections above.

1. **Default per-mode:** Per Game, universal across all pages. Users toggle for Per 36 / Per 100 / Totals / Advanced; toggle state persists in URL.
2. **Min-sample floor:** 60 total MIN + 2+ GP on leaderboards and the Explorer; always-visible slider for power users.
3. **Year picker:** horizontal scrollable scoreboard strip with inline data-quality badges. One control everywhere a year selector is needed.
4. **Live affordances:** none. Daily-refresh only; small "Updated: …" timestamp in the footer; "Today" / "Yesterday" grouping for recent games but no LIVE chips or auto-refresh.
5. **Δ-vs-consensus coloring:** green/red arrow chips (`↑+12` / `↓−5`) — *not* cell-background fill. Keeps the universal green/red semantic; distinguished from heat-shading by form.
6. **All-Summer-League methodology:** position-balanced teams (2G, 2F, 1C) using a pure stats composite. Positions sourced from `PlayerMaster`. No team-success multiplier.
7. **Mobile compact-view defaults:** per-table-type, defined in the Shared Components section above.

---

## Methodology Placeholders (unresolved — flagged in mockups)

These are surfaces where the **visual treatment is settled but the underlying formula is not yet designed**. Mockups display placeholder values so the design pattern is visible, but every value below needs a methodology decision before implementation. Items here block the affected mockups' tickets, not the mockups themselves.

| # | Placeholder | Where it appears | What needs defining |
|---|-------------|------------------|---------------------|
| M1 | **SL Composite Score** (0–100 scale) | Player-page SL section chip · Draft-class page main table · Model-validation page · All-Summer-League selection driver · Δ-vs-consensus base | The formula. Candidates: Hollinger Game Score average · per-36 NBA-Efficiency-style blend · z-score blend vs. SL cohort scaled 0–100 · two-track display (GameScore + DG-Score). Each carries a tradeoff between off-the-shelf simplicity and SL-pace recalibration. |
| M2 | **Cohort definition for percentile** | Player-page cohort chip · Draft-class "Cohort %ile" column | Once M1 is defined, decide cohort scope: same draft class only (current default in the mockup) · same draft class with ≥N GP qualifier · positional cohort within the draft class · multi-year rolling cohort. |
| M3 | **Δ-vs-consensus rank delta** | Draft-class page chips (`↑ +5`, `↓ −9`) · SL landing draft-class preview | Need to define what "rank" the SL composite produces (rank within drafted players? within drafted + undrafted with stats? positional rank?) and what consensus rank is being subtracted (pre-draft consensus board as of draft day? rolling consensus?). |
| M4 | **GameScore variant** | Per-season table on player page · Game logs · Box-score top-performers | Hollinger formula directly · per-36 variant · custom DraftGuru variant. Lower-stakes than M1–M3 since GameScore is informational, not a sort key. |
| M5 | **All-Summer-League composite + position eligibility tie-breakers** | All-Summer-League page | Resolved decision is "position-balanced 2G/2F/1C using pure stats composite," but: which composite (depends on M1), how `PlayerMaster.position` maps to G/F/C (e.g., do combo guards count as G or F?), what's the per-position composite, and what's the tie-break when two players score identically. |

**Convention:** mockups touching any of the above show a top-of-page placeholder banner listing which deferred decisions affect that mockup, plus subtle inline `TBD` indicators next to the specific fields.

## Mockup Production Order (suggested)

Roughly outside-in: pages users encounter first, then drill-downs, then power tools. Each mockup goes in `mockups/draftguru_<page>.html` following the existing convention.

1. `/stats` hub (page 1)
2. `/stats/summer-league` landing (page 5)
3. `/players/{slug}` SL section (page 16) — the highest-traffic touchpoint
4. `/stats/summer-league/{year}` season hub (page 6)
5. `/stats/summer-league/{year}/games/{game_id}` box score (page 9)
6. `/stats/summer-league/{year}/draft-class` (page 14) — the moat
7. `/stats/summer-league/leaders` (page 10)
8. `/stats/summer-league/explorer` (page 11) — most complex; do last among publics
9. `/stats/summer-league/{year}/{venue}` and `/{team}` (pages 7–8)
10. `/players/{slug}/summer-league/{year}` (page 17)
11. `/stats/summer-league/model-validation` (page 15)
12. `/stats/summer-league/all-summer-league` (page 12)
13. `/stats/summer-league/teams/{team}` (page 13)
14. Share cards (page 18) — derive from finalized public-page layouts
15. Admin roster-resolution queue (page 19)
