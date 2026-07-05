---
name: inspect-mobile
description: Audit and debug mobile usability — sweep pages at a phone viewport, visually review screenful-by-screenful screenshots, detect unreachable/clipped content and broken horizontal scrolling, then trace any offender to its root cause. Use when the user reports mobile layout issues, before shipping mobile-facing UI, or as the mobile leg of visual verification.
allowed-tools: Bash, Read, Edit
---

# Inspect Mobile Usability

`scripts/mobile_audit.py` renders pages in a real headless Chromium with full
mobile emulation (390x844, DPR 2, touch, iPhone UA) against a live dev server.
It produces two complementary things, and **both are required**:

1. **Viewport segment screenshots** (`<route>.seg01.png` …) — exactly what a
   phone user sees, one screenful per swipe. These are legible when Read;
   the single full-page PNG of a long page is not (it downscales to an
   unreadable ribbon — use it only as a map).
2. **DOM-measurement report** (`report.md`) — unreachable content, dead
   scrollers, overflow, tap targets, tiny text, console errors.

The DOM checks find what screenshots can't show (content hidden past the
viewport edge); the screenshots find what DOM checks can't see (overlap,
squished/garbled layout, missing paint, inert JS that leaves a widget stuck
in its empty state). This codebase has shipped JS that passed every unit,
integration, and QA check while doing nothing at runtime — only looking at
rendered pixels caught it. Never report a page as "clean" from the report
alone.

## TL;DR

```bash
# 1. sweep (server must be running; pick a free port — 8000 is often taken)
make mobile-audit BASE=http://localhost:8003
# or directly, with specific routes / a player page / a game page:
conda run -n draftguru python scripts/mobile_audit.py sweep \
  --base http://localhost:8003 --routes / /players/<slug> /stats/summer-league/<yr>/games/<id>

# 2. VISUAL PASS (mandatory): Read the seg*.png files for every route you are
#    auditing and look at them like a user — is anything cut off, overlapping,
#    empty when it should have data, or obviously broken? The report flags
#    where to look ("Visual pass: READ ...seg01–segNN"), and says when the
#    page was longer than the segment budget (raise --segments for the rest).

# 3. read tests/visual/screenshots/mobile-audit/report.md for the DOM findings

# 4. trace any offender to find where its width goes
conda run -n draftguru python scripts/mobile_audit.py trace \
  --base http://localhost:8003 --route /players/<slug> --selector '#summerLeagueSection'
```

Exit code = number of routes with blocking findings (errors/non-200s,
console/page errors, unreachable content, page overflow, dead scrollers —
not tap targets or tiny text), so the sweep can gate CI
or a verification loop (visual-only defects still need your eyes — the exit
code only covers the DOM checks).

## What the DOM sweep checks (and why these specific checks)

- **Unreachable content** — elements extending past the right viewport edge
  with **no `overflow-x: auto` ancestor**. This is the highest-signal check:
  document-level `scrollWidth` is usually clean because *something* clips, yet
  a `nowrap` flex row (chip selectors, button rows) still hides its tail
  beyond the edge with no way to reach it. Page-level overflow checks alone
  miss every one of these.
- **Dead scrollers** — every `overflow-x` container wider than its window is
  actually scrolled to the end in-page (`scrollLeft` probe), proving the user
  can reach the last column, not just that the CSS says `auto`.
- **Console errors / page errors** — blocking. Broken client-side JS can
  leave a widget inert (stuck in its empty state) while every layout metric
  passes; this is the programmatic side of the inert-JS lesson above.
- **Page-level horizontal overflow**, **small tap targets** (<40px in either
  dimension), and **tiny text** (<11px) — triage-level context.
  Tap-target/tiny-text findings on data tables are often the retro-mono
  aesthetic, not bugs; use judgment before "fixing" them.

## Interaction states need driving, not just loading

Some mobile bugs only exist in a state you have to click into — e.g. the SL
game shot chart's player-chip row only renders after selecting a team. The
sweep captures the initial state only. For stateful UI, write a short
throwaway Playwright script (mobile context like `_new_page` in
`scripts/mobile_audit.py`): navigate → click into the state → screenshot the
element → run the same geometry probes. Then look at the screenshot.

## Debugging workflow

1. **Sweep, then do the visual pass on the segments** (step 2 above) before
   anything else. Note visual defects even when the DOM report is clean.
2. **Read `report.md`** for offenders the screenshots can't show; offenders
   are named by selector.
3. **Trace the box chain** for anything too narrow or overflowing:
   `trace --selector '<sel>'` prints each ancestor's width, padding, margin,
   max/min-width, and overflow. Width loss is almost always visible in one
   line of this output (e.g. a `width: 80%` container plus nested padding).
4. **Fix at the root.** Known patterns in this codebase:
   - Sections starved of width → check `.container` sizing in the mobile
     media query in `app/static/main.css` before touching per-page CSS.
   - Chip/button rows clipped → the row needs `overflow-x: auto` and its
     children `flex: 0 0 auto` (see `.slg-mode-selector` in
     `app/static/css/summer-league-games.css`).
   - A flex child poking out → it has `flex-shrink: 0` or an intrinsic
     min-content width; give it `min-width: 0` + ellipsis, or let it wrap.
   - Fixed-size images overflowing tiny phones → `max-width: 100%`.
5. **Re-sweep after fixing, and re-do the visual pass** — confirm the fix by
   *looking at* the fixed section rendered at mobile width, not just by the
   DOM numbers going green. Diff `report.md` against the previous run: layout
   fixes are often global (container sizing), so verify *all* routes, not
   just the one you fixed. Also re-check at `--width 320` (smallest phones);
   a layout can be clean at 390 and broken at 320.
6. Wide tables are *supposed* to scroll on mobile (repo convention:
   `.sl-table-wrap` etc.). Don't flag a table for being wider than the
   viewport — flag it only if its scroller can't reach the end or has no
   scroller at all.

## Gotchas

- `conda run` does not forward heredoc stdin — write throwaway Playwright
  scripts to a file and run the file.
- Element screenshots of tall sections can include the fixed navbar band
  mid-image (the page scrolls during capture); it's an artifact, not a bug.
- The shared Playwright MCP browser profile may be locked by another session;
  this script launches its own headless Chromium, so it never contends.
- Decorative pixel-corner pseudo-elements (`::after` with negative offsets)
  add ~4px to a container's `scrollWidth`; ignore sub-5px overflow on cards.
