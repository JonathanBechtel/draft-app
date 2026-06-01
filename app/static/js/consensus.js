/* ==========================================================================
   consensus.js — All JavaScript for the /consensus page.
   Consolidated by ticket #278 from the following per-section files:
     consensus-board.js        (ticket #272)
     consensus-scatter.js      (ticket #273)
     consensus-trajectories.js (ticket #276)
   ========================================================================== */

/* ==========================================================================
   SECTION 0 — PAGE-LEVEL INIT
   (formerly consensus.js global hook)
   ========================================================================== */

document.addEventListener("DOMContentLoaded", function () {
  // Global consensus page initialisation hook.
  // Section modules below initialise themselves within this same listener scope
  // or via their own self-contained IIFE + DOMContentLoaded pattern.
  // Place cross-section coordination logic here.
});

/* ==========================================================================
   SECTION 1 — FULL BOARD TABLE
   (formerly consensus-board.js — ticket #272)

   Features:
   - Client-side search (player name / school)
   - Position filter
   - Column sort (rank, avg, age, #sources)
   All without a page reload.
   ========================================================================== */

(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // DOM refs (bail early when not on the consensus board page)
  // -------------------------------------------------------------------------
  const table = document.getElementById("cbTable");
  const tbody = document.getElementById("cbBody");
  const searchInput = document.getElementById("cbSearch");
  const posFilter = document.getElementById("cbPosFilter");
  const emptyMsg = document.getElementById("cbEmpty");

  if (!table || !tbody) return;

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------
  let currentSort = { col: "consensus_rank", dir: "asc" };
  let currentSearch = "";
  let currentPos = "";

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  // Map a sortable header's data-col value to the row's compact data-*
  // attribute name. Header cols use semantic names (consensus_rank, avg_rank,
  // num_sources) while rows carry shorter dataset keys (rank, avg, sources).
  const SORT_COL_TO_ATTR = {
    consensus_rank: "rank",
    avg_rank: "avg",
    num_sources: "sources",
    age: "age",
  };

  /** Return parsed numeric value from a data-* attribute, or Infinity when absent. */
  function numAttr(row, attr) {
    const key = SORT_COL_TO_ATTR[attr] || attr;
    const v = row.dataset[key];
    if (v === "" || v === undefined || v === null) return Infinity;
    const n = parseFloat(v);
    return isNaN(n) ? Infinity : n;
  }

  /** Return lowercase string from a data-* attribute. */
  function strAttr(row, attr) {
    return (row.dataset[attr] || "").toLowerCase();
  }

  // -------------------------------------------------------------------------
  // Filter: show/hide rows based on search + position
  // -------------------------------------------------------------------------
  function applyFilter() {
    const rows = Array.from(tbody.querySelectorAll(".cb-row"));
    let visibleCount = 0;

    rows.forEach(function (row) {
      const playerName = strAttr(row, "player");
      const school = strAttr(row, "school");
      const pos = strAttr(row, "pos");

      const searchMatch =
        currentSearch === "" ||
        playerName.includes(currentSearch) ||
        school.includes(currentSearch);

      // Position filter: match when the row's position contains the filter
      // value (e.g. "PG/SG" contains "PG").
      const posMatch =
        currentPos === "" ||
        pos.includes(currentPos.toLowerCase());

      // Keep the paired detail row (per-analyst breakdown) in sync with its
      // player row so a filtered-out row never leaves an orphan panel behind.
      const next = row.nextElementSibling;
      const detailRow =
        next && next.classList.contains("cb-detail-row") ? next : null;

      if (searchMatch && posMatch) {
        row.classList.remove("cb-row--hidden");
        if (detailRow) detailRow.classList.remove("cb-row--hidden");
        visibleCount++;
      } else {
        row.classList.add("cb-row--hidden");
        if (detailRow) detailRow.classList.add("cb-row--hidden");
      }
    });

    // Show the empty-state message when no rows are visible
    if (emptyMsg) {
      emptyMsg.hidden = visibleCount > 0;
    }
  }

  // -------------------------------------------------------------------------
  // Sort: reorder rows in the tbody
  // -------------------------------------------------------------------------
  function applySort() {
    const rows = Array.from(tbody.querySelectorAll(".cb-row"));
    const col = currentSort.col;
    const dir = currentSort.dir === "asc" ? 1 : -1;

    // Capture each row's paired detail row (the per-analyst breakdown) BEFORE
    // reordering, so we can keep the panel directly beneath its player.
    const pairs = rows.map(function (r) {
      const next = r.nextElementSibling;
      return {
        row: r,
        detail:
          next && next.classList.contains("cb-detail-row") ? next : null,
      };
    });

    pairs.sort(function (a, b) {
      const av = numAttr(a.row, col);
      const bv = numAttr(b.row, col);
      // Infinity (missing) always goes to the bottom regardless of direction
      if (av === Infinity && bv === Infinity) return 0;
      if (av === Infinity) return 1;
      if (bv === Infinity) return -1;
      return (av - bv) * dir;
    });

    // Re-append rows (and their detail rows) in sorted order, one reflow.
    const frag = document.createDocumentFragment();
    pairs.forEach(function (p) {
      frag.appendChild(p.row);
      if (p.detail) frag.appendChild(p.detail);
    });
    tbody.appendChild(frag);
  }

  // -------------------------------------------------------------------------
  // Update header aria-sort attributes
  // -------------------------------------------------------------------------
  function updateHeaderAriaSort() {
    const ths = table.querySelectorAll(".cb-th[data-col]");
    ths.forEach(function (th) {
      if (th.dataset.col === currentSort.col) {
        th.setAttribute("aria-sort", currentSort.dir === "asc" ? "ascending" : "descending");
      } else {
        th.removeAttribute("aria-sort");
      }
    });
  }

  // -------------------------------------------------------------------------
  // Event: search input
  // -------------------------------------------------------------------------
  if (searchInput) {
    searchInput.addEventListener("input", function () {
      currentSearch = this.value.trim().toLowerCase();
      applyFilter();
    });
  }

  // -------------------------------------------------------------------------
  // Event: position select
  // -------------------------------------------------------------------------
  if (posFilter) {
    posFilter.addEventListener("change", function () {
      currentPos = this.value;
      applyFilter();
    });
  }

  // -------------------------------------------------------------------------
  // Event: column header clicks (sort)
  // -------------------------------------------------------------------------
  const sortHeaders = table.querySelectorAll(".cb-th[data-col]");
  sortHeaders.forEach(function (th) {
    th.addEventListener("click", function () {
      const col = this.dataset.col;
      if (currentSort.col === col) {
        // Toggle direction on repeated click
        currentSort.dir = currentSort.dir === "asc" ? "desc" : "asc";
      } else {
        currentSort.col = col;
        // Default direction: ascending for rank (lower = better),
        // descending for avg (lower avg = more consensus pick → asc),
        // descending for sources (more sources = more data).
        currentSort.dir = "asc";
      }
      updateHeaderAriaSort();
      applySort();
      applyFilter(); // re-apply filter after reorder to keep hidden rows hidden
    });

    // Keyboard: Enter/Space to sort
    th.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        th.click();
      }
    });
  });

  // -------------------------------------------------------------------------
  // Per-analyst source breakdown: expand toggle + lazy fetch
  //
  // A ▸ toggle in the leading column reveals a sibling detail row listing where
  // each source ranked the player, with an outbound link to their board. The
  // panel is hydrated once, on first expand, from
  // GET /api/consensus/player/{id}?draft_year=&kind= (PlayerConsensusDetail).
  // -------------------------------------------------------------------------
  const draftYear = table.dataset.draftYear;
  const boardKind = table.dataset.boardKind;

  /** Replace the panel contents with a single status line (loading / error). */
  function setStatus(container, message, isError) {
    container.textContent = "";
    const p = document.createElement("p");
    p.className =
      "cb-detail__status" + (isError ? " cb-detail__status--error" : "");
    p.textContent = message;
    container.appendChild(p);
  }

  /** Map a per-source rank to a percentage on the [high, low] (consensus-folded) scale. */
  function scalePct(rank, scaleMin, span) {
    return (((rank - scaleMin) / span) * 100).toFixed(1) + "%";
  }

  /** Build a delta chip element (source rank vs. consensus rank). */
  function deltaChip(sourceRank, consensusRank) {
    const d = sourceRank - consensusRank;
    const el = document.createElement("span");
    if (d < 0) {
      el.className = "cb-source__delta cb-source__delta--up";
      el.textContent = "▲" + Math.abs(d);
    } else if (d > 0) {
      el.className = "cb-source__delta cb-source__delta--down";
      el.textContent = "▼" + d;
    } else {
      el.className = "cb-source__delta cb-source__delta--flat";
      el.textContent = "–";
    }
    return el;
  }

  /** Render the full per-analyst breakdown panel from a PlayerConsensusDetail. */
  function renderDetail(container, detail) {
    container.textContent = "";
    const ranks = detail.source_ranks || [];
    const consensusRank = detail.consensus_rank;

    // Header: title + summary line
    const head = document.createElement("div");
    head.className = "cb-detail__head";
    const title = document.createElement("span");
    title.className = "cb-detail__title";
    title.textContent =
      "Where analysts ranked " + (detail.player_name || "this player");
    const summary = document.createElement("span");
    summary.className = "cb-detail__summary";
    summary.textContent =
      ranks.length +
      " of " +
      detail.num_sources +
      " boards · range #" +
      detail.high_rank +
      "–#" +
      detail.low_rank +
      " · consensus #" +
      consensusRank;
    head.appendChild(title);
    head.appendChild(summary);
    container.appendChild(head);

    // Scale strip (skip when every source agrees — no spread to show)
    if (detail.low_rank !== detail.high_rank) {
      const scaleMin = Math.min(detail.high_rank, consensusRank);
      const scaleMax = Math.max(detail.low_rank, consensusRank);
      const span = scaleMax - scaleMin || 1;

      const scale = document.createElement("div");
      scale.className = "cb-detail__scale";
      const edgeLo = document.createElement("span");
      edgeLo.className = "cb-detail__scale-edge";
      edgeLo.textContent = "#" + detail.high_rank;
      const track = document.createElement("div");
      track.className = "cb-detail__scale-track";
      ranks.forEach(function (entry) {
        const tick = document.createElement("span");
        tick.className = "cb-detail__tick";
        tick.style.left = scalePct(entry.source_rank, scaleMin, span);
        tick.title =
          (entry.source_display_name || entry.source_name) +
          " · #" +
          entry.source_rank;
        track.appendChild(tick);
      });
      const cons = document.createElement("span");
      cons.className = "cb-detail__consensus";
      cons.style.left = scalePct(consensusRank, scaleMin, span);
      cons.title = "Consensus #" + consensusRank;
      track.appendChild(cons);
      const edgeHi = document.createElement("span");
      edgeHi.className = "cb-detail__scale-edge";
      edgeHi.textContent = "#" + detail.low_rank;
      scale.appendChild(edgeLo);
      scale.appendChild(track);
      scale.appendChild(edgeHi);
      container.appendChild(scale);
    }

    // Per-source list — outbound link per creator (↗) when an article URL exists
    const list = document.createElement("ul");
    list.className = "cb-detail__sources";
    ranks.forEach(function (entry) {
      const li = document.createElement("li");
      li.className = "cb-source";
      if (Math.abs(entry.source_rank - consensusRank) >= 5) {
        li.classList.add("is-outlier");
      }

      const rank = document.createElement("span");
      rank.className = "cb-source__rank";
      rank.textContent = "#" + entry.source_rank;

      const main = document.createElement("span");
      main.className = "cb-source__main";

      const name = entry.source_display_name || entry.source_name || "Source";
      let nameEl;
      if (entry.article_url) {
        nameEl = document.createElement("a");
        nameEl.className = "cb-source__name cb-source__name--link";
        nameEl.href = entry.article_url;
        nameEl.target = "_blank";
        nameEl.rel = "noopener noreferrer";
        nameEl.title = name + "'s published board (opens in a new tab)";
      } else {
        nameEl = document.createElement("span");
        nameEl.className = "cb-source__name";
      }
      nameEl.textContent = name;
      main.appendChild(nameEl);

      if (entry.article_title && entry.article_url) {
        const board = document.createElement("a");
        board.className = "cb-source__board";
        board.href = entry.article_url;
        board.target = "_blank";
        board.rel = "noopener noreferrer";
        board.textContent = entry.article_title;
        main.appendChild(board);
      }

      li.appendChild(rank);
      li.appendChild(main);
      li.appendChild(deltaChip(entry.source_rank, consensusRank));
      list.appendChild(li);
    });
    container.appendChild(list);

    // Attribution footnote — reinforces aggregate-don't-reproduce stance
    const foot = document.createElement("p");
    foot.className = "cb-detail__foot";
    foot.textContent =
      "Ranks link to each creator's original board. DraftGuru aggregates — it doesn't reproduce — their work.";
    container.appendChild(foot);
  }

  /** Fetch + render a player's per-source breakdown into its panel (once). */
  function loadDetail(row, panel) {
    const playerId = row.dataset.playerId;
    if (!playerId || !draftYear || !boardKind) {
      setStatus(panel, "No source data available.", true);
      panel.dataset.loaded = "true";
      return;
    }
    setStatus(panel, "Loading source ranks…", false);
    const url =
      "/api/consensus/player/" +
      encodeURIComponent(playerId) +
      "?draft_year=" +
      encodeURIComponent(draftYear) +
      "&kind=" +
      encodeURIComponent(boardKind);

    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (detail) {
        if (!detail.source_ranks || detail.source_ranks.length === 0) {
          setStatus(panel, "No per-source ranks available for this player.", false);
        } else {
          renderDetail(panel, detail);
        }
        panel.dataset.loaded = "true";
      })
      .catch(function () {
        // Leave data-loaded="false" so a later re-expand retries the fetch.
        setStatus(panel, "Couldn't load source ranks. Try again.", true);
        panel.dataset.loaded = "false";
      });
  }

  tbody.querySelectorAll(".cb-toggle").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      // Stop the click from bubbling to the row's navigate-to-player handler.
      e.stopPropagation();
      const row = btn.closest(".cb-row");
      const detailRow = document.getElementById(btn.getAttribute("aria-controls"));
      if (!row || !detailRow) return;

      const expanded = row.classList.toggle("is-expanded");
      btn.setAttribute("aria-expanded", expanded ? "true" : "false");
      btn.title = expanded ? "Hide source ranks" : "Show source ranks";
      detailRow.hidden = !expanded;

      if (expanded) {
        const panel = detailRow.querySelector(".cb-detail");
        if (panel && panel.dataset.loaded !== "true") {
          loadDetail(row, panel);
        }
      }
    });
  });

  // -------------------------------------------------------------------------
  // Initial aria-sort state already set in the HTML (ascending on rank).
  // -------------------------------------------------------------------------
})();

/* ==========================================================================
   SECTION 2 — AGREEMENT SCATTER + SOURCE PICKER
   (formerly consensus-scatter.js — ticket #273)

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
   * Update the alignment-score readout for the active source. Hides the block
   * when the source has no alignment score (ranked too few shared players).
   *
   * @param {Object} overlay - The currently active source overlay.
   */
  function updateAlignment(overlay) {
    const wrap = document.getElementById("scatterAlignment");
    const scoreEl = document.getElementById("scatterAlignmentScore");
    if (!wrap || !scoreEl) return;

    const score = overlay.alignment_score;
    if (score == null) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    scoreEl.textContent = score;
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
        updateAlignment(overlay);
      });
    });
  });
})();

/* ==========================================================================
   SECTION 5 — PLAYER RANK TRAJECTORIES
   (formerly consensus-trajectories.js — ticket #276)

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
