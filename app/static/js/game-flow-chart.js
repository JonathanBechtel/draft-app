/**
 * Game-flow (score-margin-over-time) SVG line chart.
 *
 * Reads window.SL_GAME_FLOW — an array of {t, margin} points where t is
 * elapsed seconds and margin is home_score - away_score — and renders a
 * lightweight SVG into #sl-game-flow-root.
 *
 * Positive margin → home team ahead (green); negative → away team (rose).
 * Period dividers are drawn at 12-min intervals (720 s) for regulation and
 * 5-min intervals (300 s) for overtime.
 */
(function () {
  'use strict';

  const SERIES = window.SL_GAME_FLOW;
  const ROOT = document.getElementById('sl-game-flow-root');
  if (!SERIES || !SERIES.length || !ROOT) return;

  // --- Layout constants ---
  const PAD = { top: 16, right: 12, bottom: 24, left: 28 };
  const SVG_W = 800; // intrinsic coordinate width (viewBox); CSS scales it
  const SVG_H = 160;
  const INNER_W = SVG_W - PAD.left - PAD.right;
  const INNER_H = SVG_H - PAD.top - PAD.bottom;

  const REG_PERIOD_S = 12 * 60;
  const OT_PERIOD_S = 5 * 60;
  const REG_PERIODS = 4;

  // --- Scales ---
  const maxT = SERIES[SERIES.length - 1].t || REG_PERIODS * REG_PERIOD_S;
  const margins = SERIES.map((p) => p.margin);
  const rawMax = Math.max(...margins, 1);
  const rawMin = Math.min(...margins, -1);
  const yExtent = Math.max(Math.abs(rawMax), Math.abs(rawMin), 5);

  function scaleX(t) {
    return PAD.left + (t / maxT) * INNER_W;
  }

  function scaleY(margin) {
    // Centre zero; positive margin → upward (lower SVG y)
    return PAD.top + INNER_H / 2 - (margin / yExtent) * (INNER_H / 2);
  }

  // --- SVG helpers ---
  function el(tag, attrs) {
    const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    return e;
  }

  const svg = el('svg', {
    viewBox: `0 0 ${SVG_W} ${SVG_H}`,
    preserveAspectRatio: 'none',
    role: 'img',
    'aria-hidden': 'true',
  });

  const g = el('g');
  svg.appendChild(g);

  // --- Zero line ---
  const zeroY = scaleY(0);
  g.appendChild(
    el('line', {
      class: 'sl-game-flow__zero-line',
      x1: PAD.left,
      y1: zeroY,
      x2: PAD.left + INNER_W,
      y2: zeroY,
    })
  );

  // --- Period dividers ---
  function periodStartSeconds(p) {
    if (p <= REG_PERIODS) return (p - 1) * REG_PERIOD_S;
    return REG_PERIODS * REG_PERIOD_S + (p - REG_PERIODS - 1) * OT_PERIOD_S;
  }

  // Determine how many periods this game lasted
  let period = 1;
  while (periodStartSeconds(period) < maxT) {
    const t = periodStartSeconds(period);
    if (t > 0 && t <= maxT) {
      const x = scaleX(t);
      g.appendChild(
        el('line', {
          class: 'sl-game-flow__period-line',
          x1: x,
          y1: PAD.top,
          x2: x,
          y2: PAD.top + INNER_H,
        })
      );
    }
    period++;
    if (period > 20) break; // safety
  }

  // Period labels (Q1…Q4, OT1…)
  const totalPeriods = period - 1;
  for (let p = 1; p <= totalPeriods; p++) {
    const midT =
      periodStartSeconds(p) +
      (p <= REG_PERIODS ? REG_PERIOD_S : OT_PERIOD_S) / 2;
    if (midT > maxT) break;
    const label = p <= REG_PERIODS ? `Q${p}` : `OT${p - REG_PERIODS}`;
    const t = el('text', {
      class: 'sl-game-flow__period-label',
      x: scaleX(midT),
      y: PAD.top + INNER_H + 12,
    });
    t.textContent = label;
    g.appendChild(t);
  }

  // --- Y-axis tick labels ---
  [yExtent, Math.round(yExtent / 2), 0, -Math.round(yExtent / 2), -yExtent].forEach(
    (v) => {
      const t = el('text', {
        class: 'sl-game-flow__ylabel',
        x: PAD.left - 4,
        y: scaleY(v),
      });
      t.textContent = v > 0 ? `+${v}` : `${v}`;
      g.appendChild(t);
    }
  );

  // --- Build polyline segments split by sign (home vs away lead) ---
  // We paint the full path in two colours: green for home-lead segments,
  // rose for away-lead segments, switching at zero crossings.
  function buildPath(points) {
    return points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0]},${p[1]}`).join(' ');
  }

  // Interpolate the exact x where margin crosses zero between two points
  function xAtZero(t1, m1, t2, m2) {
    const frac = m1 / (m1 - m2);
    return t1 + frac * (t2 - t1);
  }

  const homePts = [];
  const awayPts = [];

  for (let i = 0; i < SERIES.length; i++) {
    const { t, margin } = SERIES[i];
    const x = scaleX(t);
    const y = scaleY(margin);

    if (i > 0) {
      const prev = SERIES[i - 1];
      // Check for zero crossing
      if (
        (prev.margin > 0 && margin < 0) ||
        (prev.margin < 0 && margin > 0)
      ) {
        const crossT = xAtZero(prev.t, prev.margin, t, margin);
        const crossX = scaleX(crossT);
        const crossY = scaleY(0);
        homePts.push([crossX, crossY]);
        awayPts.push([crossX, crossY]);
      }
    }

    if (margin >= 0) {
      homePts.push([x, y]);
      // If away segment was building, cap it
      if (awayPts.length && awayPts[awayPts.length - 1][1] !== scaleY(0)) {
        // already handled at crossing above
      }
    } else {
      awayPts.push([x, y]);
    }

    // When margin === 0, add to both (no lead)
    if (margin === 0) {
      if (homePts[homePts.length - 1]?.[0] !== x) homePts.push([x, y]);
      if (awayPts[awayPts.length - 1]?.[0] !== x) awayPts.push([x, y]);
    }
  }

  if (homePts.length > 1) {
    g.appendChild(
      el('path', { class: 'sl-game-flow__line--home', d: buildPath(homePts) })
    );
  }
  if (awayPts.length > 1) {
    g.appendChild(
      el('path', { class: 'sl-game-flow__line--away', d: buildPath(awayPts) })
    );
  }

  ROOT.appendChild(svg);
})();
