/* ==========================================================================
   consensus-scatter.js — Agreement scatter + source picker JS for /consensus
   Owned by ticket #273. Consolidated into consensus.js by ticket #278.

   Reads source overlay data from #scatterOverlayData (JSON embedded by the
   template), then wires the source picker to swap the active SVG dots and
   update the caption link when a different source is selected.
   ========================================================================== */

(function initConsensusScatter() {
  "use strict";

  // SVG coordinate space constants (must match scatter.html viewBox = 260×260)
  const SVG_X_MIN = 40;   // left axis x
  const SVG_X_MAX = 250;  // right edge of plot area
  const SVG_Y_MIN = 10;   // top of plot area
  const SVG_Y_MAX = 230;  // bottom axis y (origin for rank 1)
  const NS = "http://www.w3.org/2000/svg";

  /**
   * Map a rank value to an SVG x-coordinate (consensus rank → x axis).
   * Rank 1 maps to the left end; higher ranks map rightward.
   *
   * @param {number} rank - The rank (1-based).
   * @param {number} maxRank - The maximum rank value across all dots for this source.
   * @returns {number} SVG x coordinate.
   */
  function rankToX(rank, maxRank) {
    if (maxRank <= 1) return SVG_X_MIN + 10;
    return SVG_X_MIN + ((rank - 1) / (maxRank - 1)) * (SVG_X_MAX - SVG_X_MIN);
  }

  /**
   * Map a rank value to an SVG y-coordinate (source rank → y axis).
   * Rank 1 maps to the top (small y); higher ranks map downward.
   *
   * @param {number} rank - The rank (1-based).
   * @param {number} maxRank - The maximum rank value across all dots for this source.
   * @returns {number} SVG y coordinate.
   */
  function rankToY(rank, maxRank) {
    if (maxRank <= 1) return SVG_Y_MAX - 10;
    return SVG_Y_MAX - ((rank - 1) / (maxRank - 1)) * (SVG_Y_MAX - SVG_Y_MIN);
  }

  /**
   * Render dots for a single source overlay into the #scatterDots group.
   *
   * @param {Object} overlay - One entry from the source_overlays array.
   */
  function renderDots(overlay) {
    const dotsGroup = document.getElementById("scatterDots");
    if (!dotsGroup) return;

    // Clear existing dots
    while (dotsGroup.firstChild) {
      dotsGroup.removeChild(dotsGroup.firstChild);
    }

    const rows = overlay.overlay_rows || [];
    if (rows.length === 0) return;

    // Compute the max rank across this source's rows for scaling
    const maxSourceRank = Math.max(...rows.map((r) => r.source_rank || 0), 1);
    const maxConsensusRank = Math.max(...rows.map((r) => r.consensus_rank || 0), 1);
    const maxRank = Math.max(maxSourceRank, maxConsensusRank);

    rows.forEach((row) => {
      if (row.source_rank == null || row.consensus_rank == null) return;

      const cx = rankToX(row.consensus_rank, maxRank);
      const cy = rankToY(row.source_rank, maxRank);
      const isBold = row.is_biggest_outlier === true;

      // Determine "bold" status from is_biggest_outlier OR a large absolute delta
      const delta = row.delta != null ? Math.abs(row.delta) : 0;
      const isContrarian = isBold || delta >= 5;

      const circle = document.createElementNS(NS, "circle");
      circle.setAttribute("cx", cx.toFixed(1));
      circle.setAttribute("cy", cy.toFixed(1));
      circle.setAttribute("r", isContrarian ? "5" : "4");
      circle.classList.add("scatter__dot");
      if (isContrarian) {
        circle.classList.add("scatter__dot--bold");
      }

      // Native SVG tooltip (accessible, works without hover JS)
      const playerName = row.player_name || "Unknown";
      const sourceRank = row.source_rank;
      const consensusRank = row.consensus_rank;
      const label =
        `${playerName} — their #${sourceRank} · consensus #${consensusRank}` +
        (isContrarian ? " (bold call)" : "");

      const title = document.createElementNS(NS, "title");
      title.textContent = label;
      circle.appendChild(title);

      // Also set aria-label for screen readers
      circle.setAttribute("aria-label", label);
      circle.setAttribute("role", "img");

      dotsGroup.appendChild(circle);
    });
  }

  /**
   * Update the caption link to point to the active source's external board
   * (work_url) or the internal /sources/{slug} page when no external URL exists.
   *
   * @param {Object} overlay - The currently active source overlay.
   */
  function updateCaptionLink(overlay) {
    const link = document.getElementById("scatterSourceLink");
    if (!link) return;

    const name = overlay.source_display_name || overlay.source_name || "";
    link.textContent = name;

    if (overlay.work_url) {
      link.href = overlay.work_url;
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
      link.setAttribute("title", `${name}'s published board (opens in a new tab)`);
    } else {
      link.href = `/sources/${overlay.source_slug}`;
      link.removeAttribute("target");
      link.removeAttribute("rel");
      link.setAttribute("title", `${name} on DraftGuru`);
    }
  }

  /**
   * Activate the picker button for the given index (aria + CSS class).
   *
   * @param {NodeList} buttons - All picker buttons.
   * @param {number} activeIndex - Index of the button to activate.
   */
  function activateButton(buttons, activeIndex) {
    buttons.forEach((btn, i) => {
      const isActive = i === activeIndex;
      btn.classList.toggle("is-active", isActive);
      btn.setAttribute("aria-selected", isActive ? "true" : "false");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    // Read serialized overlay data
    const dataEl = document.getElementById("scatterOverlayData");
    if (!dataEl) return;

    let overlays;
    try {
      overlays = JSON.parse(dataEl.textContent || "[]");
    } catch (e) {
      console.warn("[consensus-scatter] Failed to parse overlay data:", e);
      return;
    }

    if (!overlays || overlays.length === 0) return;

    // Render first source by default
    renderDots(overlays[0]);

    // Wire picker buttons
    const buttons = document.querySelectorAll(".scatter-card__picker-btn");
    buttons.forEach((btn) => {
      btn.addEventListener("click", function () {
        const idx = parseInt(this.getAttribute("data-source-index") || "0", 10);
        const overlay = overlays[idx];
        if (!overlay) return;

        activateButton(buttons, idx);
        renderDots(overlay);
        updateCaptionLink(overlay);
      });
    });
  });
})();
