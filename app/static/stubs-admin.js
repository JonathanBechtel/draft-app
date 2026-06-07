/**
 * stubs-admin.js
 * Bulk-select, quick-add modal, filter wiring, and enrichment polling for the
 * Stubs admin tab.  Initialised on DOMContentLoaded.
 */

(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Quick-add modal
  // ---------------------------------------------------------------------------

  function initQuickAddModal() {
    var openBtn = document.getElementById("quick-add-btn");
    var modal = document.getElementById("quick-add-modal");
    var closeBtn = document.getElementById("quick-add-close");
    var cancelBtn = document.getElementById("quick-add-cancel");

    if (!openBtn || !modal) return;

    function openModal() {
      modal.style.display = "flex";
      var nameInput = document.getElementById("quick-add-name");
      if (nameInput) nameInput.focus();
    }

    function closeModal() {
      modal.style.display = "none";
    }

    openBtn.addEventListener("click", openModal);
    if (closeBtn) closeBtn.addEventListener("click", closeModal);
    if (cancelBtn) cancelBtn.addEventListener("click", closeModal);

    // Close on backdrop click
    var backdrop = modal.querySelector(".stubs-modal__backdrop");
    if (backdrop) {
      backdrop.addEventListener("click", closeModal);
    }

    // Close on Escape key
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && modal.style.display !== "none") {
        closeModal();
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Bulk-select
  // ---------------------------------------------------------------------------

  function initBulkSelect() {
    var selectAll = document.getElementById("select-all");
    var toolbar = document.getElementById("bulk-toolbar");
    var bulkCountEl = document.getElementById("bulk-count");
    var bulkDeleteForm = document.getElementById("bulk-delete-form");
    var bulkEnrichForm = document.getElementById("bulk-enrich-form");
    var checkboxes = document.querySelectorAll(".stub-select-checkbox");

    if (!selectAll || !toolbar || !checkboxes.length) return;

    function getChecked() {
      return Array.prototype.slice
        .call(checkboxes)
        .filter(function (cb) { return cb.checked; });
    }

    function updateToolbar() {
      var checked = getChecked();
      var count = checked.length;

      if (count > 0) {
        toolbar.style.display = "flex";
        if (bulkCountEl) {
          bulkCountEl.textContent = count + " selected";
        }
      } else {
        toolbar.style.display = "none";
      }

      // Sync select-all indeterminate state
      if (count === 0) {
        selectAll.indeterminate = false;
        selectAll.checked = false;
      } else if (count === checkboxes.length) {
        selectAll.indeterminate = false;
        selectAll.checked = true;
      } else {
        selectAll.indeterminate = true;
      }
    }

    function injectSelectedIdsInto(form) {
      if (!form) return;
      // Remove previous hidden inputs
      var old = form.querySelectorAll('input[type="hidden"][name="player_ids[]"]');
      old.forEach(function (el) { el.remove(); });

      getChecked().forEach(function (cb) {
        var hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = "player_ids[]";
        hidden.value = cb.value;
        form.appendChild(hidden);
      });
    }

    selectAll.addEventListener("change", function () {
      checkboxes.forEach(function (cb) {
        cb.checked = selectAll.checked;
      });
      updateToolbar();
    });

    checkboxes.forEach(function (cb) {
      cb.addEventListener("change", updateToolbar);
    });

    if (bulkDeleteForm) {
      bulkDeleteForm.addEventListener("submit", function () {
        injectSelectedIdsInto(bulkDeleteForm);
      });
    }

    if (bulkEnrichForm) {
      bulkEnrichForm.addEventListener("submit", function () {
        injectSelectedIdsInto(bulkEnrichForm);
      });
    }
  }

  // ---------------------------------------------------------------------------
  // Auto-submit filter form on select change
  // ---------------------------------------------------------------------------

  function initFilterWiring() {
    var form = document.getElementById("stubs-filter-form");
    if (!form) return;

    var autoSubmitSelects = form.querySelectorAll("select");
    autoSubmitSelects.forEach(function (sel) {
      sel.addEventListener("change", function () {
        form.submit();
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Enrichment status polling
  //
  // Looks for rows with enrichment badge cells (data-player-id on
  // .stubs-col--enrichment td).  If any badge shows "Enriching…", starts a
  // debounced poll loop against /admin/players/stubs/enrichment-status?ids=…
  // and updates badges in place.  Stops when no in-flight jobs remain.
  // ---------------------------------------------------------------------------

  var POLL_INTERVAL_MS = 3000;
  var pollTimer = null;

  function getEnrichmentCells() {
    return Array.prototype.slice.call(
      document.querySelectorAll(".stubs-col--enrichment[data-player-id]")
    );
  }

  function getInFlightIds() {
    return getEnrichmentCells()
      .filter(function (td) {
        var badge = td.querySelector(".stubs-enrich-badge");
        return badge && badge.textContent.trim() === "Enriching…";
      })
      .map(function (td) {
        return td.getAttribute("data-player-id");
      });
  }

  function badgeClassForState(state) {
    if (state === "succeeded") return "admin-badge--enriched";
    if (state === "failed") return "admin-badge--failed";
    if (state === "queued" || state === "running") return "admin-badge--enriching";
    return "admin-badge--unknown";
  }

  function badgeLabelForState(state) {
    if (state === "succeeded") return "Enriched";
    if (state === "failed") return "Failed";
    if (state === "queued" || state === "running") return "Enriching…";
    return "Not Attempted";
  }

  function applyStatusUpdate(statusMap) {
    getEnrichmentCells().forEach(function (td) {
      var pid = td.getAttribute("data-player-id");
      if (!statusMap[pid]) return;

      var info = statusMap[pid];
      var badge = td.querySelector(".stubs-enrich-badge");
      if (!badge) return;

      var newClass = badgeClassForState(info.state);
      var newLabel = badgeLabelForState(info.state);

      // Only update DOM if something changed
      if (badge.textContent.trim() !== newLabel) {
        badge.className = "admin-badge " + newClass + " stubs-enrich-badge";
        badge.textContent = newLabel;
        if (info.error) {
          badge.title = info.error;
        }
      }
    });
  }

  function pollEnrichmentStatus() {
    var ids = getInFlightIds();
    if (ids.length === 0) {
      // Nothing in flight — stop polling
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
      return;
    }

    fetch("/admin/players/stubs/enrichment-status?ids=" + ids.join(","))
      .then(function (resp) {
        if (!resp.ok) return null;
        return resp.json();
      })
      .then(function (data) {
        if (data) {
          applyStatusUpdate(data);
        }
        // Schedule next poll only if still in-flight
        var still = getInFlightIds();
        if (still.length > 0) {
          pollTimer = setTimeout(pollEnrichmentStatus, POLL_INTERVAL_MS);
        }
      })
      .catch(function () {
        // Network error — retry after interval
        pollTimer = setTimeout(pollEnrichmentStatus, POLL_INTERVAL_MS);
      });
  }

  function initEnrichmentPolling() {
    var inFlight = getInFlightIds();
    if (inFlight.length > 0) {
      pollTimer = setTimeout(pollEnrichmentStatus, POLL_INTERVAL_MS);
    }
  }

  // ---------------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    initQuickAddModal();
    initBulkSelect();
    initFilterWiring();
    initEnrichmentPolling();
  });
})();
