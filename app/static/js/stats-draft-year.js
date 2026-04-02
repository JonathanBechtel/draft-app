/**
 * Draft Year Combine Stats — Category switching, range chart, winners, data table.
 *
 * Reads from window.DRAFT_YEAR_DATA injected by the template.
 */
(function () {
  'use strict';

  var DATA = window.DRAFT_YEAR_DATA;
  if (!DATA) return;

  var CATEGORY_COLORS = {
    anthro: {
      color: '#06b6d4',
      colorLight: '#ecfeff',
      colorMuted: 'rgba(6,182,212,0.15)',
      colorMid: 'rgba(6,182,212,0.35)'
    },
    athletic: {
      color: '#f59e0b',
      colorLight: '#fffbeb',
      colorMuted: 'rgba(245,158,11,0.15)',
      colorMid: 'rgba(245,158,11,0.35)'
    },
    shooting: {
      color: '#f43f5e',
      colorLight: '#fff1f2',
      colorMuted: 'rgba(244,63,94,0.15)',
      colorMid: 'rgba(244,63,94,0.35)'
    },
    combine_scores: {
      color: '#6366f1',
      colorLight: '#eef2ff',
      colorMuted: 'rgba(99,102,241,0.15)',
      colorMid: 'rgba(99,102,241,0.35)'
    }
  };

  // Map internal category keys to DATA.categories keys
  var CAT_DATA_KEY = {
    anthro: 'anthro',
    athletic: 'athletic',
    shooting: 'shooting'
  };

  var TAB_CONFIG = [
    { key: 'anthro',          icon: '\uD83D\uDCCF', label: 'Anthro' },
    { key: 'athletic',        icon: '\u26A1',        label: 'Athletic' },
    { key: 'shooting',        icon: '\uD83C\uDFAF',  label: 'Shooting' },
    { key: 'combine_scores',  icon: '\uD83C\uDFC6',  label: 'Combine Scores' }
  ];

  var availableCategories = TAB_CONFIG.filter(function (t) {
    if (t.key === 'combine_scores') {
      return DATA.combine_scores && DATA.combine_scores.length > 0;
    }
    var cat = DATA.categories[t.key];
    return cat && cat.players && cat.players.length > 0;
  });

  var currentCategory = availableCategories.length > 0 ? availableCategories[0].key : 'anthro';

  // ═══════════════════════════════════════════════════════════════
  // FORMAT HELPERS
  // ═══════════════════════════════════════════════════════════════

  function fmtVal(val, unit) {
    if (val == null) return '\u2014';
    if (unit === '%') return val.toFixed(1);
    if (unit === 's') return val.toFixed(2);
    if (unit === 'reps') return String(Math.round(val));
    return Number.isInteger(val) ? String(val) : val.toFixed(2);
  }

  function fmtWithUnit(val, unit) {
    if (val == null) return '\u2014';
    var v = fmtVal(val, unit);
    if (unit === 'in') return v + '"';
    if (unit === 'lbs') return v + ' lbs';
    if (unit === '%') return v + '%';
    if (unit === 's') return v + 's';
    if (unit === 'reps') return v + ' reps';
    return v;
  }

  // ═══════════════════════════════════════════════════════════════
  // CATEGORY SWITCHING
  // ═══════════════════════════════════════════════════════════════

  function renderTabs() {
    var container = document.getElementById('dy-category-tabs');
    if (!container) return;
    var html = '';
    availableCategories.forEach(function (t) {
      html += '<button class="dy-category-tab" data-cat="' + t.key + '">' +
        '<span class="dy-tab-icon">' + t.icon + '</span> ' + t.label +
        '</button>';
    });
    container.innerHTML = html;
  }

  function switchCategory(cat) {
    currentCategory = cat;
    var cfg = CATEGORY_COLORS[cat];
    var main = document.querySelector('.dy-main');
    if (!main) return;

    main.style.setProperty('--cat-color', cfg.color);
    main.style.setProperty('--cat-color-light', cfg.colorLight);
    main.style.setProperty('--cat-color-muted', cfg.colorMuted);
    main.style.setProperty('--cat-color-mid', cfg.colorMid);

    document.querySelectorAll('.dy-category-tab').forEach(function (t) {
      t.classList.toggle('active', t.dataset.cat === cat);
    });

    var rangeSection = document.getElementById('dy-range-section');
    var winnersSection = document.getElementById('dy-winners-section');
    var isCombine = cat === 'combine_scores';

    if (rangeSection) rangeSection.style.display = isCombine ? 'none' : '';
    if (winnersSection) winnersSection.style.display = isCombine ? 'none' : '';

    if (isCombine) {
      renderCombineTable();
    } else {
      renderRangeChart(cat);
      renderWinners(cat);
      renderTable(cat);
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // RANGE CHART
  // ═══════════════════════════════════════════════════════════════

  function computeRangeStats(players, metrics) {
    var stats = [];
    metrics.forEach(function (m) {
      var entries = [];
      players.forEach(function (p) {
        var v = p.metrics[m.key];
        if (v != null) entries.push({ val: v, name: p.display_name, formatted: p.formatted[m.key] });
      });
      if (entries.length < 2) return;

      var vals = entries.map(function (e) { return e.val; });
      var minVal = Math.min.apply(null, vals);
      var maxVal = Math.max.apply(null, vals);
      var avg = vals.reduce(function (a, b) { return a + b; }, 0) / vals.length;

      var minE = entries.filter(function (e) { return e.val === minVal; })[0];
      var maxE = entries.filter(function (e) { return e.val === maxVal; })[0];

      stats.push({
        display_name: m.label,
        formatted_min: minE.formatted || fmtVal(minVal, m.unit),
        formatted_max: maxE.formatted || fmtVal(maxVal, m.unit),
        formatted_avg: fmtVal(avg, m.unit),
        min_player_name: minE.name,
        max_player_name: maxE.name,
        min_value: minVal,
        max_value: maxVal,
        avg_value: avg
      });
    });
    return stats;
  }

  function renderRangeChart(cat) {
    var catData = DATA.categories[CAT_DATA_KEY[cat]];
    var chart = document.getElementById('dy-range-chart');
    if (!chart) return;

    // Filter players by range position dropdown
    var rangePosEl = document.getElementById('dy-range-pos-filter');
    var rangePos = rangePosEl ? rangePosEl.value : '';
    var pool = rangePos
      ? catData.players.filter(function (p) { return p.position === rangePos; })
      : catData.players;

    // Use pre-computed stats when unfiltered, recompute when filtered
    var rangeStats = rangePos
      ? computeRangeStats(pool, catData.metrics)
      : catData.range_stats;

    var html = '<div class="dy-range-chart-header">' +
      '<div class="dy-section-title" style="margin-bottom:0;border-left-color:var(--cat-color)">Distribution Overview</div>' +
      '<div class="dy-range-chart-legend">' +
        '<div class="dy-legend-item"><div class="dy-legend-dot dy-legend-dot--range"></div> Class Range</div>' +
        '<div class="dy-legend-item"><div class="dy-legend-dot dy-legend-dot--avg"></div> Class Average</div>' +
      '</div></div>';

    // Column headers
    html += '<div class="dy-range-col-headers">' +
      '<div class="dy-range-col-header left">Min</div>' +
      '<div class="dy-range-col-header" style="text-align:center">Range</div>' +
      '<div class="dy-range-col-header right">Max</div>' +
    '</div>';

    rangeStats.forEach(function (rs) {
      var span = rs.max_value - rs.min_value || 1;
      var avgPct = ((rs.avg_value - rs.min_value) / span * 100).toFixed(1);

      html += '<div class="dy-range-item">' +
        '<div class="dy-range-item-label">' + escHtml(rs.display_name) + '</div>' +
        '<div class="dy-range-row">' +
          '<div class="dy-range-endpoint left">' +
            '<div class="dy-ep-value">' + rs.formatted_min + '</div>' +
            '<div class="dy-ep-name">' + escHtml(rs.min_player_name) + '</div>' +
          '</div>' +
          '<div class="dy-range-track">' +
            '<div class="dy-range-bar"></div>' +
            '<div class="dy-range-avg" data-pct="' + avgPct + '">' +
              '<div class="dy-range-avg-label">AVG ' + rs.formatted_avg + '</div>' +
            '</div>' +
          '</div>' +
          '<div class="dy-range-endpoint right">' +
            '<div class="dy-ep-value">' + rs.formatted_max + '</div>' +
            '<div class="dy-ep-name">' + escHtml(rs.max_player_name) + '</div>' +
          '</div>' +
        '</div>' +
        '<div class="dy-range-endpoints-row">' +
          '<div class="dy-range-endpoint-mobile left">' +
            '<div class="dy-ep-value">' + rs.formatted_min + '</div>' +
            '<div class="dy-ep-name">' + escHtml(rs.min_player_name) + '</div>' +
          '</div>' +
          '<div class="dy-range-endpoint-mobile right">' +
            '<div class="dy-ep-value">' + rs.formatted_max + '</div>' +
            '<div class="dy-ep-name">' + escHtml(rs.max_player_name) + '</div>' +
          '</div>' +
        '</div>' +
      '</div>';
    });

    if (rangeStats.length === 0) {
      html += '<div style="text-align:center;padding:2rem;color:var(--color-slate-500);font-family:var(--font-mono);font-size:0.8rem;">No data for this position</div>';
    }

    chart.innerHTML = html;
    positionAvgMarkers(chart);
  }

  function positionAvgMarkers(chart) {
    chart.querySelectorAll('.dy-range-avg').forEach(function (el) {
      var track = el.parentElement;
      var trackWidth = track.clientWidth;
      var padded = trackWidth - 8;
      var pct = parseFloat(el.dataset.pct) || 50;
      el.style.left = (4 + pct / 100 * padded) + 'px';
    });
  }

  // ═══════════════════════════════════════════════════════════════
  // WINNER SHOWCASE
  // ═══════════════════════════════════════════════════════════════

  function getActivePositionFilter() {
    var posEl = document.getElementById('dy-pos-filter');
    return posEl ? posEl.value : '';
  }

  function findLeaderForMetric(players, metricKey, sortDirection) {
    var best = null;
    var bestVal = null;
    for (var i = 0; i < players.length; i++) {
      var v = players[i].metrics[metricKey];
      if (v == null) continue;
      if (bestVal == null ||
          (sortDirection === 'desc' && v > bestVal) ||
          (sortDirection === 'asc' && v < bestVal)) {
        bestVal = v;
        best = players[i];
      }
    }
    return best;
  }

  function renderWinners(cat) {
    var catData = DATA.categories[CAT_DATA_KEY[cat]];
    var grid = document.getElementById('dy-winners-grid');
    if (!grid) return;

    var pos = getActivePositionFilter();
    var pool = pos
      ? catData.players.filter(function (p) { return p.position === pos; })
      : catData.players;

    var html = '';
    catData.metrics.forEach(function (metric, i) {
      var leader = pos
        ? findLeaderForMetric(pool, metric.key, metric.sort_direction)
        : catData.leaders[metric.key];
      if (!leader) return;

      var val = leader.metrics[metric.key];
      var fmtd = leader.formatted[metric.key] || fmtWithUnit(val, metric.unit);
      var photoUrl = leader.photo_url || '';
      var placeholderUrl = leader.photo_url_placeholder || '';

      html += '<a href="/players/' + escAttr(leader.slug) + '" class="dy-winner-card" style="animation-delay:' + (i * 0.05) + 's">' +
        '<div class="dy-winner-badge">&#x1F451;</div>' +
        '<div class="dy-winner-photo-wrap">' +
          '<img class="dy-winner-photo" src="' + escAttr(photoUrl) + '" alt="' + escAttr(leader.display_name) + '"' +
          ' onerror="this.onerror=null;this.src=\'' + escAttr(placeholderUrl) + '\';" loading="lazy">' +
          '<div class="dy-winner-stat-ribbon"><div>' +
            '<div class="dy-winner-stat-label">' + escHtml(metric.label) + '</div>' +
            '<div class="dy-winner-stat-value">' + fmtd + '</div>' +
          '</div></div>' +
        '</div>' +
        '<div class="dy-winner-info">' +
          '<div class="dy-winner-name">' + escHtml(leader.display_name) + '</div>' +
          '<div class="dy-winner-meta">' + escHtml(leader.school || '') + ' &middot; ' + escHtml(leader.position || '') + '</div>' +
        '</div>' +
      '</a>';
    });

    grid.innerHTML = html;
  }

  // ═══════════════════════════════════════════════════════════════
  // DATA TABLE (with pagination)
  // ═══════════════════════════════════════════════════════════════

  var PAGE_SIZE = 25;
  var tablePage = 0;
  var filteredPlayers = [];
  var sortKey = null;   // null, 'name', 'pos', or a metric key
  var sortDir = 'asc';  // 'asc' or 'desc'

  function populatePositionFilter(el) {
    if (!el) return;
    var currentVal = el.value;
    var html = '<option value="">All Positions</option>';
    DATA.positions.forEach(function (pos) {
      html += '<option value="' + escAttr(pos) + '">' + escHtml(pos) + '</option>';
    });
    el.innerHTML = html;
    el.value = currentVal;
  }

  function sortArrow(key) {
    if (sortKey !== key) return ' <span class="dy-sort-arrow">\u25B4</span>';
    return sortDir === 'asc'
      ? ' <span class="dy-sort-arrow active">\u25B4</span>'
      : ' <span class="dy-sort-arrow active">\u25BE</span>';
  }

  function handleSort(key) {
    if (sortKey === key) {
      sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      sortKey = key;
      // Default sort direction: desc for metrics, asc for name/pos
      sortDir = (key === 'name' || key === 'pos') ? 'asc' : 'desc';
    }
    sortFilteredPlayers();
    tablePage = 0;
    renderTablePage();
    updateSortHeaders();
  }

  function sortFilteredPlayers() {
    if (!sortKey) return;
    var key = sortKey;
    var dir = sortDir === 'asc' ? 1 : -1;

    filteredPlayers.sort(function (a, b) {
      var va, vb;
      if (key === 'name') {
        va = (a.display_name || '').toLowerCase();
        vb = (b.display_name || '').toLowerCase();
        return va < vb ? -dir : va > vb ? dir : 0;
      } else if (key === 'pos') {
        va = (a.position || '').toLowerCase();
        vb = (b.position || '').toLowerCase();
        return va < vb ? -dir : va > vb ? dir : 0;
      } else {
        va = a.metrics[key];
        vb = b.metrics[key];
        if (va == null && vb == null) return 0;
        if (va == null) return 1;
        if (vb == null) return -1;
        return (va - vb) * dir;
      }
    });
  }

  function updateSortHeaders() {
    var thead = document.getElementById('dy-table-head');
    if (!thead) return;
    var catData = DATA.categories[CAT_DATA_KEY[currentCategory]];

    var headHtml = '<tr><th>#</th>';
    headHtml += '<th class="dy-sortable" data-sort="name">Player' + sortArrow('name') + '</th>';
    headHtml += '<th class="dy-sortable" data-sort="pos">Pos' + sortArrow('pos') + '</th>';
    catData.metrics.forEach(function (m) {
      headHtml += '<th class="text-right dy-sortable" data-sort="' + escAttr(m.key) + '">' + escHtml(m.label) + sortArrow(m.key) + '</th>';
    });
    headHtml += '</tr>';
    thead.innerHTML = headHtml;
  }

  function renderTable(cat) {
    var catData = DATA.categories[CAT_DATA_KEY[cat]];
    var thead = document.getElementById('dy-table-head');
    if (!thead) return;

    // Reset sort on category switch
    sortKey = null;
    sortDir = 'asc';

    // Build header with sortable columns
    var headHtml = '<tr><th>#</th>';
    headHtml += '<th class="dy-sortable" data-sort="name">Player' + sortArrow('name') + '</th>';
    headHtml += '<th class="dy-sortable" data-sort="pos">Pos' + sortArrow('pos') + '</th>';
    catData.metrics.forEach(function (m) {
      headHtml += '<th class="text-right dy-sortable" data-sort="' + escAttr(m.key) + '">' + escHtml(m.label) + sortArrow(m.key) + '</th>';
    });
    headHtml += '</tr>';
    thead.innerHTML = headHtml;

    // Populate both position filters
    populatePositionFilter(document.getElementById('dy-pos-filter'));
    populatePositionFilter(document.getElementById('dy-range-pos-filter'));

    tablePage = 0;
    applyFilters(false);
  }

  function applyFilters(updateWinners) {
    var catData = DATA.categories[CAT_DATA_KEY[currentCategory]];
    var searchEl = document.getElementById('dy-grid-search');
    var posEl = document.getElementById('dy-pos-filter');
    var q = searchEl ? searchEl.value.toLowerCase() : '';
    var pos = posEl ? posEl.value : '';

    filteredPlayers = catData.players.filter(function (p) {
      var nameMatch = !q || (p.display_name + ' ' + (p.school || '')).toLowerCase().indexOf(q) !== -1;
      var posMatch = !pos || p.position === pos;
      return nameMatch && posMatch;
    });

    tablePage = 0;
    renderTablePage();

    if (updateWinners) {
      renderWinners(currentCategory);
    }
  }

  function renderTablePage() {
    var catData = DATA.categories[CAT_DATA_KEY[currentCategory]];
    var tbody = document.getElementById('dy-table-body');
    if (!tbody) return;

    var start = tablePage * PAGE_SIZE;
    var pagePlayers = filteredPlayers.slice(start, start + PAGE_SIZE);
    var totalPages = Math.ceil(filteredPlayers.length / PAGE_SIZE);

    var bodyHtml = '';
    pagePlayers.forEach(function (p, i) {
      var photoUrl = p.photo_url || '';
      var placeholderUrl = p.photo_url_placeholder || '';

      bodyHtml += '<tr>';
      bodyHtml += '<td>' + (start + i + 1) + '</td>';
      bodyHtml += '<td><div class="dy-player-cell">' +
        '<img src="' + escAttr(photoUrl) + '" alt="' + escAttr(p.display_name) + '"' +
        ' onerror="this.onerror=null;this.src=\'' + escAttr(placeholderUrl) + '\';">' +
        '<div><div class="name"><a href="/players/' + escAttr(p.slug) + '">' + escHtml(p.display_name) + '</a></div>' +
        '<div class="school">' + escHtml(p.school || '') + '</div></div>' +
        '</div></td>';
      bodyHtml += '<td>' + escHtml(p.position || '') + '</td>';

      catData.metrics.forEach(function (m) {
        var val = p.metrics[m.key];
        var formatted = p.formatted[m.key];
        var pctl = p.percentiles[m.key];

        if (val == null) {
          bodyHtml += '<td class="text-right">&mdash;</td>';
          return;
        }
        var pctWidth = pctl != null ? Math.round(pctl) : 0;
        bodyHtml += '<td class="text-right"><div class="dy-pct-cell">' +
          '<span class="tabular-nums" style="min-width:42px;text-align:right">' + (formatted || fmtVal(val, m.unit)) + '</span>' +
          '<div class="dy-pct-bar-bg" style="width:50px"><div class="dy-pct-bar-fill" style="width:' + pctWidth + '%"></div></div>' +
          '</div></td>';
      });

      bodyHtml += '</tr>';
    });
    tbody.innerHTML = bodyHtml;

    // Pagination controls
    renderPagination(totalPages);
  }

  function renderPagination(totalPages) {
    var existing = document.getElementById('dy-pagination');
    if (existing) existing.remove();

    if (totalPages <= 1) return;

    var wrap = document.querySelector('.dy-data-table-wrap');
    if (!wrap) return;

    var html = '<div id="dy-pagination" class="dy-pagination">';
    html += '<button class="dy-page-btn" data-page="prev"' + (tablePage === 0 ? ' disabled' : '') + '>&laquo; Prev</button>';

    for (var i = 0; i < totalPages; i++) {
      html += '<button class="dy-page-btn' + (i === tablePage ? ' active' : '') + '" data-page="' + i + '">' + (i + 1) + '</button>';
    }

    html += '<button class="dy-page-btn" data-page="next"' + (tablePage >= totalPages - 1 ? ' disabled' : '') + '>Next &raquo;</button>';
    html += '<span class="dy-page-info">' + filteredPlayers.length + ' players</span>';
    html += '</div>';

    wrap.insertAdjacentHTML('afterend', html);

    document.getElementById('dy-pagination').addEventListener('click', function (e) {
      var btn = e.target.closest('.dy-page-btn');
      if (!btn || btn.disabled) return;
      var page = btn.dataset.page;
      var maxPage = Math.ceil(filteredPlayers.length / PAGE_SIZE) - 1;
      if (page === 'prev') tablePage = Math.max(0, tablePage - 1);
      else if (page === 'next') tablePage = Math.min(maxPage, tablePage + 1);
      else tablePage = parseInt(page, 10);
      renderTablePage();
    });
  }

  // ═══════════════════════════════════════════════════════════════
  // COMBINE SCORES TABLE
  // ═══════════════════════════════════════════════════════════════

  var combinePage = 0;
  var filteredCombine = [];
  var combineSortKey = 'overall_rank';
  var combineSortDir = 'asc';

  function pctlTier(pctl) {
    if (pctl == null) return 'none';
    if (pctl >= 90) return 'elite';
    if (pctl >= 75) return 'good';
    if (pctl >= 40) return 'average';
    return 'below-average';
  }

  function gradeLabel(pctl) {
    if (pctl == null) return 'N/A';
    if (pctl >= 90) return 'Elite';
    if (pctl >= 75) return 'Above Avg';
    if (pctl >= 40) return 'Average';
    if (pctl >= 20) return 'Below Avg';
    return 'Poor';
  }

  function pctlPill(pctl) {
    if (pctl == null) return '<span class="pctl-pill none">&mdash;</span>';
    return '<span class="pctl-pill ' + pctlTier(pctl) + '">' + Math.round(pctl) + '</span>';
  }

  function combineSortArrow(key) {
    if (combineSortKey !== key) return ' <span class="dy-sort-arrow">\u25B4</span>';
    return combineSortDir === 'asc'
      ? ' <span class="dy-sort-arrow active">\u25B4</span>'
      : ' <span class="dy-sort-arrow active">\u25BE</span>';
  }

  function handleCombineSort(key) {
    if (combineSortKey === key) {
      combineSortDir = combineSortDir === 'asc' ? 'desc' : 'asc';
    } else {
      combineSortKey = key;
      combineSortDir = (key === 'player_name') ? 'asc' : 'desc';
    }
    sortFilteredCombine();
    combinePage = 0;
    renderCombinePage();
    updateCombineHeaders();
  }

  function sortFilteredCombine() {
    var key = combineSortKey;
    var dir = combineSortDir === 'asc' ? 1 : -1;
    filteredCombine.sort(function (a, b) {
      if (key === 'player_name') {
        var va = (a.player_name || '').toLowerCase();
        var vb = (b.player_name || '').toLowerCase();
        return va < vb ? -dir : va > vb ? dir : 0;
      }
      var valA = a[key];
      var valB = b[key];
      if (valA == null && valB == null) return 0;
      if (valA == null) return 1;
      if (valB == null) return -1;
      return (valA - valB) * dir;
    });
  }

  function applyCombineFilters() {
    var searchEl = document.getElementById('dy-grid-search');
    var posEl = document.getElementById('dy-pos-filter');
    var q = searchEl ? searchEl.value.toLowerCase() : '';
    var pos = posEl ? posEl.value : '';

    filteredCombine = (DATA.combine_scores || []).filter(function (p) {
      var nameMatch = !q || ((p.player_name || '') + ' ' + (p.school || '')).toLowerCase().indexOf(q) !== -1;
      var posMatch = !pos || p.position === pos;
      return nameMatch && posMatch;
    });

    sortFilteredCombine();
    combinePage = 0;
    renderCombinePage();
  }

  function updateCombineHeaders() {
    var thead = document.getElementById('dy-table-head');
    if (!thead) return;
    var headHtml = '<tr>';
    headHtml += '<th class="dy-sortable" data-csort="overall_rank">#' + combineSortArrow('overall_rank') + '</th>';
    headHtml += '<th class="dy-sortable" data-csort="player_name">Player' + combineSortArrow('player_name') + '</th>';
    headHtml += '<th class="text-center dy-sortable" data-csort="overall_percentile">Overall' + combineSortArrow('overall_percentile') + '</th>';
    headHtml += '<th class="text-center dy-sortable" data-csort="anthro_percentile">Anthro' + combineSortArrow('anthro_percentile') + '</th>';
    headHtml += '<th class="text-center dy-sortable" data-csort="athletic_percentile">Athletic' + combineSortArrow('athletic_percentile') + '</th>';
    headHtml += '<th class="text-center dy-sortable" data-csort="shooting_percentile">Shooting' + combineSortArrow('shooting_percentile') + '</th>';
    headHtml += '<th class="text-center">Grade</th>';
    headHtml += '</tr>';
    thead.innerHTML = headHtml;
  }

  function renderCombineTable() {
    var thead = document.getElementById('dy-table-head');
    if (!thead) return;

    combineSortKey = 'overall_rank';
    combineSortDir = 'asc';

    updateCombineHeaders();

    // Populate position filter
    populatePositionFilter(document.getElementById('dy-pos-filter'));

    applyCombineFilters();
  }

  function renderCombinePage() {
    var tbody = document.getElementById('dy-table-body');
    if (!tbody) return;

    var start = combinePage * PAGE_SIZE;
    var pagePlayers = filteredCombine.slice(start, start + PAGE_SIZE);
    var totalPages = Math.ceil(filteredCombine.length / PAGE_SIZE);

    var html = '';
    pagePlayers.forEach(function (p) {
      html += '<tr>';
      html += '<td>' + (p.overall_rank || '&mdash;') + '</td>';
      html += '<td><div class="dy-player-cell">' +
        '<div><div class="name"><a href="/players/' + escAttr(p.player_slug) + '">' + escHtml(p.player_name) + '</a></div>' +
        '<div class="school">' + escHtml((p.position || '') + (p.position && p.school ? ' \u00B7 ' : '') + (p.school || '')) + '</div></div>' +
        '</div></td>';
      html += '<td class="text-center">' + pctlPill(p.overall_percentile) + '</td>';
      html += '<td class="text-center">' + pctlPill(p.anthro_percentile) + '</td>';
      html += '<td class="text-center">' + pctlPill(p.athletic_percentile) + '</td>';
      html += '<td class="text-center">' + pctlPill(p.shooting_percentile) + '</td>';
      html += '<td class="text-center"><span class="pctl-pill ' + pctlTier(p.overall_percentile) + '">' + escHtml(gradeLabel(p.overall_percentile)) + '</span></td>';
      html += '</tr>';
    });

    if (pagePlayers.length === 0) {
      html = '<tr><td colspan="7" style="text-align:center;padding:2rem;color:var(--color-slate-500);font-family:var(--font-mono);font-size:0.8rem;">No combine score data</td></tr>';
    }

    tbody.innerHTML = html;
    renderPagination(totalPages);
  }

  // ═══════════════════════════════════════════════════════════════
  // UTILITY
  // ═══════════════════════════════════════════════════════════════

  function escHtml(s) {
    if (!s) return '';
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function escAttr(s) {
    if (!s) return '';
    return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ═══════════════════════════════════════════════════════════════
  // INIT
  // ═══════════════════════════════════════════════════════════════

  document.addEventListener('DOMContentLoaded', function () {
    // Build tabs from available categories
    renderTabs();

    // Tab click handlers
    document.querySelectorAll('.dy-category-tab').forEach(function (tab) {
      tab.addEventListener('click', function () {
        switchCategory(this.dataset.cat);
      });
    });

    // Search only filters the table
    var searchEl = document.getElementById('dy-grid-search');
    if (searchEl) searchEl.addEventListener('input', function () {
      if (currentCategory === 'combine_scores') { applyCombineFilters(); }
      else { applyFilters(false); }
    });

    // Table position filter updates winners and table
    var posEl = document.getElementById('dy-pos-filter');
    if (posEl) posEl.addEventListener('change', function () {
      if (currentCategory === 'combine_scores') { applyCombineFilters(); }
      else { applyFilters(true); }
    });

    // Range position filter updates range chart
    var rangePosEl = document.getElementById('dy-range-pos-filter');
    if (rangePosEl) rangePosEl.addEventListener('change', function () {
      renderRangeChart(currentCategory);
    });

    // Sort by clicking column headers (event delegation on thead)
    var thead = document.getElementById('dy-table-head');
    if (thead) thead.addEventListener('click', function (e) {
      var th = e.target.closest('.dy-sortable');
      if (!th) return;
      if (currentCategory === 'combine_scores' && th.dataset.csort) {
        handleCombineSort(th.dataset.csort);
      } else if (th.dataset.sort) {
        handleSort(th.dataset.sort);
      }
    });

    // Initial render — first available category
    switchCategory(currentCategory);

    // Reposition avg markers on resize
    window.addEventListener('resize', function () {
      var chart = document.getElementById('dy-range-chart');
      if (chart) positionAvgMarkers(chart);
    });
  });

  // ═══════════════════════════════════════════════════════════════
  // SHARE CARD EXPORTS
  // ═══════════════════════════════════════════════════════════════

  var CATEGORY_LABELS = {
    anthro: 'Measurements',
    athletic: 'Athletic Testing',
    shooting: 'Shooting',
    combine_scores: 'Combine Scores'
  };

  function getDraftYearContext() {
    var posEl = document.getElementById('dy-pos-filter');
    var position = posEl && posEl.value ? posEl.value : null;
    return {
      _raw: {
        year: DATA.year,
        category: currentCategory,
        position: position,
      },
      redirectPath: window.location.pathname,
    };
  }

  window.exportDraftYear = function () {
    if (typeof ExportModal === 'undefined') return;
    var context = getDraftYearContext();
    ExportModal.export('draft_year', [], context);
  };

  window.shareDraftYearTweet = function () {
    if (typeof TweetShare === 'undefined') return;
    var context = getDraftYearContext();
    var catLabel = CATEGORY_LABELS[currentCategory] || 'Combine';
    var headline = 'DraftGuru: ' + DATA.year + ' Combine — ' + catLabel + ' Leaders';
    TweetShare.share({
      component: 'draft_year',
      playerIds: [],
      context: context,
      text: headline,
      pageUrl: window.location.href,
    });
  };
})();
