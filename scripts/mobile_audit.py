"""Mobile usability audit for DraftGuru pages.

Renders routes at a phone viewport with Playwright and reports the mobile
defects that page-level screenshots alone tend to miss:

- **Unreachable overflow**: elements extending past the right viewport edge
  with no scrollable (``overflow-x: auto``) ancestor — content the user can
  never reach. This is the highest-signal check; a page can report zero
  document-level overflow while a nowrap flex row still clips its children.
- **Document-level horizontal overflow** (page scrolls sideways).
- **Scroll containers**: every ``overflow-x`` wrapper wider than its viewport,
  with proof that ``scrollLeft`` can actually reach the end.
- **Small tap targets** (interactive elements under 40x40 CSS px).
- **Tiny text** (computed font-size below 11px).
- **Console errors**.

Each route also gets a full-page PNG for visual review.

Usage:
    # sweep the default route list against a dev server
    python scripts/mobile_audit.py sweep --base http://localhost:8000

    # sweep specific routes at a specific width
    python scripts/mobile_audit.py sweep --base http://localhost:8003 \
        --routes / /players/cameron-boozer --width 320

    # trace where an element's width goes (ancestor box chain)
    python scripts/mobile_audit.py trace --base http://localhost:8003 \
        --route /players/cameron-boozer --selector '#summerLeagueSection'
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, sync_playwright

DEFAULT_ROUTES = [
    "/",
    "/consensus",
    "/news",
    "/stats/summer-league",
    "/stats/summer-league/explorer",
    "/stats/summer-league/leaders",
    "/stats/summer-league/games",
    "/draft-recap",
]

MOBILE_VIEWPORT = {"width": 390, "height": 844}

CHECK_JS = """
() => {
  const vw = window.innerWidth;
  const doc = document.scrollingElement;

  const label = (el) => el.tagName.toLowerCase()
    + (el.id ? '#' + el.id : '')
    + (typeof el.className === 'string' && el.className
       ? '.' + el.className.trim().split(/\\s+/).slice(0, 3).join('.') : '');

  const inScrollableWrapper = (el) => {
    let p = el.parentElement;
    while (p && p !== document.body) {
      const s = getComputedStyle(p);
      if ((s.overflowX === 'auto' || s.overflowX === 'scroll')
          && p.scrollWidth > p.clientWidth) return true;
      p = p.parentElement;
    }
    return false;
  };

  // Elements poking past the right edge with no scrollable ancestor:
  // the user cannot reach this content at all.
  const unreachable = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > vw + 2 && !inScrollableWrapper(el)) {
      unreachable.push({sel: label(el), width: Math.round(r.width), right: Math.round(r.right)});
    }
  }

  // Scroll containers: verify each can actually scroll to its end.
  const scrollers = [];
  for (const el of document.querySelectorAll('body *')) {
    const s = getComputedStyle(el);
    if ((s.overflowX !== 'auto' && s.overflowX !== 'scroll')
        || el.scrollWidth <= el.clientWidth + 1) continue;
    const before = el.scrollLeft;
    el.scrollLeft = 1e6;
    const reached = el.scrollLeft;
    el.scrollLeft = before;
    scrollers.push({
      sel: label(el), clientW: el.clientWidth, scrollW: el.scrollWidth,
      canReachEnd: reached >= el.scrollWidth - el.clientWidth - 1,
    });
  }

  // Interactive elements with a hit area under 40x40 CSS px.
  const smallAgg = {};
  for (const el of document.querySelectorAll('a, button, input, select, [role="button"], [onclick]')) {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (!r.width || !r.height || s.visibility === 'hidden' || s.display === 'none') continue;
    if (r.width < 40 && r.height < 40) {
      const k = label(el);
      if (!smallAgg[k]) {
        smallAgg[k] = {
          sel: k, w: Math.round(r.width), h: Math.round(r.height), count: 0,
          example: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 30),
        };
      }
      smallAgg[k].count++;
    }
  }

  // Visible text nodes rendered below 11px.
  const tiny = {};
  for (const el of document.querySelectorAll('body *')) {
    const hasText = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    if (!hasText) continue;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    const fs = parseFloat(s.fontSize);
    if (fs < 11) {
      const k = label(el);
      if (!tiny[k]) tiny[k] = {sel: k, fontSize: fs, count: 0, example: (el.innerText || '').trim().slice(0, 25)};
      tiny[k].count++;
    }
  }

  return {
    viewport: vw,
    scrollWidth: doc.scrollWidth,
    pageOverflowPx: Math.max(0, doc.scrollWidth - vw),
    unreachable: unreachable.slice(0, 20),
    scrollers: scrollers.slice(0, 20),
    smallTapTargets: Object.values(smallAgg).sort((a, b) => b.count - a.count).slice(0, 12),
    tinyText: Object.values(tiny).sort((a, b) => b.count - a.count).slice(0, 12),
  };
}
"""

TRACE_JS = """
(sel) => {
  let el = document.querySelector(sel);
  if (!el) return {error: 'selector not found: ' + sel};
  const chain = [];
  while (el && el !== document.documentElement) {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    chain.push({
      sel: el.tagName.toLowerCase()
        + (el.id ? '#' + el.id : '')
        + (typeof el.className === 'string' && el.className
           ? '.' + el.className.trim().split(/\\s+/).slice(0, 3).join('.') : ''),
      width: Math.round(r.width),
      scrollW: el.scrollWidth,
      paddingL: s.paddingLeft, paddingR: s.paddingRight,
      marginL: s.marginLeft, marginR: s.marginRight,
      maxWidth: s.maxWidth, minWidth: s.minWidth,
      overflowX: s.overflowX, display: s.display, flexWrap: s.flexWrap,
    });
    el = el.parentElement;
  }
  return chain;
}
"""


def _new_page(browser: Browser, width: int, height: int) -> Page:
    """Create a mobile-emulating page (touch, DPR 2, iPhone UA)."""
    ctx = browser.new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
    )
    return ctx.new_page()


def _slug(route: str) -> str:
    """Turn a route path into a safe filename fragment."""
    return route.strip("/").replace("/", "-") or "home"


def run_sweep(base: str, routes: list[str], width: int, out_dir: Path) -> int:
    """Audit each route and write screenshots plus report.md / report.json.

    Returns the number of routes with at least one blocking finding
    (unreachable overflow, page overflow, dead scroller, error, or non-200),
    suitable for use as an exit code.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = _new_page(browser, width, height=844)
        console_errors: list[str] = []
        page.on(
            "console",
            lambda m: console_errors.append(m.text) if m.type == "error" else None,
        )
        page.on("pageerror", lambda e: console_errors.append(str(e)))

        for route in routes:
            console_errors.clear()
            entry: dict[str, Any] = {"route": route}
            try:
                resp = page.goto(
                    f"{base.rstrip('/')}{route}",
                    wait_until="networkidle",
                    timeout=45000,
                )
                entry["status"] = resp.status if resp else None
                page.wait_for_timeout(600)
                page.screenshot(
                    path=str(out_dir / f"{_slug(route)}.png"), full_page=True
                )
                entry["checks"] = page.evaluate(CHECK_JS)
                entry["consoleErrors"] = list(dict.fromkeys(console_errors))[:5]
            except Exception as exc:  # noqa: BLE001 - report per-route, keep sweeping
                entry["error"] = str(exc)[:200]
            results.append(entry)
            checks = entry.get("checks") or {}
            print(
                f"{route}: status={entry.get('status')} "
                f"pageOverflow={checks.get('pageOverflowPx')} "
                f"unreachable={len(checks.get('unreachable', []))}"
            )

        browser.close()

    (out_dir / "report.json").write_text(json.dumps(results, indent=2))
    (out_dir / "report.md").write_text(_render_report(base, width, results))
    print(f"\nreport: {out_dir / 'report.md'}")
    return sum(1 for r in results if _is_blocking(r))


def _is_blocking(entry: dict[str, Any]) -> bool:
    """Return True when a route has a finding that breaks mobile usability."""
    if entry.get("error") or (entry.get("status") or 0) >= 400:
        return True
    checks = entry.get("checks") or {}
    if checks.get("pageOverflowPx", 0) > 2 or checks.get("unreachable"):
        return True
    return any(not s["canReachEnd"] for s in checks.get("scrollers", []))


def _render_report(base: str, width: int, results: list[dict[str, Any]]) -> str:
    """Render sweep results as a markdown report."""
    lines = ["# Mobile audit report", f"Base: {base}, viewport width {width}px", ""]
    for r in results:
        flag = " ⚠️" if _is_blocking(r) else ""
        lines.append(f"## {r['route']} — status {r.get('status')}{flag}")
        if r.get("error"):
            lines.append(f"- ERROR: {r['error']}")
            lines.append("")
            continue
        c = r.get("checks") or {}
        ov = c.get("pageOverflowPx", 0)
        lines.append(f"- Page horizontal overflow: {'none' if ov <= 2 else f'{ov}px'}")
        if c.get("unreachable"):
            lines.append(
                "- **Unreachable content** (pokes past viewport, no scrollable ancestor):"
            )
            lines.extend(
                f"  - `{u['sel']}` width={u['width']} right={u['right']}"
                for u in c["unreachable"][:10]
            )
        dead = [s for s in c.get("scrollers", []) if not s["canReachEnd"]]
        if dead:
            lines.append("- **Scroll containers that cannot reach their end:**")
            lines.extend(
                f"  - `{s['sel']}` {s['clientW']} of {s['scrollW']}px" for s in dead
            )
        if c.get("smallTapTargets"):
            lines.append("- Small tap targets (<40x40):")
            lines.extend(
                f"  - `{t['sel']}` {t['w']}x{t['h']} x{t['count']} ({t['example']!r})"
                for t in c["smallTapTargets"][:8]
            )
        if c.get("tinyText"):
            lines.append("- Tiny text (<11px):")
            lines.extend(
                f"  - `{t['sel']}` {t['fontSize']}px x{t['count']} ({t['example']!r})"
                for t in c["tinyText"][:8]
            )
        if r.get("consoleErrors"):
            lines.append("- Console errors:")
            lines.extend(f"  - {e[:160]}" for e in r["consoleErrors"])
        lines.append("")
    return "\n".join(lines)


def run_trace(base: str, route: str, selector: str, width: int) -> None:
    """Print the ancestor box chain for an element to show where width is lost."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = _new_page(browser, width, height=844)
        page.goto(f"{base.rstrip('/')}{route}", wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(500)
        print(json.dumps(page.evaluate(TRACE_JS, selector), indent=1))
        browser.close()


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sweep = sub.add_parser("sweep", help="audit routes at a phone viewport")
    sweep.add_argument("--base", default="http://localhost:8000")
    sweep.add_argument("--routes", nargs="*", default=DEFAULT_ROUTES)
    sweep.add_argument("--width", type=int, default=MOBILE_VIEWPORT["width"])
    sweep.add_argument("--out", default="tests/visual/screenshots/mobile-audit")

    trace = sub.add_parser("trace", help="trace an element's ancestor box chain")
    trace.add_argument("--base", default="http://localhost:8000")
    trace.add_argument("--route", required=True)
    trace.add_argument("--selector", required=True)
    trace.add_argument("--width", type=int, default=MOBILE_VIEWPORT["width"])

    args = parser.parse_args()
    if args.cmd == "sweep":
        return min(run_sweep(args.base, args.routes, args.width, Path(args.out)), 125)
    run_trace(args.base, args.route, args.selector, args.width)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
