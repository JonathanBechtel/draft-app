# DraftGuru — Business Models & Monetization Reference

_Last updated: 2025-11-xx_

This document summarizes the potential business models, revenue streams, and strategic considerations for the DraftGuru application.  
Agents should use this as a context layer when planning features, shaping data models, or making decisions that may influence user growth or monetization.

---

# 1. Core Value Proposition

DraftGuru is a **public-facing NBA Draft analytics platform** optimized for:
- High-quality scouting intel made visual and easy to digest  
- Player comparisons for debates, fantasy planning, and prospect research  
- Consensus rankings across multiple mock draft sources  
- Shareable social-media graphics to drive virality  
- Lightweight, fast, minimalistic UX that appeals to both fans and data-minded users  

Its commercial value comes from:
- Traffic  
- Trust  
- Stickiness (return visits)  
- Integrations with fantasy gaming, sports betting, and premium data experiences  

---

# 2. Primary Monetization Streams

Below are the most realistic and aligned revenue channels for DraftGuru.

## 2.1 Sportsbook & Fantasy Affiliate Revenue (Primary V1/V2 Model)
DraftGuru’s user base is ideal for:
- Sportsbooks (FanDuel, DraftKings, BetMGM, Caesars, ESPN Bet)
- DFS platforms (Underdog, Sleeper, PrizePicks)

**What drives conversion:**
- Player comps (“Player X resembles Player Y from Year Z”)  
- Draft predictions  
- Prospect debates  
- Mock draft updates  
- Compare page (ideal for prop-bet discovery)  
- High-intent users seeking actionable insights  

**Implementation notes:**
- Include call-to-action banners or context-aware offers  
- Track via UTM parameters on share cards  
- Geotargeting filters to show/hide offers depending on state  

This model is the most proven, highest-value, and easiest to integrate technically.

---

## 2.2 Fantasy/Trivia Skill Game (Daily Micro-Tournament)
A daily $5–$10 game where users guess:
- Draft positions  
- Comparisons  
- Prospect outcomes  
- Statistical trivia based on the app’s dataset  

Top % of players win cash; the “house” keeps 5–10% plus float yield.

**Why this works:**
- Draft fans love testing their knowledge  
- Low-stakes daily game encourages habit formation  
- Uses data the site already maintains  
- Share-friendly results  

**Revenue components:**
- House fee (5–10%)  
- Interest float from retained user balances  
- Withdrawal fee (optional)  
- Ads/sponsors inside the game interface  

---

## 2.3 Premium Analytics Tools (Subscription or One-Time Upgrade)
This excludes *custom scouting products* and stays within low-effort, high-value data features.

**Potential premium offerings:**
- Extended comp explorer (beyond the free top-5)  
- Downloadable CSVs of metrics (percentiles, mock history, similarity matrices)  
- Historical draft classes browser  
- Alerts/watchlists for player updates  
- Deeper consensus analysis (source weights, volatility over time)  

**Possible tiers:**
- $5/mo basic upgrade  
- $12–15/mo “Pro Analytics” tier  
- Seasonal (April–July) draft pass  

This tier focuses on **data enhancements**, not bespoke scouting.

---

## 2.4 Advertising & Sponsorships
Low priority but easy to layer in.

Formats:
- Banner ads  
- Sponsored compare sections  
- Sponsored consensus mock (“Powered by XYZ Data”)  
- Themed quizzes/trivia nights  

Ads should remain minimal to preserve DraftGuru’s clean aesthetic.

---

## 2.5 Data Licensing / API Access
Target customers:
- Media outlets  
- Fantasy newsletters  
- Small sports analytics sites  
- Research groups  

Possible datasets:
- Consensus mock histories  
- Percentile & z-score datasets  
- Player similarity matrices  
- College production aggregates  

---

# 3. Secondary or Future Monetization Options

## 3.1 Predictive Draft Model (ML-driven)
With enough historical data, DraftGuru can offer:
- Probability of top-10, lottery, first round  
- Expected statistical growth curves  
- Risk/volatility modeling  
- “Archetype probabilities” based on player clusters  

This is optional and not required for MVP.

---

## 3.2 Specialized Affiliates (Merch, Memorabilia, Training)
Lower expected value but feasible:
- Jerseys  
- Draft guides  
- Training programs  
- Prospect-related merch drops  

---

# 4. Strategic Considerations for Agents

Agents writing backend, frontend, or analytics code should keep in mind:

### 4.1 Shareable Surfaces Drive Growth  
Every feature should consider how it can be shared:
- PNG cards  
- Trivia results  
- Mock updates  
- Compare summaries  

### 4.2 Affiliate Conversion Depends on Intent  
High-intent pages:
- Compare  
- Player  
- Consensus  
- News feed  

These must be fast, clean, and scroll-friendly.

### 4.3 Simplicity > Complexity  
DraftGuru deliberately avoids:
- Framework-heavy frontends  
- Complex auth  
- Overbuilt infrastructures  

The stack must remain AI-friendly for rapid iteration.

---

# 5. Timeline & Maturity Curve

### Phase 1 (MVP – Traffic Generation)
- Player Pages  
- Compare  
- Consensus  
- News  
- Share cards  
- Light affiliate banners  

### Phase 2 (Retention & Engagement)
- Watchlists  
- Alerts  
- Trivia  
- Frequent updates and mock tracking  

### Phase 3 (Power Users)
- Premium analytics  
- CSV export  
- Comp explorer  

### Phase 4 (Monetization Maximization)
- Skill game  
- Expanded affiliates  
- API licensing  
- Predictive models  

---

# 6. M&A / Acquisition Fit Overview

DraftGuru’s most likely acquirers (in a $5M–$15M scenario):

**Sports Media:**  
- The Ringer  
- The Athletic  
- ESPN Digital  
- Bleacher Report  
- NBC Sports  

**Fantasy Platforms:**  
- Underdog  
- Sleeper  
- DraftKings  
- FanDuel  

**Sports Analytics Firms:**  
- Sports Reference  
- Basketball Index  
- Pro Insight  

**Gaming / Mobile Engagement:**  
- Gameflip  
- Skillz  
- Quiz/trivia game studios  

At acquisition time, buyers will value:
- Stable traffic  
- Clean, differentiated dataset  
- High affiliate revenue per user  
- Strong social presence via share cards  
- A brand associated with the draft cycle  

---

# 7. Final Notes for Agents

When implementing features:
- Consider shareability, retention, and conversion pathways  
- Keep codebase conventional, readable, simple  
- Maintain the light-retro design language  
- Prioritize performance and clarity  
- Use this document for macro-level product context  

