/**
 * Summer League Desk (#509, extended #556, tracker pagination pre-prod fix)
 * -- page-scoped JS.
 *
 * Four independent, progressive-enhancement behaviors:
 *
 *   1. Morning slate disclosure -- collapses ONLY the trailing zero-signal
 *      tail of a 10+ game slate, and only after this script runs. The
 *      server always renders every game card, unhidden (`class_tracker.html`
 *      and `slate.html`'s docstrings explain why) -- with JS disabled every
 *      game stays visible, and 5-/9-game slates never collapse even with JS.
 *
 *   2. Class Tracker column sort -- reorders `<tr>`s in the DOM by a numeric
 *      column, both directions, missing values last, name as the stable
 *      tiebreak. Pure client-side reorder: no request, no navigation.
 *
 *   3. Class Tracker pagination -- caps the VISIBLE rows to 15 at a time
 *      (same pattern as #1: the server renders every row of the active
 *      cohort/stat-view, up to `TRACKER_CAP`; JS is what limits what's on
 *      screen). With JS disabled every row stays reachable in the flat,
 *      unpaginated table. Re-sorting resets to page 1 over the new order.
 *
 *   4. Desk player-link click attribution -- fires exactly one `sl_desk_click`
 *      gtag event (this repo's one analytics mechanism -- see `base.html`)
 *      per click on a Desk player link, carrying `placement` (hero/tracker/
 *      ledger/live_board) and `daily_state`. Native `<a href>` navigation is
 *      untouched and works with analytics/JS fully disabled.
 */
document.addEventListener('DOMContentLoaded', function () {
  initSlateDisclosure();
  initTrackerSort();
  initTrackerPagination();
  initDeskClickAnalytics();
});

/**
 * Collapse the trailing zero-signal tail of a 10+ game Morning slate.
 *
 * Reads `data-signal` ("1"|"0") off each `.desk__game-card` (server-set from
 * `row.read`'s truthiness). Walks the card list from the end and counts the
 * contiguous run of zero-signal cards; a card with signal never enters that
 * run once a positive-signal card is hit walking backward, so a
 * positive-signal game can never be hidden regardless of its position.
 * Below 10 total games, or with no trailing zero-signal run, nothing is
 * hidden and no toggle is created -- the slate.html markup stays exactly as
 * server-rendered.
 */
function initSlateDisclosure() {
  var slate = document.getElementById('deskSlate');
  if (!slate) {
    return;
  }
  var grid = slate.querySelector('.desk__slate-grid');
  if (!grid) {
    return;
  }
  var cards = Array.prototype.slice.call(grid.querySelectorAll('.desk__game-card'));
  var total = cards.length;
  if (total < 10) {
    return; // Never collapse slates under 10 games.
  }

  var trailingZeroRun = 0;
  for (var i = cards.length - 1; i >= 0; i--) {
    if (cards[i].dataset.signal === '0') {
      trailingZeroRun++;
    } else {
      break;
    }
  }
  if (trailingZeroRun === 0) {
    return; // No zero-signal tail to collapse.
  }

  // Defensive floor: never hide every card, even in the degenerate case
  // where the whole slate is zero-signal.
  var hiddenCount = Math.min(trailingZeroRun, total - 1);
  if (hiddenCount <= 0) {
    return;
  }

  var tailCards = cards.slice(total - hiddenCount);
  tailCards.forEach(function (card) {
    card.setAttribute('hidden', '');
    card.classList.add('desk__game-card--tail');
  });

  var moreLabel = 'Show all ' + total + ' games';
  var lessLabel = 'Show fewer games';

  var toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'desk__slate-toggle';
  toggle.id = 'deskSlateToggle';
  toggle.setAttribute('aria-expanded', 'false');
  toggle.dataset.more = moreLabel;
  toggle.dataset.less = lessLabel;
  toggle.textContent = moreLabel;
  toggle.addEventListener('click', function () {
    var expanded = toggle.getAttribute('aria-expanded') === 'true';
    var next = !expanded;
    toggle.setAttribute('aria-expanded', String(next));
    toggle.textContent = next ? lessLabel : moreLabel;
    tailCards.forEach(function (card) {
      if (next) {
        card.removeAttribute('hidden');
      } else {
        card.setAttribute('hidden', '');
      }
    });
  });
  slate.appendChild(toggle);
}

/**
 * Wire keyboard-and-click, both-direction, missing-last sorting onto the
 * Class Tracker's numeric columns.
 *
 * Sortable headers are marked with `data-sort-key` (added only to `class_
 * tracker.html`'s numeric `<th>`s -- Player/"vs cohort" are not sort
 * triggers). Each matching `<td>` in a row carries a parallel `data-value`
 * (empty string when the underlying stat is `None`); each `<tr>` carries
 * `data-name` for the tiebreak. Sorting reorders existing `<tr>` nodes in
 * place via `appendChild` -- no request, no navigation, no re-render.
 */
function initTrackerSort() {
  var table = document.querySelector('.desk__tracker-table');
  if (!table) {
    return;
  }
  var thead = table.querySelector('thead');
  var tbody = table.querySelector('tbody');
  if (!thead || !tbody) {
    return;
  }
  var headers = Array.prototype.slice.call(thead.querySelectorAll('th[data-sort-key]'));
  if (headers.length === 0) {
    return;
  }

  headers.forEach(function (th) {
    th.setAttribute('tabindex', '0');
    th.setAttribute('role', 'columnheader');
    if (!th.hasAttribute('aria-sort')) {
      th.setAttribute('aria-sort', 'none');
    }
    th.addEventListener('click', function () {
      sortByHeader(th);
    });
    th.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar') {
        event.preventDefault();
        sortByHeader(th);
      }
    });
  });

  function compareNames(a, b) {
    var an = a.dataset.name || '';
    var bn = b.dataset.name || '';
    return an.localeCompare(bn);
  }

  function sortByHeader(th) {
    var currentDir = th.getAttribute('aria-sort');
    var nextDir = currentDir === 'descending' ? 'ascending' : 'descending';

    headers.forEach(function (h) {
      h.setAttribute('aria-sort', 'none');
      h.classList.remove('desk__tracker-th--active');
    });
    th.setAttribute('aria-sort', nextDir);
    th.classList.add('desk__tracker-th--active');

    var headerCells = Array.prototype.slice.call(th.parentNode.children);
    var colIndex = headerCells.indexOf(th);

    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    rows.sort(function (a, b) {
      var aCell = a.children[colIndex];
      var bCell = b.children[colIndex];
      var aRaw = aCell ? aCell.dataset.value : undefined;
      var bRaw = bCell ? bCell.dataset.value : undefined;
      var aMissing = aRaw === undefined || aRaw === '';
      var bMissing = bRaw === undefined || bRaw === '';

      // Missing values sort last regardless of direction.
      if (aMissing && bMissing) {
        return compareNames(a, b);
      }
      if (aMissing) {
        return 1;
      }
      if (bMissing) {
        return -1;
      }

      var aNum = parseFloat(aRaw);
      var bNum = parseFloat(bRaw);
      if (aNum === bNum) {
        return compareNames(a, b);
      }
      var cmp = aNum < bNum ? -1 : 1;
      return nextDir === 'ascending' ? cmp : -cmp;
    });

    rows.forEach(function (row) {
      tbody.appendChild(row);
    });

    // Re-sorting changes which 15 rows belong on "page 1" -- tell pagination
    // (if it's active) to reset there over the freshly-sorted order.
    table.dispatchEvent(new CustomEvent('desk:tracker-sorted'));
  }
}

/**
 * Cap the Class Tracker's visible rows to `PAGE_SIZE` at a time, paginating
 * client-side over the already-rendered rows (progressive enhancement --
 * same pattern as `initSlateDisclosure`: the server renders every row of the
 * active cohort/stat-view, unhidden, up to the backend's own cap; this only
 * hides rows AFTER the script runs, so JS-disabled users can still reach
 * every row in the flat table).
 *
 * Listens for `desk:tracker-sorted` (dispatched by `initTrackerSort` after
 * every reorder) and resets to page 1 over the new order -- re-sorting
 * re-paginates, per the ticket's requirement that the two features compose.
 */
function initTrackerPagination() {
  var PAGE_SIZE = 15;
  var table = document.querySelector('.desk__tracker-table');
  if (!table) {
    return;
  }
  var tbody = table.querySelector('tbody');
  if (!tbody) {
    return;
  }
  var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
  if (rows.length <= PAGE_SIZE) {
    return; // Nothing to paginate -- every row already fits on one page.
  }

  var currentPage = 0;

  function totalPages(rowCount) {
    return Math.max(1, Math.ceil(rowCount / PAGE_SIZE));
  }

  function showPage(page) {
    var liveRows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    var pages = totalPages(liveRows.length);
    currentPage = Math.max(0, Math.min(page, pages - 1));
    liveRows.forEach(function (row, idx) {
      var onPage =
        idx >= currentPage * PAGE_SIZE && idx < (currentPage + 1) * PAGE_SIZE;
      if (onPage) {
        row.removeAttribute('hidden');
      } else {
        row.setAttribute('hidden', '');
      }
    });
    statusEl.textContent = 'Page ' + (currentPage + 1) + ' of ' + pages;
    prevBtn.disabled = currentPage === 0;
    nextBtn.disabled = currentPage >= pages - 1;
  }

  var pager = document.createElement('div');
  pager.className = 'desk__tracker-pager';

  var prevBtn = document.createElement('button');
  prevBtn.type = 'button';
  prevBtn.className = 'desk__tracker-pager-btn';
  prevBtn.textContent = 'Prev';
  prevBtn.addEventListener('click', function () {
    showPage(currentPage - 1);
  });

  var statusEl = document.createElement('span');
  statusEl.className = 'desk__tracker-pager-status';
  statusEl.setAttribute('aria-live', 'polite');

  var nextBtn = document.createElement('button');
  nextBtn.type = 'button';
  nextBtn.className = 'desk__tracker-pager-btn';
  nextBtn.textContent = 'Next';
  nextBtn.addEventListener('click', function () {
    showPage(currentPage + 1);
  });

  pager.appendChild(prevBtn);
  pager.appendChild(statusEl);
  pager.appendChild(nextBtn);

  var section = document.getElementById('slDeskTracker') || table.closest('.card');
  (section || table.parentNode).appendChild(pager);

  table.addEventListener('desk:tracker-sorted', function () {
    showPage(0);
  });

  showPage(0);
}

/**
 * Fire exactly one `sl_desk_click` gtag event per click on a Desk player
 * link, then let native navigation proceed untouched.
 *
 * A single delegated listener on `document` (not one per link) guarantees
 * "exactly one event per click" -- there is nothing to double-fire. Links
 * outside the Desk (or Desk links without `data-desk-placement`, e.g. the
 * slate/live-board matchup links and the off-window archive link) are
 * untouched and never match the selector, so they fire nothing.
 */
function initDeskClickAnalytics() {
  document.addEventListener('click', function (event) {
    var link = event.target.closest && event.target.closest('a[data-desk-placement]');
    if (!link) {
      return;
    }
    var stateHost = link.closest('[data-desk-daily-state]');
    var dailyState = stateHost ? stateHost.dataset.deskDailyState : 'unknown';

    if (typeof window.gtag === 'function') {
      window.gtag('event', 'sl_desk_click', {
        placement: link.dataset.deskPlacement,
        daily_state: dailyState,
        destination: link.getAttribute('href') || '',
      });
    }
    // No preventDefault -- navigation always proceeds via the anchor's href.
  });
}
