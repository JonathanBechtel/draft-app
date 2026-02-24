# DraftGuru Prediction Market Vision

## Overview

An ambitious product direction that transforms DraftGuru from a draft analytics site into a **prediction market for basketball talent evaluation** — a platform where users stake money on their scouting takes and get proven right or wrong over time against real NBA outcomes.

---

## Why This Direction

Every argument on Twitter about "Player X is better than Player Y" is an unresolved prediction market. DraftGuru would be the platform that makes people put their money where their mouth is and keeps score.

### What exists today (and why it's insufficient)

- **DraftKings/FanDuel** — lets you bet on who goes #1. Resolves in one night. Shallow.
- **Prediction markets** (Polymarket, Kalshi) — politics and finance. Sports talent evaluation is wide open.
- **Fantasy basketball** — managing a roster week to week. Not about evaluating talent over time.

### What DraftGuru would offer

A platform focused on **being right about talent** — which is what draft culture is actually about. Structured, verifiable scouting positions priced by collective intelligence, resolved against real NBA performance data.

---

## Product Layers

### Layer 1 — Free Scouting Boards (Near-term)

Users publish personal big boards and prospect rankings. DraftGuru tracks accuracy against actual draft position and eventual NBA performance. Leaderboards rank the best scouts.

- **Purpose:** Free tier and acquisition engine
- **Key feature:** Accuracy tracking and public scouting track records
- **Revenue:** None (growth phase)

### Layer 2 — Daily Trivia Game (Near-term)

Paid daily contests build payment infrastructure, daily habits, and the wallet system.

**Proposed structure (at 500 daily players, $5 entry):**

| Tier | Players | Payout | Total |
|---|---|---|---|
| 1st place | 1 | $100 | $100 |
| 2nd-3rd | 2 | $50 | $100 |
| 4th-10th | 7 | $20 | $140 |
| 11th-25th | 15 | $10 | $150 |
| 26th-50th | 25 | $7 | $175 |
| 51st-100th | 50 | $5 | $250 |
| 101st-250th | 150 | $3 | $450 |
| **Total payouts** | **250 winners** | | **$1,365** |
| **Total pool** | 500 x $5 | | **$2,500** |
| **Your rake** | | | **$1,135 (45%)** |

**Key design decisions:**
- Top 50% of players receive some payout — optimizes for retention over short-term profitability
- $100 top prize (20x return) is the threshold where winners screenshot and share
- Bottom-tier winners get $3 on a $5 entry — "almost broke even, I'll play again tomorrow"

**Growth sequencing:**
1. Free trivia builds the daily habit (months 1-3)
2. Freeroll tournaments with subsidized prizes train the payment behavior (months 3-4)
3. Paid pools launch once there are a few hundred daily free players (month 4-6)
4. Self-sustaining at ~200+ paid players; profitable at 500+

**Retention mechanics:**
- Streak bonuses (7-day streak = multiplier or bonus entry)
- Seasonal rankings with bigger monthly prizes
- Profile pages ("Top 5% DraftGuru trivia player" as identity)

### Layer 3 — Prospect Markets (Medium-term)

Binary outcome markets on prospect performance, priced by user activity.

**Examples:**
- "Zaccharie Risacher will average 15+ PPG in his first two seasons"
- "This prospect will be a top-5 player from the 2026 class"
- "Player X will have a better career than Player Y"

Users take positions. Markets price collective belief. Outcomes resolve against real NBA stats over months and years.

**Why multi-year resolution is the flywheel:**
- Users are locked into the platform for *years*
- Portfolios of positions become an identity — a public scouting track record
- DraftGuru accumulates a proprietary dataset: what thousands of people believed about prospects, priced in real dollars, resolved against reality

That dataset is valuable to agents, media companies, and teams trying to understand how draft consensus forms and where it goes wrong.

### Layer 4 — Scout Reputation as a Product (Long-term)

The best evaluators develop verifiable, public track records. This becomes valuable to:

- **Media companies** looking for credible draft analysts
- **Podcasts and newsletters** wanting to hire proven evaluators
- **Users themselves** — a top-50 DraftGuru scout ranking becomes a credential

DraftGuru becomes the platform that **mints credible basketball voices**, not just ranks prospects.

---

## Revenue Model Summary

| Source | Timeline | Annual Revenue (est.) |
|---|---|---|
| Daily trivia rake (500 players) | Near-term | $400K |
| Withdrawal fees | Near-term | $20-50K |
| Betting/DFS affiliates (draft season) | Near-term | $50-150K |
| Dynasty fantasy subscriptions | Near-term | $50-100K |
| Prospect market transaction fees | Medium-term | TBD |
| Data licensing | Long-term | TBD |

**Near-term revenue potential: $150-400K/year**
**Valuation at 3-5x: $500K-$2M**

With prospect markets and data licensing, the ceiling is significantly higher.

---

## What We Already Have

DraftGuru's existing infrastructure directly supports this vision:

- Prospect database with stats, percentiles, and comps
- Consensus aggregation engine
- News and podcast infrastructure for context
- Share cards for viral distribution (growth loop for trivia results + scouting takes)
- Retro analytics aesthetic that differentiates from generic sports sites

**New pieces needed:**
- User accounts and authentication
- Wallet / payment system (Stripe)
- Trivia game engine + question generation from stats DB
- Positions/markets engine (Layer 3)
- Resolution system checking NBA stats on a schedule (Layer 3)
- Leaderboard and reputation system

---

## Risks and Considerations

### Legal / Regulatory
- Daily trivia with paid entry: must be structured as skill-based contest (not gambling). Draft knowledge trivia is strong here — answers are knowable facts, not chance.
- State-by-state compliance, void-where-prohibited language, terms of service. Budget $2-5K for initial legal review.
- Prediction markets are in an evolving legal gray zone (Kalshi won CFTC approval for event contracts). Real-money prospect markets would need serious legal counsel.
- **Phased approach:** free reputation tracking → play-money markets → real-money markets as regulatory clarity improves.

### Tax / Payments
- 1099 reporting for winners over $600
- Escrow handling for prize pools
- Chargeback management

### Liquidity (the core challenge)
- Need enough players in each daily pool for prizes to feel worth the entry fee
- 20 people in a $1 pool = $14 prize. Nobody cares. 500 people in a $5 pool = $100 top prize. That matters.
- Early prize pool subsidization is a user acquisition cost — and a cheap one compared to paid ads

---

## Competitive Positioning

The closest comp is what Kalshi or Manifold does for politics/world events, applied to the one domain where casual fans genuinely believe they know more than the experts — **evaluating basketball talent**.

That emotional conviction is the engine that drives transaction volume.

### Expansion Opportunity: NFL Draft
- NFL Draft interest is 5-10x NBA Draft search volume
- The architecture is sport-agnostic — player profiles, consensus aggregation, comps, news feeds all transfer
- Same codebase, much bigger TAM

---

## Phased Roadmap

Each phase funds and validates the next. Nothing speculative gets built — each step is justified by data from the previous phase.

### Phase 1: Build Traffic
**Goal:** Establish DraftGuru as a go-to draft resource with meaningful organic reach.

- SEO-optimized consensus mock draft pages (highest-intent search queries in the draft space)
- Share cards driving organic acquisition on Twitter/Reddit/Discord
- NFL Draft expansion to capture 5-10x the search volume with the same architecture
- Podcast and news aggregation for content depth and return visits

**Key metric:** Monthly unique visitors
**Exit criteria:** Consistent organic traffic during draft season; growing off-season baseline
**Revenue:** None — pure growth phase

---

### Phase 2: Affiliate Revenue + Free Trivia Game
**Goal:** Monetize existing traffic while building the daily retention habit.

- Betting/DFS affiliate integrations (DraftKings, FanDuel) — natural fit with consensus rankings and player comps during draft season
- Free daily trivia game launches — draft history, combine stats, player comps as question sources
- Leaderboards, streaks, and share cards for trivia results drive viral loops
- Dynasty fantasy subscription tier ($5-10/month) for year-round engagement

**Key metrics:** Affiliate conversion rate; daily trivia players; trivia retention (D7, D30)
**Exit criteria:** Hundreds of daily trivia players with strong retention; affiliate revenue covering operating costs
**Revenue:** $50-150K/year (affiliates + subscriptions)

---

### Phase 3: Paid Trivia Game
**Goal:** Convert the daily habit into direct transactional revenue.

- Freeroll tournaments with subsidized prizes transition users to transactional behavior
- Paid pools ($5 entry) with top-50% payout structure optimized for retention
- Wallet system with balance management, withdrawal fees
- User accounts, payment infrastructure (Stripe), legal/compliance groundwork
- Seasonal events (Draft Night Championship with elevated prize pools)
- Private pools for friend groups and fantasy leagues (organic growth channel)

**Key metrics:** Daily paid players; rake revenue; wallet retention (users keeping balances)
**Exit criteria:** 500+ daily paid players; self-sustaining prize pools; ~$400K/year rake
**Revenue:** $200-400K/year

---

### Phase 4: Prediction Market
**Goal:** Transform DraftGuru from a game into a platform for basketball talent evaluation.

- Free scouting boards with accuracy tracking launch first (zero regulatory risk)
- Play-money prospect markets test mechanics and user appetite
- Real-money markets on structured prospect outcomes, pending legal/regulatory clarity
- Multi-year resolution engine checking NBA stats against open positions
- Scout reputation system — verifiable public track records become credentials
- Data licensing to media companies, agencies, and teams

**Key metrics:** Open positions; market volume; scout reputation engagement; data licensing inquiries
**Exit criteria:** Active prediction market with multi-year positions and a proprietary scouting intelligence dataset
**Revenue:** Transaction fees + data licensing (ceiling significantly higher than trivia alone)

---

### Phase Summary

| Phase | Focus | Revenue | De-risks |
|---|---|---|---|
| 1. Traffic | SEO, share cards, NFL expansion | $0 | Proves audience exists |
| 2. Affiliates + Free Trivia | Monetize traffic, build daily habit | $50-150K | Proves retention + willingness to engage daily |
| 3. Paid Trivia | Direct transactional revenue | $200-400K | Proves users will pay, builds payment infra |
| 4. Prediction Market | Platform for scouting intelligence | $400K+ | Each prior phase validates the next |

---

## The Core Thesis

> Do people care enough about being right about prospects to build a public track record and put money behind their takes?

If the answer is yes — and draft Twitter suggests it overwhelmingly is — then DraftGuru becomes a **platform**, not just a site. That's the difference between a $1M outcome and a $10M+ one.
