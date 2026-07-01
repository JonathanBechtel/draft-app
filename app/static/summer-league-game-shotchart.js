/* summer-league-game-shotchart.js
 *
 * Client-side controller for the game box-score shot chart. Every scope
 * (whole game / each team / each player) is preloaded into
 * window.SL_SHOTCHART_SCOPES, so switching is instant — no server round-trip and
 * no scroll jump. Per-player scopes render raw dots only (no heat map).
 *
 * Data contract (window.SL_SHOTCHART_SCOPES):
 *   { scopes: { "game": payload, "team:<id>": payload, "player:<id>": payload } }
 *   where each payload matches the window.SL_SHOTCHART shape.
 *
 * Selector contract (in the template):
 *   .slg-shotchart-selector
 *     .slg-shotchart-teamrow           → buttons: data-scope-key="game" | "team:<id>"
 *     .slg-shotchart-player-selector[data-team-key="team:<id>"]  (hidden by default)
 *       → buttons: data-scope-key="team:<id>" (All Players) | "player:<id>"
 */
(function () {
  "use strict";

  var payload = window.SL_SHOTCHART_SCOPES;
  var root = document.getElementById("sl-shotchart-root");
  if (!payload || !payload.scopes || !root || !window.SLShotChart) return;

  var scopes = payload.scopes;
  var selector = document.querySelector(".slg-shotchart-selector");
  var tableEl = document.getElementById("sl-shotchart-zone-table");
  var noteEl = document.getElementById("sl-shotchart-note");
  var EMPTY = { total_fga: 0, suppressed: true, zones: [], dots: [] };

  function renderScope(key) {
    var data = scopes[key] || EMPTY;
    var dotsOnly = key.indexOf("player:") === 0;
    window.SLShotChart.render(root, data, { dotsOnly: dotsOnly });
    window.SLShotChart.renderZoneTable(tableEl, data.zones || []);
    if (noteEl) {
      if (!dotsOnly && data.suppressed && data.total_fga > 0) {
        noteEl.textContent =
          "Small sample (" + data.total_fga +
          " FGA). Heat map hidden — ≥20 attempts needed; zone table below.";
        noteEl.style.display = "";
      } else {
        noteEl.style.display = "none";
      }
    }
  }

  function teamKeyForScope(key) {
    if (key === "game") return "game";
    if (key.indexOf("team:") === 0) return key;
    // player:<id> → the team whose sub-selector holds this button.
    var btn = selector.querySelector(
      '.slg-shotchart-player-selector [data-scope-key="' + key + '"]'
    );
    var sub = btn && btn.closest(".slg-shotchart-player-selector");
    return sub ? sub.getAttribute("data-team-key") : "game";
  }

  function setActive(key) {
    selector.querySelectorAll(".slg-shotchart-btn").forEach(function (b) {
      b.classList.remove("active");
    });
    var teamKey = teamKeyForScope(key);

    // Highlight the matching team-row button (Whole Game / Team).
    var topBtn = selector.querySelector(
      '.slg-shotchart-teamrow [data-scope-key="' + teamKey + '"]'
    );
    if (topBtn) topBtn.classList.add("active");

    // Show only the active team's player sub-selector.
    var subs = selector.querySelectorAll(".slg-shotchart-player-selector");
    subs.forEach(function (el) {
      var match = teamKey !== "game" && el.getAttribute("data-team-key") === teamKey;
      el.style.display = match ? "" : "none";
    });

    // Highlight the active button within the sub-selector (All Players / player).
    if (teamKey !== "game") {
      var sub = selector.querySelector(
        '.slg-shotchart-player-selector[data-team-key="' + teamKey + '"]'
      );
      var active = sub && sub.querySelector('[data-scope-key="' + key + '"]');
      if (active) active.classList.add("active");
    }
  }

  function select(key) {
    setActive(key);
    renderScope(key);
  }

  if (selector) {
    selector.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-scope-key]");
      if (!btn || !selector.contains(btn)) return;
      e.preventDefault();
      select(btn.getAttribute("data-scope-key"));
    });
  }

  select("game");
}());
