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
  // Duplicates modal + merge-confirm flow
  //
  // Flow:
  //   1. "Find Dups" button → fetch GET /{player_id}/duplicates → show candidates.
  //   2. "Merge" on a candidate → fetch GET /merge/preview → show dry-run report.
  //   3. Admin can flip survivor (swap keep/discard) before confirming.
  //   4. "Merge (Irreversible)" → POST /merge (with confirm=yes) via form submit.
  // ---------------------------------------------------------------------------

  var dupsModal = null;
  var mergeModal = null;

  // Current state for the merge preview
  var _mergeKeepId = null;
  var _mergeDiscardId = null;
  var _mergeKeepName = null;
  var _mergeDiscardName = null;
  var _stubPlayerId = null;  // the original stub whose "Find Dups" was clicked

  function openDupsModal(playerId, playerName) {
    if (!dupsModal) return;
    document.getElementById("dups-modal-player-name").textContent = playerName;
    document.getElementById("dups-modal-body").innerHTML =
      '<p class="admin-text--muted">Loading candidates…</p>';
    dupsModal.style.display = "flex";
    _stubPlayerId = playerId;

    fetch("/admin/players/stubs/" + playerId + "/duplicates")
      .then(function (resp) {
        if (!resp.ok) {
          throw new Error("HTTP " + resp.status);
        }
        return resp.json();
      })
      .then(function (candidates) {
        renderCandidates(candidates, playerId, playerName);
      })
      .catch(function (err) {
        document.getElementById("dups-modal-body").innerHTML =
          '<p class="admin-text--error">Failed to load candidates: ' +
          escapeHtml(err.message) +
          "</p>";
      });
  }

  function closeDupsModal() {
    if (dupsModal) dupsModal.style.display = "none";
  }

  function renderCandidates(candidates, stubId, stubName) {
    var body = document.getElementById("dups-modal-body");
    if (!body) return;

    if (!candidates || candidates.length === 0) {
      body.innerHTML =
        '<p class="admin-text--muted">No near-duplicate candidates found for this player.</p>';
      return;
    }

    var html = '<ul class="stubs-dups-list">';
    candidates.forEach(function (c) {
      var score = (c.score * 100).toFixed(1);
      html +=
        '<li class="stubs-dups-list__item">' +
        '<div class="stubs-dups-list__info">' +
        '<strong class="stubs-dups-list__name">' +
        escapeHtml(c.display_name || "—") +
        "</strong>" +
        (c.school
          ? ' <span class="admin-text--small admin-text--muted">' +
            escapeHtml(c.school) +
            "</span>"
          : "") +
        ' <span class="admin-badge admin-badge--unknown stubs-dups-score">' +
        score +
        "% match</span>" +
        "</div>" +
        '<button type="button" class="admin-btn admin-btn--secondary admin-btn--small stubs-merge-btn"' +
        ' data-keep-id="' + c.player_id + '"' +
        ' data-keep-name="' + escapeHtml(c.display_name || "") + '"' +
        ' data-discard-id="' + stubId + '"' +
        ' data-discard-name="' + escapeHtml(stubName) + '">' +
        "Preview Merge" +
        "</button>" +
        "</li>";
    });
    html += "</ul>";
    html +=
      '<p class="admin-text--small admin-text--muted" style="margin-top:0.75rem;">' +
      "Default: the candidate survives and the stub is discarded. " +
      "You can flip the survivor in the merge preview." +
      "</p>";
    body.innerHTML = html;

    // Wire merge-preview buttons
    var mergeBtns = body.querySelectorAll(".stubs-merge-btn");
    mergeBtns.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var keepId = parseInt(btn.getAttribute("data-keep-id"), 10);
        var keepName = btn.getAttribute("data-keep-name");
        var discardId = parseInt(btn.getAttribute("data-discard-id"), 10);
        var discardName = btn.getAttribute("data-discard-name");
        openMergePreview(keepId, keepName, discardId, discardName);
      });
    });
  }

  function openMergePreview(keepId, keepName, discardId, discardName) {
    if (!mergeModal) return;
    _mergeKeepId = keepId;
    _mergeDiscardId = discardId;
    _mergeKeepName = keepName;
    _mergeDiscardName = discardName;

    closeDupsModal();

    document.getElementById("merge-preview-body").innerHTML =
      '<p class="admin-text--muted">Loading dry-run preview…</p>';
    document.getElementById("merge-preview-footer").style.display = "none";
    mergeModal.style.display = "flex";

    var url =
      "/admin/players/stubs/merge/preview?keep_id=" + keepId + "&discard_id=" + discardId;
    fetch(url)
      .then(function (resp) {
        if (!resp.ok) {
          throw new Error("HTTP " + resp.status);
        }
        return resp.json();
      })
      .then(function (report) {
        renderMergePreview(report);
      })
      .catch(function (err) {
        document.getElementById("merge-preview-body").innerHTML =
          '<p class="admin-text--error">Failed to load preview: ' +
          escapeHtml(err.message) +
          "</p>";
      });
  }

  function renderMergePreview(report) {
    var body = document.getElementById("merge-preview-body");
    var footer = document.getElementById("merge-preview-footer");
    var directionControls = document.getElementById("merge-direction-controls");
    if (!body || !footer) return;

    // Summary
    var totalReassigned = 0;
    var totalDeleted = 0;
    Object.keys(report.per_table).forEach(function (tbl) {
      totalReassigned += report.per_table[tbl].reassigned || 0;
      totalDeleted += report.per_table[tbl].deleted_conflict || 0;
    });

    var html =
      '<div class="stubs-merge-preview">' +
      '<div class="stubs-merge-preview__summary">' +
      '<span class="stubs-merge-preview__survivor">' +
      "<strong>Survivor:</strong> " +
      escapeHtml(_mergeKeepName || String(report.keep_id)) +
      " (id " + report.keep_id + ")" +
      "</span>" +
      '<span class="stubs-merge-preview__discard">' +
      "<strong>Discarded:</strong> " +
      escapeHtml(_mergeDiscardName || String(report.discard_id)) +
      " (id " + report.discard_id + ")" +
      "</span>" +
      "</div>";

    if (report.alias_added) {
      html +=
        '<p class="stubs-merge-preview__alias">Alias to add: <em>' +
        escapeHtml(report.alias_added) +
        "</em></p>";
    }

    if (Object.keys(report.per_table).length > 0) {
      html += '<table class="admin-table stubs-merge-preview__table"><thead><tr>' +
        "<th>Table / Column</th><th>Reassigned</th><th>Conflict-deleted</th>" +
        "</tr></thead><tbody>";
      Object.keys(report.per_table).forEach(function (tbl) {
        var row = report.per_table[tbl];
        html +=
          "<tr><td>" +
          escapeHtml(tbl) +
          "</td><td>" +
          (row.reassigned || 0) +
          "</td><td>" +
          (row.deleted_conflict || 0) +
          "</td></tr>";
      });
      html += "</tbody></table>";
    } else {
      html += '<p class="admin-text--muted">No child rows to migrate.</p>';
    }

    html +=
      '<p class="admin-text--small admin-text--muted" style="margin-top:0.75rem;">' +
      "Total: " +
      totalReassigned +
      " rows will be reassigned, " +
      totalDeleted +
      " conflict rows deleted. This action cannot be undone." +
      "</p>";
    html += "</div>";

    body.innerHTML = html;

    // Wire the direction controls (flip survivor)
    if (directionControls) {
      directionControls.innerHTML =
        '<div class="stubs-merge-direction__row">' +
        '<span class="stubs-merge-direction__label">Survivor: <strong id="merge-survivor-label">' +
        escapeHtml(_mergeKeepName || String(report.keep_id)) +
        "</strong></span>" +
        '<button type="button" class="admin-btn admin-btn--secondary admin-btn--small" id="merge-flip-btn">' +
        "⇄ Flip Survivor" +
        "</button>" +
        "</div>";

      var flipBtn = document.getElementById("merge-flip-btn");
      if (flipBtn) {
        flipBtn.addEventListener("click", function () {
          // Swap keep/discard
          var tmpId = _mergeKeepId;
          var tmpName = _mergeKeepName;
          _mergeKeepId = _mergeDiscardId;
          _mergeKeepName = _mergeDiscardName;
          _mergeDiscardId = tmpId;
          _mergeDiscardName = tmpName;

          // Re-fetch and re-render
          openMergePreview(_mergeKeepId, _mergeKeepName, _mergeDiscardId, _mergeDiscardName);
        });
      }
    }

    // Update hidden form inputs
    var keepInput = document.getElementById("merge-keep-id");
    var discardInput = document.getElementById("merge-discard-id");
    if (keepInput) keepInput.value = String(report.keep_id);
    if (discardInput) discardInput.value = String(report.discard_id);

    footer.style.display = "";
  }

  function closeMergeModal() {
    if (mergeModal) mergeModal.style.display = "none";
  }

  function escapeHtml(str) {
    var d = document.createElement("div");
    d.appendChild(document.createTextNode(str));
    return d.innerHTML;
  }

  function initDuplicatesFlow() {
    dupsModal = document.getElementById("dups-modal");
    mergeModal = document.getElementById("merge-preview-modal");

    if (!dupsModal && !mergeModal) return;

    // Dups modal close
    var dupsClose = document.getElementById("dups-modal-close");
    if (dupsClose) dupsClose.addEventListener("click", closeDupsModal);
    if (dupsModal) {
      var dupsBackdrop = dupsModal.querySelector(".stubs-modal__backdrop");
      if (dupsBackdrop) dupsBackdrop.addEventListener("click", closeDupsModal);
    }

    // Merge modal close
    var mergeClose = document.getElementById("merge-preview-close");
    if (mergeClose) mergeClose.addEventListener("click", closeMergeModal);
    var mergeCancel = document.getElementById("merge-preview-cancel");
    if (mergeCancel) mergeCancel.addEventListener("click", closeMergeModal);
    if (mergeModal) {
      var mergeBackdrop = mergeModal.querySelector(".stubs-modal__backdrop");
      if (mergeBackdrop) mergeBackdrop.addEventListener("click", closeMergeModal);
    }

    // Escape key closes whichever modal is open
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        if (mergeModal && mergeModal.style.display !== "none") {
          closeMergeModal();
        } else if (dupsModal && dupsModal.style.display !== "none") {
          closeDupsModal();
        }
      }
    });

    // Wire "Find Dups" buttons (event delegation on the table)
    document.addEventListener("click", function (e) {
      var btn = e.target.closest(".stub-find-dups-btn");
      if (!btn) return;
      var playerId = btn.getAttribute("data-player-id");
      var playerName = btn.getAttribute("data-player-name") || "Player";
      openDupsModal(playerId, playerName);
    });
  }

  // ---------------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    initQuickAddModal();
    initBulkSelect();
    initFilterWiring();
    initEnrichmentPolling();
    initDuplicatesFlow();
  });
})();
