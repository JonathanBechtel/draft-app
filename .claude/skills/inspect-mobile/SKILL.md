---
name: inspect-mobile
description: Verify DraftGuru's mobile experience the way a human user would — drive the core user journeys with Playwright (tap, swipe, search, drill down), look at what the user actually sees, and judge it against expectations stated first. DOM probes and the audit script are tripwires, never the verdict. Use when the user reports mobile issues, before shipping mobile-facing UI, or as the mobile leg of verification.
allowed-tools: Bash, Read, Edit, Agent
---

# Inspect Mobile — verify the experience, not the code

## Why this skill exists

There is a recurring wedge between "the code ran, every test and check passed"
and "I opened the app and it works the way I expect." Mobile is the surface
most prone to it. This repo has shipped JS that passed unit, integration, and
QA checks while doing nothing at runtime; and the bug that motivated this
skill — shot-chart player chips unreachable past the screen edge — was
invisible to a clean page-load sweep, because it only exists after a tap.

So the rule: **the verdict comes from using the app like a person and looking
at what a person would see.** Scripts, DOM probes, exit codes — everything
else in this skill is instrumentation that narrows where to look. Instruments
never close an issue; a driven, rendered, *looked-at* page does.

## The behavior contract — what must work, as a user experiences it

These are the core behaviors to verify across the major surfaces (home,
consensus, player page, SL hub / explorer / leaders / games / game detail,
draft recap). Each one is verified by **doing it** in a mobile browser,
**screenshotting** what the user sees at each step, and **judging** against
expectations written down before looking.

1. **Arrive and orient.** The page loads readable at phone width — headline,
   nav, first panel. The hamburger menu opens and its links navigate.
2. **Find things.** Navbar search → suggestion → player page. Every major
   surface is reachable from the nav.
3. **Read everything the page offers.** Every panel shows its real data. Wide
   tables swipe to their **last** column. Nothing is clipped with no way to
   reach it.
4. **Every control changes what's on screen.** Rate-mode toggles, filters,
   tabs, expanders, scope chips: a tap visibly changes the page. A tap that
   does nothing is a bug even when nothing throws — silent inertness is the
   canonical wedge failure.
5. **Drill down and come back.** List → detail journeys end-to-end:
   consensus board → player; games index → game → shot chart → team scope →
   player scope; explorer → filter → drilldown → back.
6. **Survive the edges.** 320px width; sparse-data entities (a player with no
   SL games); long names; empty states. The happy-path exemplar passing says
   nothing about the branch a real user hits — pick at least one ugly case.

The contract is the durable artifact. The Playwright drivers you write to
exercise it are disposable — write them fresh per session against today's
page rather than maintaining a brittle journey framework that encodes
yesterday's DOM.

## Method

1. **Instrument sweep first** — `make mobile-audit BASE=http://localhost:8003`
   (dev server required; 8000 is usually taken). This is the tripwire:
   geometry defects, console errors, screenshots. Exit code counts blocking
   findings, but **exit 0 closes nothing** — it only means the tripwires
   didn't fire on initial page states.
2. **Drive the journeys.** For each contract behavior touching the changed
   surface, write a short Playwright driver (see recipe below) that does what
   a user does — tap, type, swipe — and screenshots after each step.
3. **State expectations, then look.** Before reading any screenshot, write
   down what should be visible in it ("per-100 columns now show possessions-
   scaled values", "all 11 player chips reachable"). Then read the screenshot
   and check each claim. "Looks fine" without prior claims is how empty
   panels and wrong data sail through.
4. **Fresh-eyes verdict.** You wrote the fix; you don't grade it. Spawn a
   read-only subagent (general-purpose) with the screenshot paths and the
   claim list, instructed to try to **refute** each claim and to flag anything
   a first-time user would find broken, unreadable, or unreachable. Ship only
   what survives.
5. **Trace to the root.** For any offender:
   `python scripts/mobile_audit.py trace --route <r> --selector '<sel>'`
   prints the ancestor box chain (width/padding/margin/max-width/overflow) —
   width loss is usually visible in one line of it.
6. **Re-verify by re-driving the journey** at 390px and 320px — not by the
   numbers going green. Layout fixes are often global (container sizing), so
   re-sweep all routes for regressions too.

## Playwright is the tool of record

Use Playwright's Python sync API (already in the conda env, used by
`tests/visual/` and `scripts/mobile_audit.py`). Standard mobile context:

```python
ctx = browser.new_context(
    viewport={"width": 390, "height": 844},  # 320 for smallest phones
    device_scale_factor=2, is_mobile=True, has_touch=True,
    user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) ...",
)
```

Rules that keep the driving honest:

- **Interact through hit-testing, never through JS.** `locator.tap()` /
  `locator.click()` go through the same target-finding a finger does and fail
  on covered or off-screen elements. `page.evaluate()` state mutation (setting
  `scrollLeft`, calling handlers directly) bypasses hit-testing and will pass
  bugs a finger hits — invisible overlays, missing `pointer-events`, elements
  past the viewport edge. Use `evaluate` to *measure*, never to *act*.
- **Screenshot what a user sees**: viewport-sized shots (`full_page=False`)
  after each action; element shots for a specific panel. Full-page PNGs of
  long pages are unreadably tall — use them only as maps.
- **Wait like a user waits.** `networkidle` plus a beat is the floor; charts
  and lazy panels can paint later. If a screenshot shows a skeleton/spinner,
  wait and re-shoot before judging.
- **iOS-flavored suspicion → real WebKit.** `p.webkit.launch()` runs actual
  WebKit; Chromium-with-iPhone-UA misses Safari-specific breakage (viewport
  units, sticky hover, safe-area insets).
- Launch your own headless browser (as the audit script does) — the shared
  Playwright MCP browser profile is often locked by another session.
- `conda run` does not forward heredoc stdin — write drivers to a file (the
  session scratchpad) and run the file.

## Instruments reference

- `make mobile-audit BASE=... [ROUTES="/a /b"] [WIDTH=320]` → sweep. Blocking
  findings: unreachable content (pokes past the viewport edge with no
  scrollable ancestor — the highest-signal check), dead scrollers, page
  overflow, console/page errors, non-200s. Triage-level: tap targets <40px in
  either dimension, text <11px (on data tables these are often the retro-mono
  aesthetic — judge before "fixing"). Writes `report.md`, `report.json`,
  full-page PNGs, and viewport segments (`--segments`, default 6; the report
  says when a page needed more — read them for every route you're auditing).
- `python scripts/mobile_audit.py trace --route <r> --selector '<sel>'` →
  ancestor box chain for root-causing width loss.

## Repo-specific fix patterns

- Sections starved of width → check `.container` sizing in the ≤768px media
  query in `app/static/main.css` before touching per-page CSS.
- Chip/button rows clipped → the row needs `overflow-x: auto` and children
  `flex: 0 0 auto` (see `.slg-mode-selector` in
  `app/static/css/summer-league-games.css`).
- A flex child poking out → `flex-shrink: 0` or intrinsic min-content width;
  give it `min-width: 0` + ellipsis, or let it wrap.
- Fixed-size images overflowing tiny phones → `max-width: 100%`.
- Wide stat tables are *supposed* to scroll (`.sl-table-wrap` convention) —
  flag only a scroller that can't reach its end, or content with no scroller.
- Decorative pixel-corner pseudo-elements add ~4px to `scrollWidth`; ignore
  sub-5px overflow on cards.
- Element screenshots of tall sections can include the fixed navbar band
  mid-image (capture scrolls the page); artifact, not a bug.
