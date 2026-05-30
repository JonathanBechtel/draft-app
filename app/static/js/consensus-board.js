/* ==========================================================================
   consensus-board.js — Full board table JS for /consensus
   Owned by ticket #272. Consolidated into consensus.js by ticket #278.

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

  /** Return parsed numeric value from a data-* attribute, or Infinity when absent. */
  function numAttr(row, attr) {
    const v = row.dataset[attr];
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

      if (searchMatch && posMatch) {
        row.classList.remove("cb-row--hidden");
        visibleCount++;
      } else {
        row.classList.add("cb-row--hidden");
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

    rows.sort(function (a, b) {
      const av = numAttr(a, col);
      const bv = numAttr(b, col);
      // Infinity (missing) always goes to the bottom regardless of direction
      if (av === Infinity && bv === Infinity) return 0;
      if (av === Infinity) return 1;
      if (bv === Infinity) return -1;
      return (av - bv) * dir;
    });

    // Re-append rows in sorted order (fragment for one reflow)
    const frag = document.createDocumentFragment();
    rows.forEach(function (r) {
      frag.appendChild(r);
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
  // Initial aria-sort state already set in the HTML (ascending on rank).
  // -------------------------------------------------------------------------
})();
