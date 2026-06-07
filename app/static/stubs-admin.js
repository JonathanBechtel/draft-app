/**
 * stubs-admin.js
 * Bulk-select, quick-add modal, and filter wiring for the Stubs admin tab.
 * Initialised on DOMContentLoaded.
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

    function injectSelectedIds() {
      if (!bulkDeleteForm) return;
      // Remove previous hidden inputs
      var old = bulkDeleteForm.querySelectorAll('input[type="hidden"][name="player_ids[]"]');
      old.forEach(function (el) { el.remove(); });

      getChecked().forEach(function (cb) {
        var hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = "player_ids[]";
        hidden.value = cb.value;
        bulkDeleteForm.appendChild(hidden);
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
      bulkDeleteForm.addEventListener("submit", injectSelectedIds);
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
  // Init
  // ---------------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    initQuickAddModal();
    initBulkSelect();
    initFilterWiring();
  });
})();
