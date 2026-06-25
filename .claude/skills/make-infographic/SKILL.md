---
name: make-infographic
description: Produce one share-ready, player-forward infographic from live app data — a self-contained HTML file, a Twitter-sized PNG, and drafted post copy — visually verified by the infographic-qa sub-agent before it ships. Runs autonomously (picks a share-worthy angle from the data) or directed (you name the template/year/subject). Sibling to the x-thread skill; reuses the same data + draft conventions.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Make Infographic (render → verify → ship)

One invocation produces **one** reviewable infographic package: `infographic.html`
(self-contained), `infographic.png` (2× retina, drops into an X card), and
`tweet.txt` (engagement-optimized copy). It does not post — drafts are saved for
human review, mirroring the `x-thread` skill.

Core principle: **never ship a graphic you haven't looked at.** Every render goes
through the `infographic-qa` sub-agent, and you fix-and-reverify until it says
SHIP. The pixels are the source of truth, not the code.

All commands run from the repo root in the `draftguru` env
(`conda run -n draftguru python -m …`).

## Step 1 — choose template + subject

- **Directed** (user named it): use their template / year / subject.
- **Autonomous** (default): pass `--auto` and let `facts.py` score the candidates
  and pick the most share-worthy angle + template for the data.

Available templates:
- `scatter` — "Who Beat the Board?" outcome-vs-consensus scatter (the broad view).
- `leaderboard` — "Biggest Movers" two-column board with delta bars.
- `hero` — single-player "Steal of the Draft" / "Biggest Reach" card
  (`--mode steal|reach`).

Pull the headline facts you'll need for QA + copy (top-3 risers and fallers with
`name / Δ / team / #from→#to`, total picks, year). Render prints the chosen
`template`, `params`, `year`, `picks`, `faces`, and (for `--auto`) the `angle`:

```bash
# autonomous
conda run -n draftguru python -m scripts.infographics.render --auto --out /tmp/ig.html
# directed
conda run -n draftguru python -m scripts.infographics.render --template hero --mode reach --out /tmp/ig.html
```
(`--year YYYY` forces a year.)

## Step 2 — render + screenshot

The render command above writes the HTML. Then:
```bash
conda run -n draftguru python -m scripts.infographics.screenshot /tmp/ig.html /tmp/ig.png
```

## Step 3 — verify (the gate)

Launch the **infographic-qa** sub-agent (Agent tool, `subagent_type: infographic-qa`).
Pass it:
- the HTML path (`/tmp/ig.html`),
- the `facts` you gathered (top movers, counts, year) so it can check data fidelity.

It returns a structured `VERDICT: SHIP|FIX` with a precise issue list.

## Step 4 — fix loop (max 3 rounds)

If `VERDICT: FIX`, act on each BLOCK issue (and WARNs you agree with):
- Layout/overflow/alignment/geometry fixes live in the template module
  (`scripts/infographics/scatter.py`) or its layout constants, or in `theme.py`
  for shared chrome. Make the **smallest targeted edit** the QA region points to.
- Re-run Step 2 (render + screenshot) and Step 3 (re-verify).
- Repeat until `SHIP` or 3 rounds elapse. If still failing after 3, **stop and
  report the outstanding issues** to the user — don't ship a graphic the gate
  rejected.

## Step 5 — draft the tweet copy

Write **one** post (≤ 280 chars) optimized for engagement, in confident
analyst voice:
- Lead by naming the two extremes (biggest steal + biggest reach) with their moves.
- End on a low-effort reply hook ("who'd you actually want?").
- 1 hashtag max (`#NBADraft`); optionally one team tag if a take centers on it.
- Note for the poster: **image goes native in the post; put the `nbadraft.app`
  link in the first reply** (links in-post suppress reach).

## Step 6 — save the draft + report

Save to a draft dir mirroring x-thread:
`scripts/infographics/drafts/<YYYY-MM-DD>/<HHMMSS>_<template>_<slug>/` containing
`infographic.html`, `infographic.png`, `tweet.txt`, and a short `meta.json`
(template, year, facts, qa verdict). Report the directory + the final QA verdict
to the user, and paste the tweet copy inline for quick approval.

## Notes
- Drafts are gitignored (`scripts/infographics/drafts/`); they're review artifacts.
- If render exits non-zero (e.g. no draft results for the year), stop and tell the
  user — don't fabricate data.
- Keep player visuals front-and-center: if QA flags faces missing/not prominent,
  that's a blocker, not a nicety.
