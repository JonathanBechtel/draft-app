/**
 * sources.js — Leaderboard sort + row-click navigation for /sources pages.
 *
 * Features:
 *   - Sortable columns (contrarian score, avg deviation) with toggling asc/desc.
 *   - Click anywhere on a leaderboard row to navigate to the source detail page.
 *   - Keyboard navigation: Enter/Space on focused rows triggers navigation.
 */

(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // Row click navigation (leaderboard)
  // -------------------------------------------------------------------------

  function initRowNavigation() {
    var rows = document.querySelectorAll(
      ".sources-leaderboard__row[data-href]"
    );
    rows.forEach(function (row) {
      row.addEventListener("click", function (e) {
        // Allow clicking the player link inside the outlier cell without
        // navigating away from the leaderboard row's own href.
        if (e.target && e.target.closest("a")) return;
        var href = row.getAttribute("data-href");
        if (href) window.location.href = href;
      });
      row.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          var href = row.getAttribute("data-href");
          if (href) window.location.href = href;
        }
      });
    });
  }

  // -------------------------------------------------------------------------
  // Column sort (leaderboard table)
  // -------------------------------------------------------------------------

  /**
   * Sort the leaderboard tbody rows by a numeric data attribute.
   *
   * @param {string} col  - data-col value ("contrarian_score" | "avg_deviation")
   * @param {boolean} asc - sort ascending when true, descending when false
   */
  function sortLeaderboard(col, asc) {
    var tbody = document.getElementById("sourcesLeaderboardBody");
    if (!tbody) return;

    var attrMap = {
      contrarian_score: "data-contrarian-score",
      avg_deviation: "data-avg-deviation",
    };
    var attr = attrMap[col];
    if (!attr) return;

    var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
    rows.sort(function (a, b) {
      var av = parseFloat(a.getAttribute(attr) || "0");
      var bv = parseFloat(b.getAttribute(attr) || "0");
      return asc ? av - bv : bv - av;
    });
    rows.forEach(function (row) {
      tbody.appendChild(row);
    });
  }

  function initTableSort() {
    var headers = document.querySelectorAll(
      ".sources-leaderboard__th.sortable[data-col]"
    );
    if (!headers.length) return;

    // Track current sort state: {col, asc}
    var current = { col: "contrarian_score", asc: false };

    headers.forEach(function (th) {
      th.addEventListener("click", function () {
        var col = th.getAttribute("data-col");
        var asc;
        if (col === current.col) {
          asc = !current.asc;
        } else {
          // Default sort direction: contrarian/deviation descending first.
          asc = false;
        }
        current = { col: col, asc: asc };

        // Update aria-sort on all sortable headers.
        headers.forEach(function (h) {
          var indicator = h.querySelector(".sort-indicator");
          if (h === th) {
            h.setAttribute("aria-sort", asc ? "ascending" : "descending");
            if (indicator) indicator.textContent = asc ? "▲" : "▼";
          } else {
            h.setAttribute("aria-sort", "none");
            if (indicator) indicator.textContent = "";
          }
        });

        sortLeaderboard(col, asc);
      });
    });
  }

  // -------------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    initRowNavigation();
    initTableSort();
  });
})();
