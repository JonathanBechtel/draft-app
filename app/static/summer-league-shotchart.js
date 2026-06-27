/* summer-league-shotchart.js
 *
 * Renders a zone-heat half-court shot chart from window.SL_SHOTCHART.
 * Default: zone heat (FG% vs pool average → green/red gradient).
 * Toggle:  raw shot dots (make=filled green, miss=hollow red).
 * Suppressed (<20 FGA or missing data): graceful placeholder + table only.
 *
 * Data contract (window.SL_SHOTCHART):
 *   total_fga  : int
 *   suppressed : bool   — true → skip court, show table + small-sample note
 *   zones      : [ { shot_zone_basic, fga, fgm, fg_pct, freq_pct, pool_fg_pct } ]
 *   dots       : [ { loc_x, loc_y, made } ]   — may be absent/empty
 *
 * Mount point: <div id="sl-shotchart-root"></div>
 * Loaded by the page via {% block extra_js %}; no bundler.
 */
(function () {
  "use strict";

  /* ── Constants ─────────────────────────────────────────────────────────── */

  var SVG_NS = "http://www.w3.org/2000/svg";

  // Half-court viewBox: 500 wide × 470 tall (standard NBA half).
  // Origin (0,0) = top-left. Basket at (250, 415).
  var VB_W = 500;
  var VB_H = 470;
  var BASKET_X = 250;
  var BASKET_Y = 415;

  // NBA canonical zone labels (shot_zone_basic field values).
  var ZONES = [
    "Restricted Area",
    "In The Paint (Non-RA)",
    "Mid-Range",
    "Left Corner 3",
    "Right Corner 3",
    "Above the Break 3",
  ];

  // Zone display names (shorter for labels).
  var ZONE_SHORT = {
    "Restricted Area": "RA",
    "In The Paint (Non-RA)": "Paint",
    "Mid-Range": "Mid",
    "Left Corner 3": "LC3",
    "Right Corner 3": "RC3",
    "Above the Break 3": "ATB 3",
  };

  // Label anchor points (cx, cy) for each zone on the half-court SVG.
  var ZONE_LABEL_POS = {
    "Restricted Area":        { x: 250, y: 390 },
    "In The Paint (Non-RA)": { x: 250, y: 340 },
    "Mid-Range":              { x: 250, y: 250 },
    "Left Corner 3":          { x: 60,  y: 390 },
    "Right Corner 3":         { x: 440, y: 390 },
    "Above the Break 3":      { x: 250, y: 120 },
  };

  /* ── Colour helpers ────────────────────────────────────────────────────── */

  // Interpolate between two hex colours by t ∈ [0,1].
  function lerpHex(a, b, t) {
    var ar = parseInt(a.slice(1, 3), 16);
    var ag = parseInt(a.slice(3, 5), 16);
    var ab = parseInt(a.slice(5, 7), 16);
    var br = parseInt(b.slice(1, 3), 16);
    var bg = parseInt(b.slice(3, 5), 16);
    var bb = parseInt(b.slice(5, 7), 16);
    var rr = Math.round(ar + (br - ar) * t);
    var rg = Math.round(ag + (bg - ag) * t);
    var rb = Math.round(ab + (bb - ab) * t);
    return (
      "#" +
      rr.toString(16).padStart(2, "0") +
      rg.toString(16).padStart(2, "0") +
      rb.toString(16).padStart(2, "0")
    );
  }

  // Map a fg_pct vs pool_fg_pct to a fill colour.
  // delta > 0 → greener; delta < 0 → redder; no pool → neutral slate.
  var COLD   = "#f43f5e"; // --color-accent-rose
  var NEUTRAL= "#94a3b8"; // slate-400
  var HOT    = "#10b981"; // --color-accent-emerald
  var MAX_DELTA = 0.12;   // ±12 pp clamps to full red/green

  function zoneColor(fg_pct, pool_fg_pct) {
    if (fg_pct === null || fg_pct === undefined) return NEUTRAL;
    if (pool_fg_pct === null || pool_fg_pct === undefined) return NEUTRAL;
    var delta = fg_pct - pool_fg_pct;
    var t = Math.max(-1, Math.min(1, delta / MAX_DELTA));
    if (t >= 0) return lerpHex(NEUTRAL, HOT, t);
    return lerpHex(NEUTRAL, COLD, -t);
  }

  /* ── SVG helpers ───────────────────────────────────────────────────────── */

  function svgEl(tag, attrs) {
    var el = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs).forEach(function (k) {
      el.setAttribute(k, attrs[k]);
    });
    return el;
  }

  /* ── Court path data ───────────────────────────────────────────────────── */
  // All zones are drawn as SVG paths in the half-court coordinate system.
  // NBA key: 16ft wide = 160 units, free-throw line 190 from baseline = y=225 (VB_H-415+190 → 245 from top)
  // 3-point line: arc r=23.75ft=237.5 units from basket; corners at x=30/470, y >= 350 (=65 from baseline)
  // We scale: 1 foot = 10 units.  Basket at (250,415).
  // Baseline = y=470 (bottom edge, extended 5 units past basket for visual).
  // FT line:  y = 415 - 190 = 225.
  // Key width: 80 units each side of basket center → x=170 to x=330.
  // Restricted area: r=40 units (4ft).
  // Corner 3: x ≤ 30+220=250? No. Corners are x=30 (left) and x=470 (right), y from 415 down.
  //   In NBA: corner 3pt line is 22ft from basket at x=±220 from center; corners are at sideline (x=250±250).
  //   Simplified: corner 3 region: x<80 or x>420, y >= 350 (≥6.5ft from baseline).
  //   Above-break 3: arc area outside paint, not corner.

  // Zone paths as SVG path `d` strings. Clipped to half-court (y >= 0 means only top half shown).
  var ZONE_PATHS = {
    "Restricted Area": (function () {
      // Circle r=40 around basket (250,415), clipped by FT lane bottom at y=415 (same as basket).
      // Show a semicircle toward the top.
      return "M 210 415 A 40 40 0 0 1 290 415 Z";
    }()),

    "In The Paint (Non-RA)": (function () {
      // Key rectangle x=170..330, y=225..415 minus the restricted area semicircle.
      // Path: outer rect, then subtract RA arc.
      return (
        "M 170 415 L 170 225 L 330 225 L 330 415 " +
        "L 290 415 A 40 40 0 0 0 210 415 Z"
      );
    }()),

    "Mid-Range": (function () {
      // Region inside 3pt arc but outside paint, plus the area above the FT line inside 3pt arc.
      // 3pt arc: r=237.5 from basket (250,415), but clipped to court boundary.
      // Corner cutoffs: x<30 or x>470 (sideline). y<470 (court end). y>0 (top of our view).
      // This is complex; approximate as the 3pt arc region minus paint minus corners.
      // We draw: arc from left non-corner point to right non-corner point (y=350 on both sides, approximately).
      // Corner 3 region x <= 80 or x >= 420, y >= 350.
      // Left non-corner 3pt point: approx (30, 350) transitioning to arc.
      // For simplicity: mid-range = big arc region excluding paint and corners.
      // Path: outer 3pt arc (large) then inner paint rectangle.
      return (
        // Outer boundary: left sideline corner → up to arc start → arc → right arc end → down to right corner → across baseline
        "M 30 470 L 30 350 " + // left sideline going up to corner-3 top
        "A 237.5 237.5 0 0 1 470 350 " + // 3pt arc from left to right (approximate)
        "L 470 470 " + // right sideline down to baseline
        "Z " + // close outer shape
        // Inner paint cut-out (subtractive — browsers fill with even-odd).
        "M 170 415 L 170 225 L 330 225 L 330 415 " +
        "L 290 415 A 40 40 0 0 0 210 415 Z"
      );
    }()),

    "Left Corner 3": (function () {
      // Left corner region: x=0..30 to x=~80..., y=350..470
      return "M 0 470 L 0 350 L 30 350 L 30 470 Z";
    }()),

    "Right Corner 3": (function () {
      return "M 470 470 L 470 350 L 500 350 L 500 470 Z";
    }()),

    "Above the Break 3": (function () {
      // Everything above (lower y) the 3pt arc, within the half-court.
      // = court rectangle (0,0)→(500,0)→(500,470)→(0,470) MINUS mid-range MINUS paint MINUS corners MINUS RA.
      // Simplest: rectangle from y=0 to y=350 (approximate arc top) + the arc caps.
      // We approximate: above-break = court top down to the top of the 3pt arc.
      // The 3pt arc peaks at y = 415 - 237.5 = 177.5 ≈ 178 from top.
      return (
        "M 0 0 L 500 0 L 500 350 " +
        "A 237.5 237.5 0 0 0 0 350 " +
        "Z"
      );
    }()),
  };

  /* ── SVG court background lines ─────────────────────────────────────────── */

  function buildCourtLines(svg) {
    var g = svgEl("g", { class: "sl-shotchart__court-lines", stroke: "#334155", "stroke-width": "1.5", fill: "none" });

    // Court boundary
    g.appendChild(svgEl("rect", { x: 0, y: 0, width: VB_W, height: VB_H, fill: "#1e293b", stroke: "none" }));

    // Baseline
    g.appendChild(svgEl("line", { x1: 0, y1: VB_H, x2: VB_W, y2: VB_H }));
    // Sidelines
    g.appendChild(svgEl("line", { x1: 0, y1: 0, x2: 0, y2: VB_H }));
    g.appendChild(svgEl("line", { x1: VB_W, y1: 0, x2: VB_W, y2: VB_H }));

    // Paint (key) outline
    g.appendChild(svgEl("rect", { x: 170, y: 225, width: 160, height: 190, fill: "none" }));

    // Free-throw circle (top half only) — center at (250, 225), r=60
    var ftArc = svgEl("path", { d: "M 190 225 A 60 60 0 0 1 310 225", fill: "none" });
    g.appendChild(ftArc);
    // Bottom half dashed
    var ftArcD = svgEl("path", { d: "M 190 225 A 60 60 0 0 0 310 225", fill: "none", "stroke-dasharray": "4 4" });
    g.appendChild(ftArcD);

    // Restricted area arc
    g.appendChild(svgEl("path", { d: "M 210 415 A 40 40 0 0 1 290 415", fill: "none" }));

    // 3-point arc (from corner to corner)
    // Corner lines: x=30 from y=415 to y=350, x=470 same
    g.appendChild(svgEl("line", { x1: 30, y1: VB_H, x2: 30, y2: 350 }));
    g.appendChild(svgEl("line", { x1: 470, y1: VB_H, x2: 470, y2: 350 }));
    // Arc from (30,350) to (470,350) — large arc of r=237.5 centered at basket (250,415)
    g.appendChild(svgEl("path", { d: "M 30 350 A 237.5 237.5 0 0 1 470 350", fill: "none" }));

    // Backboard (at y=400, x=225..275)
    g.appendChild(svgEl("line", { x1: 220, y1: 400, x2: 280, y2: 400, "stroke-width": "3" }));

    // Basket (circle at basket center)
    g.appendChild(svgEl("circle", { cx: BASKET_X, cy: BASKET_Y, r: 10, fill: "none", "stroke-width": "2" }));

    svg.appendChild(g);
  }

  /* ── Zone rendering ────────────────────────────────────────────────────── */

  function buildZones(svg, zones, zoneMap) {
    var g = svgEl("g", { class: "sl-shotchart__zones" });

    ZONES.forEach(function (zoneName) {
      var pathD = ZONE_PATHS[zoneName];
      if (!pathD) return;
      var zd = zoneMap[zoneName];
      var color = zd ? zoneColor(zd.fg_pct, zd.pool_fg_pct) : NEUTRAL;

      var path = svgEl("path", {
        d: pathD,
        fill: color,
        class: "sl-shotchart__zone",
        "data-zone": zoneName,
        "fill-rule": "evenodd",
      });
      path.setAttribute("title", zoneName);
      g.appendChild(path);

      // Zone label
      var pos = ZONE_LABEL_POS[zoneName];
      if (pos && zd && zd.fga > 0) {
        var pct = zd.fg_pct !== null ? (zd.fg_pct * 100).toFixed(0) + "%" : "—";
        var line1 = svgEl("text", {
          x: pos.x,
          y: pos.y - 6,
          class: "sl-shotchart__zone-label",
        });
        line1.textContent = pct;
        g.appendChild(line1);

        var line2 = svgEl("text", {
          x: pos.x,
          y: pos.y + 7,
          class: "sl-shotchart__zone-label",
          style: "font-size:7px;opacity:0.8",
        });
        line2.textContent = zd.fga + " FGA";
        g.appendChild(line2);
      }
    });

    svg.appendChild(g);
    return g;
  }

  /* ── Dot rendering ─────────────────────────────────────────────────────── */
  // NBA loc_x/loc_y use tenths of feet from basket center; max range ≈ ±250x ±47.5y.
  // Our SVG basket is at (250, 415). Scale: 1 NBA unit = 1 SVG unit (both ~1ft).
  // So: svgX = 250 + loc_x/10, svgY = 415 - loc_y/10.
  // (loc_y is distance from basket toward half-court, so positive = away from basket = lower y in our coords)

  function buildDots(svg, dots) {
    if (!dots || dots.length === 0) return;
    var g = svgEl("g", { class: "sl-shotchart__dots" });

    dots.forEach(function (dot) {
      var sx = BASKET_X + dot.loc_x / 10;
      var sy = BASKET_Y - dot.loc_y / 10;
      // Clip to half-court view
      if (sx < 0 || sx > VB_W || sy < 0 || sy > VB_H) return;

      var r = 4;
      if (dot.made) {
        var c = svgEl("circle", {
          cx: sx, cy: sy, r: r,
          class: "sl-shotchart__dot sl-shotchart__dot--made",
        });
        g.appendChild(c);
      } else {
        // Miss: hollow circle (an X is also common but circle is simpler/cleaner)
        var c2 = svgEl("circle", {
          cx: sx, cy: sy, r: r,
          class: "sl-shotchart__dot sl-shotchart__dot--miss",
        });
        g.appendChild(c2);
      }
    });

    svg.appendChild(g);
  }

  /* ── Court SVG builder ─────────────────────────────────────────────────── */

  function buildCourtSVG(data, zoneMap) {
    var svg = svgEl("svg", {
      viewBox: "0 0 " + VB_W + " " + VB_H,
      "aria-label": "Shot chart half-court",
      role: "img",
    });

    buildCourtLines(svg);
    buildZones(svg, data.zones, zoneMap);
    buildDots(svg, data.dots);

    return svg;
  }

  /* ── Zone table ────────────────────────────────────────────────────────── */

  function buildTable(zoneMap) {
    var table = document.createElement("table");
    table.className = "sl-shotchart__table";

    var thead = document.createElement("thead");
    thead.innerHTML =
      "<tr>" +
      "<th>Zone</th>" +
      "<th>FGA</th>" +
      "<th>FG%</th>" +
      "<th>Freq%</th>" +
      "<th>vs Pool</th>" +
      "</tr>";
    table.appendChild(thead);

    var tbody = document.createElement("tbody");

    ZONES.forEach(function (zoneName) {
      var zd = zoneMap[zoneName];
      if (!zd || zd.fga === 0) return;

      var color = zoneColor(zd.fg_pct, zd.pool_fg_pct);
      var pct = zd.fg_pct !== null ? (zd.fg_pct * 100).toFixed(1) + "%" : "—";
      var freq = zd.freq_pct !== null ? (zd.freq_pct * 100).toFixed(1) + "%" : "—";

      var vsLabel = "—";
      var vsClass = "sl-shotchart__vs--neutral";
      if (zd.fg_pct !== null && zd.pool_fg_pct !== null) {
        var delta = (zd.fg_pct - zd.pool_fg_pct) * 100;
        var sign = delta >= 0 ? "+" : "";
        vsLabel = sign + delta.toFixed(1) + " pp";
        vsClass = delta >= 0.5 ? "sl-shotchart__vs--above" : delta <= -0.5 ? "sl-shotchart__vs--below" : "sl-shotchart__vs--neutral";
      } else if (zd.fg_pct !== null) {
        vsLabel = pct;
        vsClass = "sl-shotchart__vs--neutral";
      }

      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" +
          "<span class='sl-shotchart__swatch' style='background:" + color + "'></span>" +
          (ZONE_SHORT[zoneName] || zoneName) +
        "</td>" +
        "<td>" + zd.fga + "</td>" +
        "<td>" + pct + "</td>" +
        "<td>" + freq + "</td>" +
        "<td><span class='sl-shotchart__vs " + vsClass + "'>" + vsLabel + "</span></td>";
      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    return table;
  }

  /* ── Suppressed / placeholder ──────────────────────────────────────────── */

  function buildPlaceholder(totalFga) {
    var div = document.createElement("div");
    div.className = "sl-shotchart__placeholder";
    var icon = document.createElement("span");
    icon.className = "sl-shotchart__placeholder-icon";
    icon.textContent = "◎";
    div.appendChild(icon);
    var msg = document.createElement("p");
    if (totalFga === 0 || totalFga === null || totalFga === undefined) {
      msg.textContent = "No shot data available for this selection.";
    } else {
      msg.textContent =
        "Small sample (" + totalFga + " FGA). Zone chart not shown — " +
        "≥20 attempts required for reliable heat display.";
    }
    div.appendChild(msg);
    return div;
  }

  /* ── Main render ───────────────────────────────────────────────────────── */

  function render(root, data) {
    root.innerHTML = "";
    root.className = "sl-shotchart";

    var hasDots = data.dots && data.dots.length > 0;

    // Index zones by name for O(1) lookup.
    var zoneMap = {};
    (data.zones || []).forEach(function (z) {
      zoneMap[z.shot_zone_basic] = z;
    });

    // ── Header
    var header = document.createElement("div");
    header.className = "sl-shotchart__header";

    var title = document.createElement("h3");
    title.className = "sl-shotchart__title";
    title.textContent = "Shot Chart";
    header.appendChild(title);

    var fgaBadge = document.createElement("span");
    fgaBadge.className = "sl-shotchart__badge";
    fgaBadge.textContent = (data.total_fga || 0) + " FGA";
    header.appendChild(fgaBadge);

    if (data.suppressed) {
      var warnBadge = document.createElement("span");
      warnBadge.className = "sl-shotchart__badge sl-shotchart__badge--warn";
      warnBadge.textContent = "Small sample";
      header.appendChild(warnBadge);
    }

    var caveat = document.createElement("span");
    caveat.className = "sl-shotchart__caveat";
    caveat.textContent = "calibrated to SL pool";
    header.appendChild(caveat);

    root.appendChild(header);

    // ── Court or placeholder
    if (!data.suppressed) {
      var toggleBtn = null;
      if (hasDots) {
        toggleBtn = document.createElement("button");
        toggleBtn.className = "sl-shotchart__toggle";
        toggleBtn.type = "button";
        toggleBtn.innerHTML = "&#9632; Dots";
        toggleBtn.setAttribute("aria-pressed", "false");
        header.insertBefore(toggleBtn, caveat);
      }

      var wrap = document.createElement("div");
      wrap.className = "sl-shotchart__court-wrap";
      var svg = buildCourtSVG(data, zoneMap);
      wrap.appendChild(svg);
      root.appendChild(wrap);

      if (toggleBtn) {
        toggleBtn.addEventListener("click", function () {
          var on = root.classList.toggle("sl-shotchart--dots");
          toggleBtn.setAttribute("aria-pressed", on ? "true" : "false");
        });
      }
    } else {
      root.appendChild(buildPlaceholder(data.total_fga));
    }

    // ── Zone table always shown
    if (data.zones && data.zones.length > 0) {
      root.appendChild(buildTable(zoneMap));
    }
  }

  /* ── Boot ───────────────────────────────────────────────────────────────── */

  function init() {
    var root = document.getElementById("sl-shotchart-root");
    if (!root) return;

    var data = window.SL_SHOTCHART;
    if (!data) {
      root.textContent = "Shot chart data unavailable.";
      return;
    }

    render(root, data);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
}());
