# DraftGuru IP & Copyright Risk Assessment

**Date:** 2026-03-31  
**Purpose:** Comprehensive assessment of intellectual property and copyright liability risks across DraftGuru's content aggregation, AI image generation, and media embedding practices.

---

## Executive Summary

DraftGuru aggregates third-party content (news, podcasts, YouTube videos) and generates AI illustrations of real NBA draft prospects. This assessment identifies four risk areas, ranked by severity:

1. **AI-Generated Player Images (Moderate-High)** — Right of publicity exposure from generating recognizable likenesses of named athletes
2. **Podcast Audio Embedding (Medium)** — Full audio playback via hotlinked MP3s bypasses creator analytics and creates market substitution
3. **RSS/Substack Aggregation (Low-Medium)** — Automated ingestion exceeds Substack ToS despite excerpt-only storage
4. **YouTube Content Display (Low)** — Largely compliant; minor branding requirements

---

## 1. AI-Generated Player Images

### What We Do

- Generate flat vector illustrations of named draft prospects via Google Gemini API
- System prompt enforces non-photorealistic style, no team logos, no branded uniforms
- Admins can upload real player reference photos; these are analyzed by Gemini vision API to produce text descriptions of facial features, which then guide illustration generation
- Reference photos are stored privately and never displayed to users
- Generated images appear on player pages **without disclosure** that they are AI-generated
- Full audit trail stored: prompts, likeness descriptions, generation metadata

### Risk Analysis

**Risk Level: Moderate-High**

Key concerns:

- **Right of publicity** covers any recognizable depiction of a real person used for commercial advantage — illustrations included. *White v. Samsung* (9th Cir. 1992) found liability for a robot that merely evoked a celebrity's likeness.
- **EA Sports precedent** is directly on point: EA paid $60M+ settling claims from college athletes whose AI-rendered likenesses appeared in video games. Courts rejected the "transformative use" defense.
- **NIL era** means college athletes now have recognized commercial rights in their likeness, with agents and NIL collectives actively monitoring usage. Many draft prospects are current or recent college athletes.
- **Reference photo pipeline** strengthens a plaintiff's argument that generated output is deliberately capturing real likeness.
- **No disclosure** that images are AI-generated. Multiple states are enacting AI content disclosure laws (California, Tennessee, New York).
- **State legislation expanding:** Tennessee's ELVIS Act (2024), California's AB 1836/AB 2602 (2024), and New York's updated digital replica statute all extend publicity rights to AI-generated likenesses.

Mitigating factors:

- Editorial/informational context (analytics site providing stats, percentiles, scouting data)
- Stylized vector illustrations, not photorealistic
- Small scale reduces enforcement likelihood
- *CBC v. MLBAM* (8th Cir. 2007) protects use of names and stats in fantasy/analytics contexts (but not likenesses specifically)
- Players generally benefit from pre-draft coverage and visibility

### Recommended Actions

1. **Add "AI-generated illustration" labels** to every player image across all pages (highest priority, lowest effort)
2. **Add a site-wide disclosure page** explaining the image generation process
3. **Evaluate the reference photo pipeline** — the text-description intermediary step helps, but having a direct pipeline from real photo to generated image strengthens a plaintiff's argument
4. **Strengthen editorial framing** — ensure player pages are clearly analytical content, not primarily showcasing generated images

---

## 2. Podcast Audio Embedding

### What We Do

- Ingest podcast RSS feeds and store episode metadata (title, description, audio_url, artwork)
- Generate AI summaries and topic tags via Gemini
- Display episodes with show attribution and "Listen on [Show Name]" CTAs
- **Stream full audio** directly on site via HTML5 `<Audio>` element using the raw MP3 URL from RSS enclosures

### Risk Analysis

**Risk Level: Medium**

Key concerns:

- **Market substitution** — Users can listen to full episodes without visiting the podcast's platform. This is the critical factor that weakens a fair use argument.
- **Hotlinking MP3s** consumes creator hosting bandwidth without registering proper download/listener counts. Podcast monetization depends on download metrics reported by hosting providers (Libsyn, Buzzsprout, etc.) for ad revenue and sponsorship.
- **RSS enclosures aren't a playback license** — Providing an MP3 URL in a feed is intended for registered podcast players (Apple Podcasts, Overcast, Spotify) that register as downloads. A web app streaming via `<Audio>` may not register the same way.

### Recommended Actions

1. **Switch to official embed players** where available — most podcast hosts offer embeddable widgets that give creators proper analytics
2. **Alternatively, limit playback** to a preview clip (60-90 seconds) with a CTA for the full episode
3. **Investigate whether your audio plays register as downloads** on creators' hosting providers

---

## 3. RSS/Substack News Aggregation

### What We Do

- Ingest RSS feeds from 8 sources (mostly small Substacks and independent draft blogs)
- Store: title, description excerpt (NOT full text), URL, author, publish date
- Generate AI summaries and topic tags via Gemini
- Display article cards with source attribution, author credit, and "Read at [Source Name]" CTAs linking to originals
- No full-text storage; links drive traffic to original sources

### Configured Sources

| Source | Feed URL |
|--------|----------|
| Floor and Ceiling | floorandceiling.substack.com/feed |
| No Ceilings | noceilingsnba.com/feed |
| NBA Big Board | nbabigboard.com/feed |
| The Box And One | theboxandone.substack.com/feed |
| Draft Stack | draftstack.substack.com/feed |
| Ersin Demir | edemirnba.substack.com/feed |
| Assisted Development | assisteddevelopment.substack.com/feed |
| NBA Draft Room | nbadraftroom.com/feed |

### Risk Analysis

**Risk Level: Low-Medium**

Key concerns:

- **Substack ToS** restricts RSS usage to "RSS feed readers" and prohibits storing or redistributing content without creator consent. Automated ingestion + storage + AI summarization technically exceeds this.
- **Legal precedent:** *ThriveAP v. ACI* (11th Cir. 2021) established that publishing an RSS feed does NOT create an implied license to republish — $202,500 damages were awarded.
- **AI summaries** of copyrighted content remain legally unsettled. Courts have split: one 2025 ruling dismissed claims against bullet-point abridgments, while another found plausible infringement for substantial AI-generated excerpts.

Mitigating factors:

- Excerpt-only storage (no full text)
- Full attribution preserved (source name, author)
- CTAs drive traffic to original sources
- Small creators generally welcome the traffic and visibility
- Summaries are brief and factual, based on metadata rather than full articles
- No major media outlets in the source list

### Recommended Actions

1. **Informal outreach to Substack creators** — a simple email notifying them of inclusion and offering opt-out builds goodwill and informal permission
2. **Keep AI summaries brief and factual** — avoid paraphrasing creative analysis; focus on extraction-style summaries

---

## 4. YouTube Content (Including The Ringer)

### What We Do

- Ingest YouTube video metadata (title, description, thumbnail URL, YouTube link) from ~13 channels
- Store metadata in database indefinitely
- Display video cards with channel attribution and links to YouTube
- Hotlink thumbnails directly from YouTube CDN (i.ytimg.com) — not cached locally
- The Ringer NBA (@TheRingerNBA) is one configured channel

### Risk Analysis

**Risk Level: Low**

- **Thumbnails are hotlinked**, not cached — compliant with YouTube policy
- **Links drive views** to YouTube — DraftGuru functions as a traffic driver
- **The Ringer specifically:** No evidence of aggressive enforcement against aggregators. Usage is promotional for their channel.
- **YouTube API ToS** has a 30-day data retention rule for API-retrieved metadata, but storing video URLs/links and embedding is explicitly supported. The retention concern applies primarily to apps building competing offline databases.
- **Minor gap:** YouTube branding should appear next to YouTube-sourced content per API ToS.

### Recommended Actions

1. **Add YouTube branding/attribution** next to Film Room content
2. **Consider periodic metadata refresh** to keep titles/descriptions current and remove deleted videos

---

## 5. AI Content Summaries (Cross-Cutting)

### What We Do

All ingested content (news, podcasts, videos) receives AI-generated summaries and topic classification tags via Google Gemini API.

### Risk Analysis

**Risk Level: Low-Medium (legally unsettled)**

- Key legal factor is **market substitution** — if summaries reduce traffic to sources, fair use weakens
- DraftGuru's approach of summarizing metadata (not full articles) and driving traffic via CTAs is relatively defensible
- This area of law is actively evolving with no clear precedent

---

## Risk Summary

| Area | Risk Level | Enforcement Likelihood | Impact if Enforced |
|------|-----------|----------------------|-------------------|
| AI player images (likeness) | **Moderate-High** | Low-Medium | High |
| Podcast audio embedding | **Medium** | Low | Medium |
| RSS/Substack aggregation | **Low-Medium** | Very Low | Low-Medium |
| YouTube content display | **Low** | Very Low | Low |
| AI summaries (cross-cutting) | **Low-Medium** | Very Low | Low |

---

## Priority Action Items

| Priority | Action | Effort | Risk Reduced |
|----------|--------|--------|-------------|
| 1 | Add "AI-generated illustration" labels to all player images | Low | High |
| 2 | Add site-wide AI disclosure page | Low | Medium |
| 3 | Switch podcast embedding to official widgets or limit to preview clips | Medium | Medium |
| 4 | Add YouTube branding next to Film Room content | Low | Low |
| 5 | Outreach to Substack creators for informal permission | Low | Low-Medium |
| 6 | Evaluate reference photo pipeline risk/benefit | Low | Medium |

---

## Context & Caveats

- This assessment identifies legal risk areas but is not legal advice. Consult a media/entertainment attorney before scaling commercially.
- The sports media ecosystem broadly operates with informal norms around player photo usage that are rarely enforced at small scale. DraftGuru's AI illustration approach is more legally thoughtful than industry standard practice.
- Legislative landscape around AI-generated likenesses is evolving rapidly — reassess periodically.
- Enforcement economics favor DraftGuru at current scale, but risk increases with growth, revenue, and visibility.
