/* Summer League Explorer — light in-place enhancement.
 *
 * Progressive enhancement over the server-rendered builder: intercept the
 * filter form submit and the sort/pager links inside the results, fetch the
 * same URL with `?partial=1` (which returns just the results fragment), and
 * swap it in without a full page reload. Everything still works with JS off —
 * the form is a normal GET and the links are real hrefs.
 *
 * Additionally:
 *   - Column-group toggle (Box / Shooting / Advanced): persistent state across
 *     AJAX swaps; delegated click from main; re-applied after each swap.
 *   - Per-pool drill-down: expand a career-grain player row to show its
 *     per-competition breakdown; fetched from /explorer/drilldown.
 */
(function () {
  "use strict";

  var RESULTS_ID = "explorer-results";
  var form = document.getElementById("explorer-form");
  var resultsHost = document.getElementById(RESULTS_ID);
  if (!form || !resultsHost) return;

  // The persistent ancestor we delegate clicks from (survives result swaps).
  var main = resultsHost.closest("main") || document.body;

  // "Add filter" button — referenced in syncForm too, so hoist.
  var addBtn = document.getElementById("add-metric-filter");

  // ── Column-group toggle state ──────────────────────────────────────────────
  // Persists across AJAX swaps (lives in module scope, not the DOM).
  var activeGroups = { box: true, shooting: true, advanced: true };

  function applyColGroups() {
    ["box", "shooting", "advanced"].forEach(function (group) {
      var show = !!activeGroups[group];
      document.querySelectorAll('[data-col-group="' + group + '"]').forEach(function (el) {
        el.style.display = show ? "" : "none";
      });
      // Keep toggle buttons in sync (they live outside the swapped fragment).
      var btn = document.querySelector('.slg-col-group-btn[data-group="' + group + '"]');
      if (btn) {
        btn.classList.toggle("is-active", show);
      }
    });
  }

  function withPartial(url) {
    return url + (url.indexOf("?") === -1 ? "?" : "&") + "partial=1";
  }

  // Strip a `partial` param so the address bar shows the canonical URL.
  function canonical(url) {
    return url.replace(/([?&])partial=1(&|$)/, function (_m, pre, post) {
      return post ? pre : pre === "?" ? "" : "";
    });
  }

  // Push the just-loaded URL's params back onto the (persistent) form controls,
  // so the form always reflects the visible results. Params absent from the URL
  // reset their control to empty (the "All/Any" option).
  function syncForm(url) {
    var qi = url.indexOf("?");
    var params = new URLSearchParams(qi === -1 ? "" : url.slice(qi + 1));
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || el.type === "submit" || el.type === "button") return;
      el.value = params.has(el.name) ? params.get(el.name) : "";
    });
    // Reveal metric filter rows that have data in the URL.
    for (var i = 0; i < 3; i++) {
      var row = document.getElementById("metric-filter-row-" + i);
      if (!row) continue;
      var hasData = params.get("fcol" + i) && params.get("fval" + i);
      if (hasData) row.classList.remove("slg-hidden");
    }
    // Sync add-filter button visibility.
    if (addBtn) {
      var anyHidden = document.querySelectorAll(".slg-metric-filter-row.slg-hidden").length > 0;
      addBtn.style.display = anyHidden ? "" : "none";
    }
  }

  function load(url, push) {
    var host = document.getElementById(RESULTS_ID);
    if (host) host.classList.add("is-loading");
    fetch(withPartial(url), { headers: { "X-Requested-With": "fetch" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) {
        var cur = document.getElementById(RESULTS_ID);
        if (!cur) {
          window.location.assign(url);
          return;
        }
        cur.outerHTML = html;
        if (push) history.pushState({ explorerUrl: url }, "", canonical(url));
        // The form lives outside the swapped fragment, so its controls (and the
        // hidden sort/dir inputs) would keep stale values after a sort/pager/back
        // swap — a later filter submit would then send the old sort. Re-sync the
        // whole form to the URL we just loaded.
        syncForm(url);
        // Re-apply column-group visibility to the freshly rendered cells.
        applyColGroups();
        // Keep the viewport anchored to the results, not jumped to top.
        var fresh = document.getElementById(RESULTS_ID);
        if (fresh) fresh.scrollIntoView({ block: "nearest" });
      })
      .catch(function () {
        // Network/parse failure — fall back to a real navigation.
        window.location.assign(url);
      });
  }

  // Build the query URL from the current form state and load it.
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    // Drop empty fields so the shareable URL stays clean (the server treats a
    // missing param the same as a blank one).
    var clean = new URLSearchParams();
    new URLSearchParams(new FormData(form)).forEach(function (v, k) {
      if (v !== "") clean.append(k, v);
    });
    load(form.action + "?" + clean.toString(), true);
  });

  // Sort headers and pager links live inside the swapped results fragment, so
  // delegate from the persistent ancestor.
  main.addEventListener("click", function (e) {
    var link = e.target.closest && e.target.closest("a");
    if (!link) return;
    var host = document.getElementById(RESULTS_ID);
    if (!host || !host.contains(link)) return; // only links inside the results
    if (link.target === "_blank" || e.metaKey || e.ctrlKey) return;
    // Download links (CSV export) must navigate normally so the browser saves the
    // file — never swap their body into the results container.
    if (link.hasAttribute("download")) return;
    // Only intercept query-state links (sort headers + pager), which are
    // relative `?...` hrefs. Row links (player/team/game) are absolute paths
    // and must navigate out of the explorer normally.
    var href = link.getAttribute("href");
    if (!href || href.charAt(0) !== "?") return;
    e.preventDefault();
    load(href, true);
  });

  // Back/forward should restore the corresponding results.
  window.addEventListener("popstate", function () {
    load(window.location.href, false);
  });

  // Metric filter row reveal: "Add filter" shows the next hidden row.
  if (addBtn) {
    addBtn.addEventListener("click", function () {
      var rows = document.querySelectorAll(".slg-metric-filter-row.slg-hidden");
      if (rows.length > 0) {
        rows[0].classList.remove("slg-hidden");
      }
      // Hide the button once all rows are visible.
      if (document.querySelectorAll(".slg-metric-filter-row.slg-hidden").length === 0) {
        addBtn.style.display = "none";
      }
    });
  }

  // ── Column-group toggle ────────────────────────────────────────────────────
  // Toggle buttons live in explorer.html (persistent, outside the swapped
  // fragment) so their event listeners survive AJAX swaps.
  main.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".slg-col-group-btn");
    if (!btn) return;
    var group = btn.dataset.group;
    if (!group) return;
    activeGroups[group] = !activeGroups[group];
    applyColGroups();
  });

  // Apply initial column visibility (all visible by default; respects any
  // server-rendered active state should the server ever pre-collapse groups).
  applyColGroups();

  // ── Drill-down (expand career row to per-competition breakdown) ────────────
  // Build the drilldown URL from the current window URL (which reflects the
  // active query after any AJAX navigation) and the player slug.
  function buildDrilldownUrl(playerSlug) {
    var qi = window.location.href.indexOf("?");
    var params = new URLSearchParams(qi === -1 ? "" : window.location.href.slice(qi + 1));
    params.set("player_slug", playerSlug);
    params.delete("format");
    params.delete("partial");
    params.delete("page");
    return "/stats/summer-league/explorer/drilldown?" + params.toString();
  }

  // Insert an array of <tr> elements after a reference <tr>.
  function insertRowsAfter(refRow, rows) {
    var cur = refRow;
    rows.forEach(function (row) {
      cur.parentNode.insertBefore(row, cur.nextSibling);
      cur = row;
    });
  }

  // Parse raw HTML text into <tr> elements using a temporary <tbody>.
  function parseRows(html) {
    var tmp = document.createElement("tbody");
    tmp.innerHTML = html;
    return Array.prototype.slice.call(tmp.querySelectorAll("tr"));
  }

  // Delegate expand-button clicks from the persistent ancestor.
  main.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".slg-expand-btn");
    if (!btn) return;
    var playerSlug = btn.dataset.playerSlug;
    if (!playerSlug) return;

    var parentRow = btn.closest("tr");
    if (!parentRow) return;

    // Collapse if already expanded.
    var existing = document.querySelectorAll('[data-drilldown="' + playerSlug + '"]');
    if (existing.length > 0) {
      existing.forEach(function (r) { r.parentNode && r.parentNode.removeChild(r); });
      btn.setAttribute("aria-expanded", "false");
      btn.innerHTML = "&#9654;"; // ▶
      return;
    }

    // Fetch and insert drilldown rows.
    btn.innerHTML = "&#8230;"; // …
    var url = buildDrilldownUrl(playerSlug);
    fetch(url, { headers: { "X-Requested-With": "fetch" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) {
        var rows = parseRows(html);
        insertRowsAfter(parentRow, rows);
        btn.setAttribute("aria-expanded", "true");
        btn.innerHTML = "&#9660;"; // ▼
        // Apply active column groups to the newly inserted rows.
        applyColGroups();
      })
      .catch(function () {
        btn.innerHTML = "&#9654;"; // ▶ revert on error
      });
  });
})();
