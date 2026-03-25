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
    }
  };

  // Map internal category keys to DATA.categories keys
  var CAT_DATA_KEY = {
    anthro: 'anthro',
    athletic: 'athletic',
    shooting: 'shooting'
  };

  var currentCategory = 'anthro';

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

    renderRangeChart(cat);
    renderWinners(cat);
    renderTable(cat);
  }

  // ═══════════════════════════════════════════════════════════════
  // RANGE CHART
  // ═══════════════════════════════════════════════════════════════

  function renderRangeChart(cat) {
    var catData = DATA.categories[CAT_DATA_KEY[cat]];
    var chart = document.getElementById('dy-range-chart');
    if (!chart) return;

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

    catData.range_stats.forEach(function (rs) {
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
      '</div>';
    });

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

  function renderWinners(cat) {
    var catData = DATA.categories[CAT_DATA_KEY[cat]];
    var grid = document.getElementById('dy-winners-grid');
    if (!grid) return;

    var html = '';
    catData.metrics.forEach(function (metric, i) {
      var leader = catData.leaders[metric.key];
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
  // DATA TABLE
  // ═══════════════════════════════════════════════════════════════

  function renderTable(cat) {
    var catData = DATA.categories[CAT_DATA_KEY[cat]];
    var thead = document.getElementById('dy-table-head');
    var tbody = document.getElementById('dy-table-body');
    if (!thead || !tbody) return;

    // Header
    var headHtml = '<tr><th>#</th><th>Player</th><th>Pos</th>';
    catData.metrics.forEach(function (m) {
      headHtml += '<th class="text-right">' + escHtml(m.label) + '</th>';
    });
    headHtml += '</tr>';
    thead.innerHTML = headHtml;

    // Body
    var bodyHtml = '';
    catData.players.forEach(function (p, i) {
      var photoUrl = p.photo_url_placeholder || '';

      bodyHtml += '<tr data-name="' + escAttr((p.display_name + ' ' + (p.school || '')).toLowerCase()) + '" data-pos="' + escAttr(p.position || '') + '">';
      bodyHtml += '<td>' + (i + 1) + '</td>';
      bodyHtml += '<td><div class="dy-player-cell">' +
        '<img src="' + escAttr(photoUrl) + '" alt="' + escAttr(p.display_name) + '">' +
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

    // Populate position filter
    var posFilter = document.getElementById('dy-pos-filter');
    if (posFilter) {
      var currentVal = posFilter.value;
      var optionsHtml = '<option value="">All Positions</option>';
      DATA.positions.forEach(function (pos) {
        optionsHtml += '<option value="' + escAttr(pos) + '">' + escHtml(pos) + '</option>';
      });
      posFilter.innerHTML = optionsHtml;
      posFilter.value = currentVal;
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // SEARCH & FILTER
  // ═══════════════════════════════════════════════════════════════

  function applyFilters() {
    var searchEl = document.getElementById('dy-grid-search');
    var posEl = document.getElementById('dy-pos-filter');
    var q = searchEl ? searchEl.value.toLowerCase() : '';
    var pos = posEl ? posEl.value : '';

    document.querySelectorAll('#dy-table-body tr').forEach(function (row) {
      var nameMatch = !q || (row.dataset.name || '').indexOf(q) !== -1;
      var posMatch = !pos || row.dataset.pos === pos;
      row.style.display = nameMatch && posMatch ? '' : 'none';
    });
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
    // Tab click handlers
    document.querySelectorAll('.dy-category-tab').forEach(function (tab) {
      tab.addEventListener('click', function () {
        switchCategory(this.dataset.cat);
      });
    });

    // Search & filter handlers
    var searchEl = document.getElementById('dy-grid-search');
    if (searchEl) searchEl.addEventListener('input', applyFilters);

    var posEl = document.getElementById('dy-pos-filter');
    if (posEl) posEl.addEventListener('change', applyFilters);

    // Initial render
    switchCategory('anthro');

    // Reposition avg markers on resize
    window.addEventListener('resize', function () {
      var chart = document.getElementById('dy-range-chart');
      if (chart) positionAvgMarkers(chart);
    });
  });
})();
