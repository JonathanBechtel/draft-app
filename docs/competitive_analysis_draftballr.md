# DraftGuru vs. DraftBallr — Competitive Analysis

_April 2026_

## Overview

| | **DraftGuru** | **DraftBallr** |
|---|---|---|
| **Focus** | NBA Draft analytics + content aggregation | NBA Draft scouting + interactive tools |
| **Design** | Light retro analytics (Russo One, soft neutrals) | Dark cyberpunk terminal (JetBrains Mono, neon) |
| **Tech** | FastAPI + Jinja + vanilla JS (no build step) | Next.js + React + Tailwind (Vercel) |
| **Target** | Fans, fantasy players, casual analysts | Hardcore scouts, draft nerds, competitive users |

---

## Feature-by-Feature Comparison

| Feature | **DraftGuru** | **DraftBallr** |
|---|---|---|
| **Player Profiles** | Yes — bio, college stats, combine, percentiles, comps, news/pods/film | Yes — game logs, advanced stats, comparisons |
| **Combine Scores** | Yes — proprietary composite (anthro + athletic + shooting) | Yes — basic combine data |
| **Head-to-Head Compare** | Yes — side-by-side metrics with share cards | Yes — "Compare Engine" |
| **Similar Players** | Yes — multi-dimensional KNN similarity | Yes — "Comp Galaxy" (3D visualization) |
| **Mock Draft / Board** | Not yet (schema ready) | Yes — "My Board" drag-to-reorder + "Redraft Machine" |
| **Lottery Sim** | No | Yes — slot-machine style reveals |
| **Scouting Notes** | No | Yes — embedded notebook with bulk export |
| **News Aggregation** | Yes — AI-summarized RSS from 20+ sources | No visible news feed |
| **Film Room** | Yes — curated YouTube with tags/filters/player mentions | Yes — "Film Terminal" (details unclear) |
| **Podcasts** | Yes — aggregated with AI summaries + player tagging | No |
| **Stats Leaderboards** | Yes — combine leaderboards with position/year filtering | Yes — "Stat Matrix" with historical data back to 2008 |
| **Social Sharing** | Yes — PNG card export (6 card types) with OG tags | No visible share feature |
| **Multiplayer** | No | Yes — competitive redraft snake drafts |
| **Historical Data** | Current class focused | Database back to 2008 |
| **Admin/CMS** | Yes — full RBAC admin with CRUD, ingestion, image gen | Not visible |

---

## Where DraftBallr Beats Us

1. **Interactive/Gamified Features** — Redraft Machine (solo + multiplayer), Lottery Sim, and My Board give users *things to do*, not just things to read. These are sticky, return-visit drivers.

2. **3D Comp Galaxy** — Their similarity visualization is more visually impressive than our list-based comps. It's a "wow" feature for demos and social.

3. **Historical Depth** — Data back to 2008 means users can explore past drafts and validate comps against real outcomes. We're largely current-class focused.

4. **Custom Board Builder** — Drag-to-reorder personal big board is a core engagement mechanic we lack entirely. This is table stakes for draft sites.

5. **Dark Mode / Modern Aesthetic** — Their cyberpunk terminal look reads as "cutting edge" to a younger audience. Our retro style is distinctive but could feel dated to some.

6. **Scouting Notes** — Embedded notebook with export turns the site into a *tool*, not just a reference. Increases session time and lock-in.

---

## Where DraftGuru Beats Them

1. **Content Aggregation Engine** — News feed, podcasts, and film room with AI summaries and player tagging is a massive differentiation. They have zero content aggregation visible. Users come to us for the *full picture*, not just stats.

2. **Social Sharing Cards** — Our PNG export pipeline (VS Arena, Performance, Comps, Metric Leaders, Draft Year) is production-ready. They have no visible share feature. This is critical for organic growth.

3. **Combine Score System** — Our proprietary composite scoring (anthro + athletic + shooting with z-score normalization) is more sophisticated than what they display.

4. **Admin/CMS** — Our full admin panel with RBAC, ingestion management, and AI image generation means we can operate editorially. Their content appears more static.

5. **AI Integration** — Gemini-powered portraits, news summaries, podcast summaries, and player mention detection give us an AI content moat.

6. **SEO / Discoverability** — Server-rendered Jinja pages with proper OG tags beat their client-side React for search indexing (though Next.js SSR mitigates this somewhat).

---

## Strategic Gaps to Close (Priority Order)

1. **Big Board / My Board** — This is the #1 missing feature. Draft fans expect to build personal rankings. Consider a simple drag-to-reorder board with localStorage persistence (no auth needed initially).

2. **Mock Draft Simulator** — The schema is already in place. Even a basic single-player mock draft would match their Redraft Machine at minimum.

3. **Lottery Simulator** — Low-effort, high-engagement feature. Pure frontend JS with current lottery odds.

4. **Historical Draft Data** — Extending our data back several years would unlock "how did this comp actually turn out?" narratives that drive credibility.

5. **Dark Mode Toggle** — Low-hanging fruit that signals modernity. Our CSS variable system could support this without a redesign.

---

## Strategic Advantages to Protect

- **Content moat** (news + pods + film + AI summaries) — They can't easily replicate this
- **Share card pipeline** — Keep investing here; this drives organic social growth
- **Combine analytics depth** — Our composite scoring is genuinely better
- **Operational maturity** — Admin panel + ingestion pipeline means we can move faster on content

---

## Bottom Line

DraftBallr is strong on **interactivity and engagement** (boards, redrafts, lottery, 3D viz). DraftGuru is strong on **content, analytics, and shareability** (news aggregation, AI summaries, combine scores, share cards).

The most urgent gap is **My Board / mock draft** — these are table-stakes features for draft sites that we're missing entirely. The good news is our content aggregation and sharing pipeline are genuine competitive advantages they'd struggle to replicate quickly.
