/* ==========================================================================
   consensus-trajectories.js — Player rank trajectories JS for /consensus
   Owned by ticket #276. Consolidated into consensus.js by ticket #278.

   Responsibilities:
   - Highlight the hovered line in the trajectories chart, dimming others.
   - Sync legend item highlight with the hovered polyline.
   ========================================================================== */

(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    var section = document.getElementById("consensusTrajectoriesSection");
    if (!section) return;

    var lines = section.querySelectorAll(".traj__line");
    var dots = section.querySelectorAll(".traj__dot");
    var legendItems = section.querySelectorAll(".traj__legend-item");

    if (!lines.length) return;

    /**
     * Dim all lines/dots to the given opacity, except the one at `activeIdx`.
     * Pass -1 to restore full opacity for all.
     *
     * @param {number} activeIdx - Index of the focused line, or -1 to reset.
     */
    function setFocus(activeIdx) {
      lines.forEach(function (line, i) {
        line.style.opacity = activeIdx === -1 || i === activeIdx ? "1" : "0.2";
      });
      dots.forEach(function (dot, i) {
        dot.style.opacity = activeIdx === -1 || i === activeIdx ? "1" : "0.2";
      });
      legendItems.forEach(function (item, i) {
        if (activeIdx === -1) {
          item.style.opacity = "1";
          item.style.fontWeight = "";
        } else if (i === activeIdx) {
          item.style.opacity = "1";
          item.style.fontWeight = "700";
        } else {
          item.style.opacity = "0.4";
          item.style.fontWeight = "";
        }
      });
    }

    lines.forEach(function (line, i) {
      line.addEventListener("mouseenter", function () {
        setFocus(i);
      });
      line.addEventListener("mouseleave", function () {
        setFocus(-1);
      });
      line.addEventListener("focusin", function () {
        setFocus(i);
      });
      line.addEventListener("focusout", function () {
        setFocus(-1);
      });
    });

    legendItems.forEach(function (item, i) {
      item.addEventListener("mouseenter", function () {
        setFocus(i);
      });
      item.addEventListener("mouseleave", function () {
        setFocus(-1);
      });
    });
  });
})();
