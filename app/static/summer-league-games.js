/* Summer League games store — client interactions.
   - Index: auto-submit filter selects; whole row click-through to the box score.
   - Box score: traditional / advanced column-set toggle.
   Page-scoped, initialized on DOMContentLoaded. */

(function () {
  "use strict";

  function initFilters() {
    var form = document.getElementById("slgFilters");
    if (!form) return;
    form.querySelectorAll("[data-autosubmit]").forEach(function (el) {
      // Submitting omits `page`, which the route defaults to 1 — so a filter
      // change naturally resets pagination.
      el.addEventListener("change", function () {
        // Drop empty-valued controls (e.g. the "All-time" year option) so they
        // are omitted from the query string rather than submitted as `year=`,
        // which would fail the route's `int | None` validation.
        form.querySelectorAll("[name]").forEach(function (field) {
          field.disabled = field.value === "";
        });
        form.submit();
      });
    });
  }

  function initRowLinks() {
    document.querySelectorAll(".slg-row[data-href]").forEach(function (row) {
      row.addEventListener("click", function (e) {
        // Let real links/buttons handle their own clicks.
        if (e.target.closest("a")) return;
        window.location.href = row.getAttribute("data-href");
      });
    });
  }

  function initBoxModeToggle() {
    var btns = document.querySelectorAll(".slg-mode-btn");
    if (!btns.length) return;
    btns.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var mode = btn.getAttribute("data-slg-mode");
        btns.forEach(function (b) {
          b.classList.toggle("active", b.getAttribute("data-slg-mode") === mode);
        });
        document.querySelectorAll(".slg-mode-data").forEach(function (panel) {
          panel.style.display =
            panel.getAttribute("data-slg-mode") === mode ? "" : "none";
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initFilters();
    initRowLinks();
    initBoxModeToggle();
  });
})();
