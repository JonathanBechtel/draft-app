/* Scope-parameterized cumulative trend chart; no framework or build step. */
(function () {
  "use strict";

  const COLORS = { gmsc: "#2563eb", ts_pct: "#d97706", bpm: "#e11d48" };
  const NS = "http://www.w3.org/2000/svg";
  const chartBox = { left: 48, right: 742, top: 22, bottom: 300 };

  function svgNode(name, attrs) {
    const node = document.createElementNS(NS, name);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  }

  function formatValue(metric, value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    const number = Number(value);
    if (metric === "ts_pct" && Math.abs(number) <= 1) return `${(number * 100).toFixed(1)}%`;
    return `${number >= 0 && metric === "bpm" ? "+" : ""}${number.toFixed(1)}`;
  }

  function draw(root, payload, muted) {
    const svg = root.querySelector("[data-trend-chart]");
    if (!svg) return;
    svg.replaceChildren();
    const points = Array.isArray(payload.points) ? payload.points : [];
    const days = [...new Set(points.map((point) => point.effective_day))].sort();
    const width = chartBox.right - chartBox.left;
    const xFor = (day) => chartBox.left + (days.length <= 1 ? width / 2 : (days.indexOf(day) / (days.length - 1)) * width);

    svg.appendChild(svgNode("rect", { x: 0, y: 0, width: 760, height: 330, rx: 6, fill: "transparent" }));
    [0, 1, 2].forEach((lane) => {
      const y = chartBox.top + lane * 92;
      svg.appendChild(svgNode("line", { x1: chartBox.left, y1: y + 52, x2: chartBox.right, y2: y + 52, stroke: "currentColor", "stroke-opacity": ".12" }));
    });

    (payload.metrics || []).forEach((metric, lane) => {
      const key = metric.key;
      if (muted[key]) return;
      const metricPoints = points.filter((point) => point.metric_key === key);
      if (!metricPoints.length) return;
      const numbers = metricPoints.flatMap((point) => [point.value, point.cohort_band && point.cohort_band.q1, point.cohort_band && point.cohort_band.q3]).filter((value) => value !== null && value !== undefined).map(Number);
      let min = Math.min(...numbers);
      let max = Math.max(...numbers);
      if (min === max) { min -= 1; max += 1; }
      const pad = (max - min) * .12;
      min -= pad; max += pad;
      const laneTop = chartBox.top + lane * 92;
      const yFor = (value) => laneTop + 70 - ((Number(value) - min) / (max - min)) * 62;
      const color = COLORS[key] || "#0891b2";
      const bandPath = metricPoints.map((point, index) => `${index ? "L" : "M"}${xFor(point.effective_day)},${yFor(point.cohort_band.q3)}`).join(" ") + " " + metricPoints.slice().reverse().map((point) => `L${xFor(point.effective_day)},${yFor(point.cohort_band.q1)}`).join(" ") + " Z";
      svg.appendChild(svgNode("path", { d: bandPath, class: "trend-card__band", fill: color, "fill-opacity": ".15", stroke: "none", "data-trend-band": key }));
      const linePath = metricPoints.map((point, index) => `${index ? "L" : "M"}${xFor(point.effective_day)},${yFor(point.value)}`).join(" ");
      svg.appendChild(svgNode("path", { d: linePath, class: "trend-card__line", fill: "none", stroke: color, "stroke-width": 3, "stroke-linecap": "round", "stroke-linejoin": "round", "data-trend-line": key }));
      const label = svgNode("text", { x: 5, y: laneTop + 20, fill: color, "font-family": "Azeret Mono, monospace", "font-size": 13, "font-weight": 600 });
      label.textContent = metric.label;
      svg.appendChild(label);
      metricPoints.forEach((point) => {
        const circle = svgNode("circle", { cx: xFor(point.effective_day), cy: yFor(point.value), r: 5, class: "trend-card__point", fill: color, stroke: "white", "stroke-width": 2, "data-trend-point": "true", tabindex: 0 });
        circle.dataset.metric = key;
        circle.dataset.day = point.effective_day;
        circle.dataset.value = formatValue(key, point.value);
        svg.appendChild(circle);
      });
    });
    days.forEach((day) => {
      const label = svgNode("text", { x: xFor(day), y: 322, "text-anchor": "middle", fill: "currentColor", "fill-opacity": ".6", "font-family": "Azeret Mono, monospace", "font-size": 11 });
      label.textContent = day.slice(5);
      svg.appendChild(label);
    });
  }

  async function exportTrend(root, payload) {
    const playerId = Number(root.dataset.trendPlayerId || payload.player_id || 0);
    if (!playerId) return;
    const button = root.querySelector("[data-trend-share]");
    if (button) button.disabled = true;
    try {
      const response = await fetch("/api/export/image", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ component: "sl_trend", player_ids: [playerId], context: { scope_key: payload.scope_key, metric_keys: payload.metric_keys }, redirect_path: window.location.pathname }) });
      if (!response.ok) throw new Error(`trend export failed (${response.status})`);
      const result = await response.json();
      const link = document.createElement("a");
      link.href = result.url;
      link.download = result.filename || "trend.png";
      link.target = "_blank";
      link.rel = "noopener";
      link.click();
    } catch (error) {
      console.error(error);
    } finally {
      if (button) button.disabled = false;
    }
  }

  function init(root) {
    const payloadScript = root.querySelector("[data-trend-payload]");
    if (!payloadScript) return;
    let payload;
    try { payload = JSON.parse(payloadScript.textContent || "{}"); } catch (error) { console.error("Invalid trend payload", error); return; }
    const muted = {};
    draw(root, payload, muted);
    root.querySelectorAll("[data-trend-metric]").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.dataset.trendMetric;
        muted[key] = !muted[key];
        button.classList.toggle("is-muted", muted[key]);
        draw(root, payload, muted);
      });
    });
    const tooltip = root.querySelector("[data-trend-tooltip]");
    root.querySelector("[data-trend-chart]")?.addEventListener("click", (event) => {
      const point = event.target.closest("[data-trend-point]");
      if (point && tooltip) tooltip.textContent = `${point.dataset.metric.toUpperCase()} · ${point.dataset.day} · ${point.dataset.value}`;
    });
    root.querySelector("[data-trend-share]")?.addEventListener("click", () => exportTrend(root, payload));
  }

  function boot() { document.querySelectorAll("[data-trend-root]").forEach(init); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
