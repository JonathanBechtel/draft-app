---
name: inspect-mobile
description: Audit and debug mobile usability — sweep pages at a phone viewport for unreachable/clipped content, broken horizontal scrolling, small tap targets, and tiny text; then trace any offender to its root cause. Use when the user reports mobile layout issues, before shipping mobile-facing UI, or as the mobile leg of visual verification.
allowed-tools: Bash, Read, Edit
---

# Inspect Mobile Usability

`scripts/mobile_audit.py` renders pages headlessly at a phone viewport
(390x844, DPR 2, touch, iPhone UA) and reports the defects that full-page
screenshots alone miss. Run it against a live dev server.

## TL;DR

```bash
# 1. sweep (server must be running; pick a free port — 8000 is often taken)
make mobile-audit BASE=http://localhost:8003
# or directly, with specific routes / a player page / a game page:
conda run -n draftguru python scripts/mobile_audit.py sweep \
  --base http://localhost:8003 --routes / /players/<slug> /stats/summer-league/<yr>/games/<id>

# 2. read tests/visual/screenshots/mobile-audit/report.md and the PNGs

# 3. trace any offender to find where its width goes
conda run -n draftguru python scripts/mobile_audit.py trace \
  --base http://localhost:8003 --route /players/<slug> --selector '#summerLeagueSection'
```

Exit code = number of routes with blocking findings, so the sweep can gate CI
or a verification loop.

## What the sweep checks (and why these specific checks)

- **Unreachable content** — elements extending past the right viewport edge
  with **no `overflow-x: auto` ancestor**. This is the highest-signal check:
  document-level `scrollWidth` is usually clean because *something* clips, yet
  a `nowrap` flex row (chip selectors, button rows) still hides its tail
  beyond the edge with no way to reach it. Page-level overflow checks alone
  miss every one of these.
- **Dead scrollers** — every `overflow-x` container wider than its window is
  actually scrolled to the end in-page (`scrollLeft` probe), proving the user
  can reach the last column, not just that the CSS says `auto`.
- **Page-level horizontal overflow**, **small tap targets** (<40px),
  **tiny text** (<11px), and **console errors** — triage-level context.
  Tap-target/tiny-text findings on data tables are often the retro-mono
  aesthetic, not bugs; use judgment before "fixing" them.

## Debugging workflow

1. **Sweep first, then read the report — not just the screenshots.** Full-page
   PNGs of long pages are unreadably tall; the report names offenders by
   selector. Use element screenshots (Playwright `locator(...).screenshot()`)
   when you need to see a specific section.
2. **Trace the box chain** for anything too narrow or overflowing:
   `trace --selector '<sel>'` prints each ancestor's width, padding, margin,
   max/min-width, and overflow. Width loss is almost always visible in one
   line of this output (e.g. a `width: 80%` container plus nested padding).
3. **Fix at the root.** Known patterns in this codebase:
   - Sections starved of width → check `.container` sizing in the mobile
     media query in `app/static/main.css` before touching per-page CSS.
   - Chip/button rows clipped → the row needs `overflow-x: auto` and its
     children `flex: 0 0 auto` (see `.slg-mode-selector` in
     `app/static/css/summer-league-games.css`).
   - A flex child poking out → it has `flex-shrink: 0` or an intrinsic
     min-content width; give it `min-width: 0` + ellipsis, or let it wrap.
   - Fixed-size images overflowing tiny phones → `max-width: 100%`.
4. **Re-sweep after fixing** and diff `report.md` against the previous run —
   the container fix is global, so verify *all* routes, not just the one you
   fixed. Also re-check at `--width 320` (smallest phones); a layout can be
   clean at 390 and broken at 320.
5. Wide tables are *supposed* to scroll on mobile (repo convention:
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
