# Summer League Feature — Market & ROI Analysis

*Prepared June 1, 2026. Companion to `summer-league-stats-pitch.md` and `summer_league_stats_plan.md`.*

> **Bottom line:** Summer League is the right first move under DraftGuru's year-1 "ride the draft calendar" strategy — not for its standalone July revenue (modest), but because it is (1) DraftGuru's **first genuinely proprietary data asset**, (2) the cheapest place to validate the year-round traffic relay, and (3) a 2-year compounding SEO/retention play that delivers a warm audience into the high-monetization June 2027 draft window.

All revenue figures are modeled estimates with stated assumptions, not forecasts.

---

## 1. Strategic frame: the draft-calendar pipeline

DraftGuru's year-1 goal is to continuously ship a feature for the **next adjacent event on the NBA draft timeline**, building a relay that accumulates a retained audience and sets up monetization at the next draft peak. SL is the first feeder after the June draft itself.

| Window | Adjacent event | Feature | Role in pipeline |
|---|---|---|---|
| Late June | **NBA Draft** | Consensus / big board (have it) | Peak — the monetization moment |
| **Early–mid July** | **Summer League** | **SL stats (this feature)** | First handoff — keeps the just-drafted class alive |
| Aug–Sept | FIBA / EuroBasket / U19 intl | Intl prospect tracking | Bridges the deepest dead zone |
| Nov | **College season tips** | NCAA freshman / prospect tracking | Introduces *next* cycle's class |
| March | **March Madness** | Tournament riser/faller tracking | Biggest stock-movement event |
| Apr–May | Declarations, **Combine**, Lottery | Combine percentiles (data model exists) | Run-up to peak |
| → June 2027 | **Next Draft** | Everything compounds | Monetize the warm year-round audience |

**Implication for how to judge SL:** the KPI is **July→June retention** (how many SL visitors are still here when the 2027 cycle heats up), not in-the-moment SL traffic. The consensus/big-board page is the *destination* the relay feeds — keep it solid first; calendar events are feeders.

---

## 2. SL is DraftGuru's first proprietary moat

Every other DraftGuru surface — consensus, comps, news, percentiles, podcasts — is a **re-presentation of public inputs**. Defensible on UX and aggregation, but a competitor with the same sources can reproduce the dataset.

SL stats ingested from the NBA Stats API and **cross-linked to pre-draft consensus rank** is different: it's a dataset DraftGuru *constructs*, not one it re-packages. No competitor in the lane (RealGM, Basketball-Reference, CraftedNBA, To The Mean) stitches SL performance back to consensus. This is the first asset that's a **data moat**, not a design moat — and the first thing that makes the aggregator genuinely unique. (Note: this is additive to, not a contradiction of, the aggregator positioning — the editorial layer stays out of scope; this is data work.)

---

## 3. The demand pool

SL is a real, growing, concentrated attention event that lands in DraftGuru's calendar dead zone.

| Signal | Figure | Source |
|---|---|---|
| Total SL viewers (ESPN networks + NBATV) | ~17.9M (2022) | MVPindex |
| Avg viewers per Vegas SL game | ~238K–404K | MVPindex / ESPN |
| 2025 Vegas viewership YoY | **+27%** avg; championship 420K | ESPN Press Room |
| Social engagement (team accounts) | 40.2M engagements, 1.31B impressions | MVPindex |

Incumbent stats coverage (RealGM, Basketball-Reference) ranks for the queries but has thin, clunky, poorly cross-linked UX — a **low-competition, underserved query space**, the ideal SEO profile.

---

## 4. Traffic model

**Baseline: ~3K visitors/month (~36K sessions/yr), June 2026.** At this scale the ROI lens is proportional growth, not absolute revenue.

| Outcome | Incremental annual sessions | Effect on baseline |
|---|---|---|
| Modest success | +35–70K | **2–3x** |
| Strong (Explorer shared widely) | +100K+ | **4x+** |

**Critical timing caveat — ramp, not spike, in year 1.** At ~3K/mo with low domain authority, SEO discoverability takes 2–4 months to mature:

- **July 2026 (launch):** smaller spike, mostly existing audience + social/share. Still likely a record month, but not the full 40K.
- **Aug 2026 → July 2027:** evergreen long-tail compounds as pages rank — where the 2–3x lands and *sustains*.
- **July 2027:** spike arrives at full size, on top of an established archive.

The asset is a **two-year compounding play**, not a one-summer event.

---

## 5. ROI by business model

Direct year-1 cash is modest — be clear-eyed. Mapping base-case incremental traffic (~95K sessions, mature-state) onto `BUSINESS_MODELS.md` streams:

| Stream | Mechanism | Base-case | Notes |
|---|---|---|---|
| **Sportsbook/DFS affiliate** (primary) | ~95K sessions × ~0.3% click × ~8% deposit ≈ 23 conv. × ~$150 CPA | **~$3.5K** | CPA: sportsbook $100–400, DFS $30–100, RevShare 20–40%. Edge is *timing* — SL coincides with live summer prop markets + NBA-season run-up. |
| **Advertising** | ~135K PV × ~$8 sports RPM | **~$1.1K** | Negligible standalone. |
| **Premium / SL data export** | Seasonal pass, CSV, cohort percentiles | **~$0.5–2K** | Niche power users + researchers. |
| **Data licensing** | SL consensus-vs-performance dataset | $0 yr-1 | Optionality. |

**Direct total: ~$5–7K base** ($1.5K conservative / $20K+ optimistic).

---

## 6. Verdict

SL is **weak as a direct-revenue line in year 1 but strong on four indirect levers**, which is where the real ROI lives:

1. **First proprietary data asset** — a data moat, not a design moat (§2).
2. **Retention / habit** — fills the biggest gap in the engagement calendar; return-frequency multiplies every monetization stream. Measure July→fall cohort retention.
3. **SEO moat** — durable organic traffic in an underserved space lowers effective CAC site-wide; compounds over years.
4. **Affiliate timing + M&A narrative** — accumulates warm audience and delivers it into the June peak; completes the "consensus → SL → rookie season" story acquirers value (`BUSINESS_MODELS.md` §6).

**Cost:** non-trivial build (curl_cffi ingestion, 2010+ backfill, schema, player-page section, draft-class page, Explorer, share cards) but agent-orchestratable — low cash cost; real cost is dev opportunity vs. table-stakes gaps (big board, mock page).

**Recommendation:** Greenlight as a **proprietary-asset + retention + SEO option play**, sequenced *after* the table-stakes destination pages (big board, mock) are solid — since those are where the whole relay funnels during draft season. SL is the first and cheapest validation of the year-round pipeline thesis; if it fails to retain audience into the fall, that's a cheap early signal to rethink the relay before the harder intl/college builds.

---

### Sources
- [ESPN Press Room — 2025 Vegas SL viewership +27%](https://espnpressroom.com/us/press-releases/2025/07/2025-nba-las-vegas-summer-league-viewership-up-double-digits/)
- [MVPindex — SL 17.9M viewers / social engagement](https://www.mvpindex.com/insights/las-vegas-holds-court-for-nba-summer-league)
- [DraftKings affiliate terms](https://sportsbookapi.com/affiliate-programs/draftkings/)
- [FanDuel affiliate terms](https://sportsbookapi.com/affiliate-programs/fanduel/)
- [RealGM Summer League stats (incumbent)](https://basketball.realgm.com/nba/summer)
- Internal: `docs/BUSINESS_MODELS.md`, `docs/competitor_analysis.md`, `docs/plans/summer-league-stats-pitch.md`, `docs/summer_league_api_probe_findings.md`
