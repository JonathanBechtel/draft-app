/* summer-league-shotchart.js
 *
 * Continuous (kernel-smoothed) shot heat map from window.SL_SHOTCHART.
 *   • A Gaussian kernel is sampled over a fine grid, so color varies SMOOTHLY
 *     across the floor instead of jumping bin-to-bin.
 *   • COLOR  = shooting efficiency — vs the SL pool for that area when a pool
 *     baseline exists (red below → green above), else a sequential FG% scale.
 *   • OPACITY/INTENSITY = where the shots actually are (density), so empty floor
 *     stays clear and hot spots glow.
 *   • Toggle to raw makes/misses dots. Hover for a local readout.
 *   • <20 FGA or no data → graceful placeholder + table only.
 *
 * Data contract (window.SL_SHOTCHART):
 *   total_fga, suppressed,
 *   zones : [ { shot_zone_basic, fga, fgm, fg_pct, freq_pct, pool_fg_pct } ],
 *   dots  : [ { loc_x, loc_y, made } ]   (NBA tenths-of-feet; hoop at 0,0)
 */
(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";

  /* Court coords: 1 unit = 1 tenth-foot. Hoop at (250, 418); baseline y=470.
   * We crop the view to y ∈ [95, 470] (baseline → just above the arc).        */
  var VB_W = 500, VB_H = 470, HOOP_X = 250, HOOP_Y = 418, BASELINE_Y = 470;
  var CROP_TOP = 95, CROP_H = VB_H - CROP_TOP; // 375
  function sx(x) { return HOOP_X + x; }
  function sy(y) { return HOOP_Y - y; }

  var ZONES = ["Restricted Area","In The Paint (Non-RA)","Mid-Range","Left Corner 3","Right Corner 3","Above the Break 3"];
  var ZONE_SHORT = { "Restricted Area":"RA","In The Paint (Non-RA)":"Paint","Mid-Range":"Mid","Left Corner 3":"LC3","Right Corner 3":"RC3","Above the Break 3":"ATB 3" };

  function classifyZone(x, y) {
    var dist = Math.sqrt(x * x + y * y);
    if (Math.abs(x) >= 220 && y <= 92) return x < 0 ? "Left Corner 3" : "Right Corner 3";
    if (dist >= 237.5) return "Above the Break 3";
    if (dist <= 40) return "Restricted Area";
    if (Math.abs(x) <= 80 && y <= 190) return "In The Paint (Non-RA)";
    return "Mid-Range";
  }

  /* ── Colour ramps (return [r,g,b]) ──────────────────────────────────────── */
  function mix(a, b, t) { return [Math.round(a[0]+(b[0]-a[0])*t), Math.round(a[1]+(b[1]-a[1])*t), Math.round(a[2]+(b[2]-a[2])*t)]; }
  var C_COLD = [225,29,72], C_MID = [238,232,220], C_HOT = [16,185,129];          // diverging vs pool (cream midpoint → no amber band)
  var S0 = [37,99,235], S1 = [56,189,248], S2 = [250,204,21], S3 = [239,68,68];  // sequential FG% (blue→cyan→amber→red)
  var MAX_DELTA = 0.10;
  function divColor(fg, pool) {
    var t = Math.max(-1, Math.min(1, (fg - pool) / MAX_DELTA));
    return t >= 0 ? mix(C_MID, C_HOT, t) : mix(C_MID, C_COLD, -t);
  }
  function seqColor(fg) {
    var t = Math.max(0, Math.min(1, fg / 0.65));
    if (t < 0.34) return mix(S0, S1, t / 0.34);
    if (t < 0.67) return mix(S1, S2, (t - 0.34) / 0.33);
    return mix(S2, S3, (t - 0.67) / 0.33);
  }
  function rgbStr(c, a) { return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")"; }

  function svgEl(tag, attrs) {
    var el = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    return el;
  }

  /* ── Court lines (SVG overlay above the heat canvas) ───────────────────── */
  function buildCourtSVG() {
    var svg = svgEl("svg", { viewBox: "0 " + CROP_TOP + " " + VB_W + " " + CROP_H, class: "sl-shotchart__court", role: "img", "aria-label": "Shot heat map" });
    var line = function (a) { a.class = "sl-shotchart__line"; svg.appendChild(svgEl("line", a)); };
    var path = function (d, dash) { var o = { d: d, class: "sl-shotchart__line", fill: "none" }; if (dash) o["stroke-dasharray"] = "5 5"; svg.appendChild(svgEl("path", o)); };
    line({ x1: 0, y1: BASELINE_Y, x2: VB_W, y2: BASELINE_Y });
    line({ x1: 0, y1: CROP_TOP, x2: 0, y2: BASELINE_Y });
    line({ x1: VB_W, y1: CROP_TOP, x2: VB_W, y2: BASELINE_Y });
    var FT_Y = sy(190);
    svg.appendChild(svgEl("rect", { x: 170, y: FT_Y, width: 160, height: BASELINE_Y - FT_Y, fill: "none", class: "sl-shotchart__line" }));
    path("M 190 " + FT_Y + " A 60 60 0 0 1 310 " + FT_Y);
    path("M 190 " + FT_Y + " A 60 60 0 0 0 310 " + FT_Y, true);
    path("M " + sx(-40) + " " + sy(0) + " A 40 40 0 0 1 " + sx(40) + " " + sy(0));
    line({ x1: sx(-30), y1: sy(-12), x2: sx(30), y2: sy(-12) });
    svg.appendChild(svgEl("circle", { cx: HOOP_X, cy: sy(0), r: 7.5, fill: "none", class: "sl-shotchart__line" }));
    var cY = sy(90);
    line({ x1: sx(-220), y1: BASELINE_Y, x2: sx(-220), y2: cY });
    line({ x1: sx(220), y1: BASELINE_Y, x2: sx(220), y2: cY });
    path("M " + sx(-220) + " " + cY + " A 237.5 237.5 0 0 1 " + sx(220) + " " + cY);
    return svg;
  }

  /* ── Kernel-smoothed heat onto a canvas ─────────────────────────────────── */
  var SIGMA = 28;       // kernel bandwidth in court units (~2.8 ft)
  var GW = 200, GH = Math.round(GW * CROP_H / VB_W); // offscreen grid res

  function drawHeat(canvas, dots, zoneMap, hasPool) {
    var shots = dots.filter(function (d) { return d.loc_y <= 405 && d.loc_y >= -55; });
    if (!shots.length) return;
    // Precompute display-space shot positions (court coords).
    var pts = shots.map(function (d) { return { x: sx(d.loc_x), y: sy(d.loc_y), m: d.made ? 1 : 0 }; });

    var off = document.createElement("canvas");
    off.width = GW; off.height = GH;
    var octx = off.getContext("2d");
    var img = octx.createImageData(GW, GH);
    var data = img.data;
    var twoSig2 = 2 * SIGMA * SIGMA;
    var cell_x = VB_W / GW, cell_y = CROP_H / GH;

    // First pass: weight + weighted makes per cell; track max weight for normalisation.
    var W = new Float32Array(GW * GH), M = new Float32Array(GW * GH), maxW = 0;
    for (var gy = 0; gy < GH; gy++) {
      var cy = CROP_TOP + (gy + 0.5) * cell_y;
      for (var gx = 0; gx < GW; gx++) {
        var cx = (gx + 0.5) * cell_x;
        var w = 0, wm = 0;
        for (var i = 0; i < pts.length; i++) {
          var dx = cx - pts[i].x, dy = cy - pts[i].y;
          var d2 = dx * dx + dy * dy;
          if (d2 > twoSig2 * 4) continue;       // skip far shots (>~2.8σ)
          var e = Math.exp(-d2 / twoSig2);
          w += e; wm += e * pts[i].m;
        }
        var idx = gy * GW + gx;
        W[idx] = w; M[idx] = wm;
        if (w > maxW) maxW = w;
      }
    }
    // Second pass: colour by efficiency, alpha by density.
    for (var p = 0; p < GW * GH; p++) {
      var wgt = W[p];
      var di = p * 4;
      if (wgt < maxW * 0.05) { data[di + 3] = 0; continue; } // clear empty floor
      var eff = M[p] / wgt;
      var gx2 = p % GW, gy2 = (p / GW) | 0;
      var col;
      if (hasPool) {
        var lx = (gx2 + 0.5) * cell_x - HOOP_X;
        var ly = HOOP_Y - (CROP_TOP + (gy2 + 0.5) * cell_y);
        var zd = zoneMap[classifyZone(lx, ly)];
        var pool = zd && zd.pool_fg_pct != null ? zd.pool_fg_pct : null;
        col = pool != null ? divColor(eff, pool) : seqColor(eff);
      } else {
        col = seqColor(eff);
      }
      var intensity = Math.pow(wgt / maxW, 0.6);
      data[di] = col[0]; data[di + 1] = col[1]; data[di + 2] = col[2];
      data[di + 3] = Math.round(Math.min(0.92, 0.18 + 0.78 * intensity) * 255);
    }
    octx.putImageData(img, 0, 0);

    // Scale the small grid up onto the display canvas → bilinear smoothing.
    var ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(off, 0, 0, canvas.width, canvas.height);
  }

  var DOT_R = 8;         // heat-mode toggle dots
  var DOT_R_LARGE = 10;  // dots-only (per-player) view — legible standalone

  function buildDots(svg, dots, radius) {
    var r = radius || DOT_R;
    var g = svgEl("g", { class: "sl-shotchart__dots" });
    dots.forEach(function (d) {
      if (d.loc_y > 405 || d.loc_y < -55) return;
      g.appendChild(svgEl("circle", { cx: sx(d.loc_x), cy: sy(d.loc_y), r: r, class: "sl-shotchart__dot " + (d.made ? "sl-shotchart__dot--made" : "sl-shotchart__dot--miss") }));
    });
    svg.appendChild(g);
  }

  /* Zone breakdown table (client-rendered for the game page, which swaps scopes
   * without a round-trip). Matches the server-rendered table on other pages. */
  function renderZoneTable(container, zones) {
    if (!container) return;
    if (!zones || !zones.length) { container.innerHTML = ""; return; }
    function pct(v) { return v == null ? "—" : (v * 100).toFixed(1) + "%"; }
    var body = zones.map(function (z) {
      return "<tr><th scope='row'>" + z.shot_zone_basic + "</th>" +
        "<td>" + z.fga + "</td><td>" + z.fgm + "</td>" +
        "<td>" + pct(z.fg_pct) + "</td><td>" + pct(z.freq_pct) + "</td>" +
        "<td>" + pct(z.pool_fg_pct) + "</td></tr>";
    }).join("");
    container.innerHTML =
      "<table class='sl-stats-table'><thead><tr>" +
      "<th scope='col'>Zone</th><th scope='col' title='Field goals attempted'>FGA</th>" +
      "<th scope='col' title='Field goals made'>FGM</th>" +
      "<th scope='col' title='Field goal percentage'>FG%</th>" +
      "<th scope='col' title='Frequency — share of total FGA'>Freq%</th>" +
      "<th scope='col' title='Pool average FG% for this zone'>Pool%</th>" +
      "</tr></thead><tbody>" + body + "</tbody></table>";
  }

  /* ── Legend / table / placeholder ───────────────────────────────────────── */
  function buildLegend(hasPool) {
    var l = document.createElement("div");
    l.className = "sl-shotchart__legend";
    var grad = hasPool
      ? "<span class='sl-shotchart__grad sl-shotchart__grad--div'></span> efficiency vs SL pool (red below · green above)"
      : "<span class='sl-shotchart__grad sl-shotchart__grad--seq'></span> FG% (cool → hot)";
    l.innerHTML =
      "<span class='sl-shotchart__legend-item'><span class='sl-shotchart__fade'></span> shot density</span>" +
      "<span class='sl-shotchart__legend-item'>" + grad + "</span>";
    return l;
  }

  function buildPlaceholder(total) {
    var div = document.createElement("div");
    div.className = "sl-shotchart__placeholder";
    div.innerHTML = "<span class='sl-shotchart__placeholder-icon'>◎</span><p>" +
      (!total ? "No shot data available for this selection." : "Small sample (" + total + " FGA). Heat map hidden — ≥20 attempts needed.") + "</p>";
    return div;
  }

  /* ── Render ─────────────────────────────────────────────────────────────── */
  /* opts.dotsOnly → plot raw makes/misses only, no heat (used for a single
   * player, where a smoothed density map over a handful of shots is noise).
   * A suppressed sample (total FGA below the heat floor) degrades to the same
   * dots-only view instead of hiding the chart — matching the game page. */
  function render(root, data, opts) {
    opts = opts || {};
    var hasDots = data.dots && data.dots.length > 0;
    var dotsOnly = !!opts.dotsOnly || (data.suppressed && hasDots);
    root.innerHTML = "";
    root.className = "sl-shotchart";
    var zoneMap = {};
    (data.zones || []).forEach(function (z) { zoneMap[z.shot_zone_basic] = z; });
    var hasPool = (data.zones || []).some(function (z) { return z.pool_fg_pct != null; });

    var header = document.createElement("div");
    header.className = "sl-shotchart__header";
    header.innerHTML = "<h3 class='sl-shotchart__title'>Shot Chart</h3>" +
      "<span class='sl-shotchart__badge'>" + (data.total_fga || 0) + " FGA</span>" +
      (data.suppressed ? "<span class='sl-shotchart__badge sl-shotchart__badge--warn'>Small sample</span>" : "");
    root.appendChild(header);

    // Dots-only needs ≥1 plotted shot; heat mode also needs a large-enough sample.
    var canDraw = hasDots && (dotsOnly || !data.suppressed);
    if (!canDraw) {
      root.appendChild(buildPlaceholder(dotsOnly && !hasDots ? 0 : data.total_fga));
      return;
    }

    var wrap = document.createElement("div");
    wrap.className = "sl-shotchart__court-wrap";
    var svg = buildCourtSVG();

    if (dotsOnly) {
      root.classList.add("sl-shotchart--dots"); // reveal dots, hide (absent) heat
      buildDots(svg, data.dots, DOT_R_LARGE);
      wrap.appendChild(svg);
      root.appendChild(wrap);
      return;
    }

    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "sl-shotchart__toggle";
    toggle.textContent = "Show shots";
    header.appendChild(toggle);

    var canvas = document.createElement("canvas");
    canvas.className = "sl-shotchart__heat";
    canvas.width = VB_W * 2; canvas.height = CROP_H * 2; // hi-dpi backing store
    wrap.appendChild(canvas);
    buildDots(svg, data.dots, DOT_R);
    wrap.appendChild(svg);
    root.appendChild(wrap);
    root.appendChild(buildLegend(hasPool));

    drawHeat(canvas, data.dots, zoneMap, hasPool);

    toggle.addEventListener("click", function () {
      var on = root.classList.toggle("sl-shotchart--dots");
      toggle.textContent = on ? "Show heat" : "Show shots";
      toggle.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  // Public API for pages that drive the chart themselves (e.g. the game page,
  // which swaps scopes client-side). Classic global — no ES modules.
  window.SLShotChart = { render: render, renderZoneTable: renderZoneTable };

  function init() {
    var root = document.getElementById("sl-shotchart-root");
    if (!root) return;
    // The game page owns its own controller (multiple scopes); skip auto-init.
    if (window.SL_SHOTCHART_SCOPES) return;
    var data = window.SL_SHOTCHART;
    if (!data) { root.textContent = "Shot chart data unavailable."; return; }
    render(root, data);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
}());
