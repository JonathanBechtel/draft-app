/**
 * ============================================================================
 * PLAYER-DETAIL.JS - Player Detail Page JavaScript Modules
 * All interactive functionality and data rendering for the player detail page
 * ============================================================================
 */

/**
 * ============================================================================
 * SCOREBOARD MODULE
 * Populates the sports scoreboard metrics from player data
 * ============================================================================
 */
const ScoreboardModule = {
  /**
   * Initialize the scoreboard with player data
   */
  init() {
    if (!window.PLAYER_DATA) return;
    this.populatePlayerData();
  },

  /**
   * Populate all scoreboard elements from player data
   */
  populatePlayerData() {
    const player = window.PLAYER_DATA;
    const metrics = player.metrics;

    const clean = (val) => {
      if (val === null || val === undefined) return '';
      const text = String(val).trim();
      if (!text) return '';
      if (['null', 'none'].includes(text.toLowerCase())) return '';
      return text;
    };

    // Update page header
    const pageHeader = document.getElementById('pageHeader');
    if (pageHeader) {
      pageHeader.textContent = `${player.name} — Player Profile`;
    }

    // Update player photo
    const playerPhoto = document.getElementById('playerPhoto');
    if (playerPhoto) {
      playerPhoto.src = player.photo_url;
      playerPhoto.alt = player.name;
    }

    // Update primary meta
    const playerPrimaryMeta = document.getElementById('playerPrimaryMeta');
    if (playerPrimaryMeta) {
      const parts = [
        clean(player.position),
        clean(player.college),
        clean(player.height),
        clean(player.weight),
      ].filter(Boolean);
      playerPrimaryMeta.textContent = parts.length ? parts.join(' • ') : 'Bio information unavailable';
    }

    // Update secondary meta
    const updates = {
      'playerAge': clean(player.age),
      'playerClass': clean(player.class),
      'playerHometown': clean(player.hometown),
      'playerWingspan': clean(player.wingspan)
    };

    Object.keys(updates).forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.textContent = updates[id];
    });

    // Update scoreboard metrics
    const consensusRank = document.getElementById('consensusRank');
    if (consensusRank) consensusRank.textContent = metrics.consensusRank;

    const consensusChange = document.getElementById('consensusChange');
    if (consensusChange && metrics.consensusChange !== undefined) {
      const isPositive = metrics.consensusChange > 0;
      consensusChange.className = `change-indicator ${isPositive ? 'positive' : metrics.consensusChange < 0 ? 'negative' : 'neutral'}`;
      consensusChange.innerHTML = `
        <span>${isPositive ? '▲' : metrics.consensusChange < 0 ? '▼' : '='}</span>
        <span>${Math.abs(metrics.consensusChange)}</span>
      `;
    }

    const buzzScore = document.getElementById('buzzScore');
    if (buzzScore) buzzScore.textContent = metrics.buzzScore;

    const buzzFill = document.getElementById('buzzFill');
    if (buzzFill) buzzFill.style.width = `${metrics.buzzScore}%`;

    const truePosition = document.getElementById('truePosition');
    if (truePosition) truePosition.textContent = metrics.truePosition;

    const trueRange = document.getElementById('trueRange');
    if (trueRange) trueRange.textContent = `± ${metrics.trueRange}`;

    const winsAdded = document.getElementById('winsAdded');
    if (winsAdded) winsAdded.textContent = `+${metrics.winsAdded}`;

    const trendIndicator = document.getElementById('trendIndicator');
    if (trendIndicator) {
      const isRising = metrics.trendDirection === 'rising';
      trendIndicator.className = `change-indicator ${isRising ? 'positive' : 'negative'}`;
    }
  }
};

/**
 * ============================================================================
 * PERFORMANCE MODULE
 * Renders percentile bars with cohort/category filtering
 * ============================================================================
 */
const PerformanceModule = {
  currentCategory: 'anthropometrics',
  currentCohort: 'currentDraft',
  positionAdjusted: true,
  cache: {},

  /**
   * Initialize performance module
   */
  init() {
    const perfContainer = document.getElementById('perfBarsContainer');
    if (!perfContainer || !window.PLAYER_DATA) return;

    this.setupEventListeners();
    this.fetchAndRender();
  },

  /**
   * Setup event listeners for controls
   */
  setupEventListeners() {
    // Cohort selector
    const cohortSelect = document.getElementById('perfCohort');
    if (cohortSelect) {
      cohortSelect.addEventListener('change', (e) => {
        this.currentCohort = e.target.value;
        this.fetchAndRender();
      });
    }

    // Position checkbox
    const positionCheckbox = document.getElementById('perfPositionAdjusted');
    if (positionCheckbox) {
      positionCheckbox.addEventListener('change', (e) => {
        this.positionAdjusted = e.target.checked;
        this.fetchAndRender();
      });
    }

    // Category tabs
    const tabs = document.querySelectorAll('.perf-tab');
    tabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        tabs.forEach((t) => t.classList.remove('active'));
        tab.classList.add('active');
        this.currentCategory = tab.dataset.category;
        this.fetchAndRender();
      });
    });
  },

  /**
   * Map UI values to API query params
   */
  mapCohort() {
    const map = {
      currentDraft: 'current_draft',
      historical: 'all_time_draft',
      nbaPlayers: 'current_nba',
      allTimeNba: 'all_time_nba',
    };
    return map[this.currentCohort] || 'current_draft';
  },

  mapCategory() {
    const map = {
      anthropometrics: 'anthropometrics',
      combinePerformance: 'combine_performance',
      advancedStats: 'advanced_stats',
    };
    return map[this.currentCategory] || 'anthropometrics';
  },

  cacheKey() {
    return `${this.mapCohort()}|${this.mapCategory()}|${this.positionAdjusted}`;
  },

  /**
   * Fetch metrics from API (with simple caching)
   */
  async fetchAndRender() {
    const key = this.cacheKey();
    if (this.cache[key]) {
      this.renderPercentiles(this.cache[key]);
      return;
    }

    const slug = window.PLAYER_DATA.slug;
    const params = new URLSearchParams({
      cohort: this.mapCohort(),
      category: this.mapCategory(),
      position_adjusted: this.positionAdjusted ? 'true' : 'false',
    });

    try {
      const response = await fetch(`/api/players/${encodeURIComponent(slug)}/metrics?${params.toString()}`);
      if (!response.ok) {
        this.renderPercentiles({ metrics: [] });
        return;
      }
      const data = await response.json();
      this.cache[key] = data;
      this.renderPercentiles(data);
    } catch (err) {
      console.error('Failed to load metrics', err);
      this.renderPercentiles({ metrics: [] });
    }
  },

  /**
   * Get percentile class based on value
   */
  getPercentileClass(value) {
    if (value >= 90) return 'elite';
    if (value >= 70) return 'good';
    if (value >= 40) return 'average';
    return 'below-average';
  },

  /**
   * Render percentile bars
   */
  renderPercentiles(data) {
    const container = document.getElementById('perfBarsContainer');
    if (!container) return;

    const rows = (data && data.metrics) || [];
    const populationSize = data && data.population_size ? data.population_size : null;

    if (!rows.length) {
      container.innerHTML = '<div class="empty-state">No metrics available for this view.</div>';
      return;
    }

    // Header row
    let html = `
      <div class="perf-header-row">
        <span class="perf-header-label">Percentile</span>
        <span class="perf-header-label">Value</span>
      </div>
    `;

    html += rows.map((item) => {
      const percentileValue = item.percentile ?? 0;
      const percentileClass = this.getPercentileClass(percentileValue);
      const value = item.value !== null && item.value !== undefined ? item.value : '—';
      const unit = item.unit || '';
      const rank = item.rank;

      return `
        <div class="perf-bar-row">
          <div class="perf-metric-label">${item.metric}</div>
          <div class="perf-bar-track">
            <div class="perf-bar-fill ${percentileClass}" style="width: ${percentileValue}%;">
              <span class="perf-bar-overlay">${percentileValue}th</span>
            </div>
          </div>
          <div class="perf-values">
            <span class="perf-actual-value">${value}${unit}</span>
            ${rank && populationSize ? `<span class="perf-rank-value">#${rank} of ${populationSize}</span>` : ''}
          </div>
        </div>
      `;
    }).join('');

    container.innerHTML = html;
  }
};

/**
 * ============================================================================
 * PLAYER COMPARISONS MODULE
 * Comparison grid with modal overlay for detailed comparisons
 * ============================================================================
 */
const PlayerComparisonsModule = {
  currentType: 'anthropometrics',
  currentPool: 'currentDraft',
  positionFilter: false,
  selectedCompPlayer: null,

  /**
   * Initialize comparisons module
   */
  init() {
    const grid = document.getElementById('compResultsGrid');
    if (!grid || !window.COMPARISON_DATA) return;

    this.setupEventListeners();
    this.renderResults();
  },

  /**
   * Setup event listeners
   */
  setupEventListeners() {
    // Category tabs
    const tabs = document.querySelectorAll('.comp-tab');
    tabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        tabs.forEach((t) => t.classList.remove('active'));
        tab.classList.add('active');
        this.currentType = tab.dataset.category;
        this.renderResults();
      });
    });

    // Pool dropdown
    const poolSelect = document.getElementById('compPool');
    if (poolSelect) {
      poolSelect.addEventListener('change', (e) => {
        this.currentPool = e.target.value;
        this.renderResults();
      });
    }

    // Position checkbox
    const posCheckbox = document.getElementById('compPositionFilter');
    if (posCheckbox) {
      posCheckbox.addEventListener('change', (e) => {
        this.positionFilter = e.target.checked;
        this.renderResults();
      });
    }

    // Modal close button
    const closeBtn = document.getElementById('compCloseBtn');
    if (closeBtn) {
      closeBtn.addEventListener('click', () => this.closeComparison());
    }

    // Close on backdrop click
    const modal = document.getElementById('compComparisonView');
    if (modal) {
      modal.addEventListener('click', (e) => {
        if (e.target === modal) this.closeComparison();
      });
    }

    // Close on escape key
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') this.closeComparison();
    });
  },

  /**
   * Get similarity badge class
   */
  getSimilarityBadgeClass(similarity) {
    if (similarity >= 90) return 'similarity-badge-high';
    if (similarity >= 80) return 'similarity-badge-good';
    if (similarity >= 70) return 'similarity-badge-moderate';
    return 'similarity-badge-weak';
  },

  /**
   * Render comparison card
   */
  renderCompCard(player) {
    const badgeClass = this.getSimilarityBadgeClass(player.similarity);

    return `
      <div class="prospect-card" data-player="${player.name}">
        <div class="prospect-image-wrapper">
          <img src="${player.img}" alt="${player.name}" class="prospect-image" />
          <span class="similarity-badge ${badgeClass}">${player.similarity}%</span>
        </div>
        <div class="prospect-info">
          <h4 class="prospect-name">${player.name}</h4>
          <p class="prospect-meta">${player.position} • ${player.school}</p>
          <div class="prospect-stats">
            <div class="stat-pill">
              <span class="label">HT</span>
              <span class="value">${player.stats.ht}"</span>
            </div>
            <div class="stat-pill">
              <span class="label">WS</span>
              <span class="value">${player.stats.ws}"</span>
            </div>
            <div class="stat-pill">
              <span class="label">VRT</span>
              <span class="value">${player.stats.vert}"</span>
            </div>
          </div>
          <button class="comp-compare-btn" onclick="PlayerComparisonsModule.showComparison('${player.name}')">
            <svg class="icon" viewBox="0 0 24 24">
              <path d="M14.5 17.5L3 6l3-3 11.5 11.5"></path>
              <path d="M13 19l2 2 6-6-2-2-6 6z"></path>
            </svg>
            Compare
          </button>
        </div>
      </div>
    `;
  },

  /**
   * Render all comparison results
   */
  renderResults() {
    const grid = document.getElementById('compResultsGrid');
    if (!grid) return;

    const html = window.COMPARISON_DATA.map((player) => this.renderCompCard(player)).join('');
    grid.innerHTML = html;
  },

  /**
   * Show detailed comparison modal
   */
  showComparison(playerName) {
    this.selectedCompPlayer = window.COMPARISON_DATA.find((p) => p.name === playerName);
    if (!this.selectedCompPlayer) return;

    const modal = document.getElementById('compComparisonView');
    const title = document.getElementById('compComparisonTitle');
    const body = document.getElementById('compComparisonBody');

    if (title) {
      title.textContent = `${window.PLAYER_DATA.name} vs ${this.selectedCompPlayer.name}`;
    }

    if (body) {
      body.innerHTML = this.renderComparisonTable();
    }

    if (modal) {
      modal.classList.add('active');
      document.body.style.overflow = 'hidden';
    }
  },

  /**
   * Close comparison modal
   */
  closeComparison() {
    const modal = document.getElementById('compComparisonView');
    if (modal) {
      modal.classList.remove('active');
      document.body.style.overflow = '';
    }
    this.selectedCompPlayer = null;
  },

  /**
   * Render comparison table for modal
   */
  renderComparisonTable() {
    if (!this.selectedCompPlayer) return '';

    const player = window.PLAYER_DATA;
    const comp = this.selectedCompPlayer;

    // Sample metrics for comparison
    const metrics = [
      { label: 'Height', playerA: player.height, playerB: `${comp.stats.ht}"` },
      { label: 'Wingspan', playerA: player.wingspan, playerB: `${comp.stats.ws}"` },
      { label: 'Vertical', playerA: '36"', playerB: `${comp.stats.vert}"` }
    ];

    const rows = metrics.map((m) => `
      <tr>
        <td class="text-right comp-table-value">${m.playerA}</td>
        <td class="text-center">${m.label}</td>
        <td class="text-left comp-table-value">${m.playerB}</td>
      </tr>
    `).join('');

    return `
      <div class="scanlines"></div>
      <table class="comp-table" style="position: relative;">
        <thead>
          <tr>
            <th class="text-right">${player.name}</th>
            <th class="text-center">Metric</th>
            <th class="text-left">${comp.name}</th>
          </tr>
        </thead>
        <tbody>
          ${rows}
        </tbody>
      </table>
    `;
  }
};

/**
 * ============================================================================
 * HEAD-TO-HEAD MODULE (PLAYER DETAIL VERSION)
 * H2H comparison with live metrics and fixed Player A
 * ============================================================================
 */
const HeadToHeadModule = {
  currentCategory: 'anthropometrics',
  selectedPlayerA: null,
  selectedPlayerB: null,
  players: {},
  cache: {},
  searchTimeout: null,

  /**
   * Initialize the H2H module
   */
  init() {
    const playerBInput = document.getElementById('h2hPlayerB');
    if (!playerBInput || !window.PLAYER_DATA) return;

    this.selectedPlayerA = window.PLAYER_DATA.slug;
    this.players[this.selectedPlayerA] = {
      slug: window.PLAYER_DATA.slug,
      name: window.PLAYER_DATA.name,
      photo: window.PLAYER_DATA.photo_url
    };

    this.loadPlayers()
      .then(() => {
        this.setupEventListeners();
        // Pre-select the first available player (if any) to show an initial comparison.
        const first = Object.keys(this.players).find((slug) => slug !== this.selectedPlayerA);
        if (first) {
          this.selectedPlayerB = first;
          document.getElementById('h2hPlayerB').value = this.players[first].name;
        }
        return this.renderComparison();
      })
      .catch((err) => console.error('Failed to initialize head-to-head module', err));
  },

  /**
   * Fetch available players for selection
   */
  async loadPlayers() {
    try {
      const resp = await fetch('/players');
      if (!resp.ok) return;
      const players = await resp.json();
      players.forEach((p) => {
        if (p.slug === this.selectedPlayerA) return;
        this.players[p.slug] = {
          slug: p.slug,
          name: p.display_name,
          photo: `https://placehold.co/200x200/edf2f7/1f2937?text=${encodeURIComponent(p.display_name)}`
        };
      });
    } catch (err) {
      console.error('Failed to load players list', err);
    }
  },

  /**
   * Setup event listeners
   */
  setupEventListeners() {
    const input = document.getElementById('h2hPlayerB');
    const results = document.getElementById('h2hPlayerResults');
    if (input && results) {
      input.addEventListener('input', (e) => {
        const term = e.target.value.trim();
        if (this.searchTimeout) clearTimeout(this.searchTimeout);
        this.searchTimeout = setTimeout(() => this.searchPlayers(term), 150);
      });

      results.addEventListener('click', (e) => {
        const option = e.target.closest('[data-slug]');
        if (!option) return;
        const slug = option.getAttribute('data-slug');
        const name = option.getAttribute('data-name');
        this.selectedPlayerB = slug;
        input.value = name;
        results.innerHTML = '';
        results.classList.remove('active');
        this.renderComparison();
      });
    }

    const tabs = document.querySelectorAll('.h2h-tab');
    tabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        tabs.forEach((t) => t.classList.remove('active'));
        tab.classList.add('active');
        this.currentCategory = tab.dataset.category;
        this.renderComparison();
      });
    });
  },

  async searchPlayers(term) {
    const results = document.getElementById('h2hPlayerResults');
    if (!results) return;
    if (term.length < 2) {
      results.innerHTML = '';
      results.classList.remove('active');
      return;
    }

    try {
      const resp = await fetch(`/players/search?q=${encodeURIComponent(term)}`);
      if (!resp.ok) return;
      const matches = await resp.json();
      const filtered = matches.filter((p) => p.slug !== this.selectedPlayerA);
      if (!filtered.length) {
        results.innerHTML = '<div class="search-results-empty">No matches</div>';
        results.classList.add('active');
        return;
      }

      results.innerHTML = filtered
        .map(
          (p) => `
          <div class="search-result-item" data-slug="${p.slug}" data-name="${p.display_name}">
            <div class="search-result-name">${p.display_name}</div>
            <div class="search-result-school">${p.school || ''}</div>
          </div>`
        )
        .join('');
      results.classList.add('active');
    } catch (err) {
      console.error('Player search failed', err);
    }
  },

  mapCategoryToApi() {
    const map = {
      anthropometrics: 'anthropometrics',
      combinePerformance: 'combine_performance'
    };
    return map[this.currentCategory] || 'anthropometrics';
  },

  cacheKey() {
    return `${this.selectedPlayerA}|${this.selectedPlayerB}|${this.currentCategory}`;
  },

  async fetchComparison() {
    if (!this.selectedPlayerA || !this.selectedPlayerB) return null;
    const key = this.cacheKey();
    if (this.cache[key]) return this.cache[key];

    const params = new URLSearchParams({
      player_a: this.selectedPlayerA,
      player_b: this.selectedPlayerB,
      category: this.mapCategoryToApi()
    });

    try {
      const resp = await fetch(`/api/players/head-to-head?${params.toString()}`);
      if (!resp.ok) return null;
      const data = await resp.json();
      this.cache[key] = data;
      return data;
    } catch (err) {
      console.error('Failed to fetch head-to-head data', err);
      return null;
    }
  },

  resolvePhoto(slug) {
    const player = this.players[slug];
    if (player && player.photo) return player.photo;
    const name = player ? player.name : slug;
    return `https://placehold.co/200x200/edf2f7/1f2937?text=${encodeURIComponent(name)}`;
  },

  resolveName(slug) {
    const player = this.players[slug];
    return player ? player.name : slug;
  },

  /**
   * Render comparison
   */
  async renderComparison() {
    if (!this.selectedPlayerA || !this.selectedPlayerB) return;

    const data = await this.fetchComparison();
    const comparisonBody = document.getElementById('h2hComparisonBody');
    const winnerTarget = document.getElementById('h2hWinnerDeclaration');
    if (!data || !comparisonBody || !winnerTarget) return;

    // Update photos and names
    document.getElementById('h2hPhotoA').src = this.resolvePhoto(this.selectedPlayerA);
    document.getElementById('h2hPhotoB').src = this.resolvePhoto(this.selectedPlayerB);
    document.getElementById('h2hPhotoNameA').textContent = this.resolveName(this.selectedPlayerA);
    document.getElementById('h2hPhotoNameB').textContent = this.resolveName(this.selectedPlayerB);
    document.getElementById('h2hHeaderA').textContent = this.resolveName(this.selectedPlayerA);
    document.getElementById('h2hHeaderB').textContent = this.resolveName(this.selectedPlayerB);

    // Update similarity badge
    const badge = document.getElementById('h2hSimilarityBadge');
    if (badge) {
      if (data.similarity && data.similarity.score !== undefined && data.similarity.score !== null) {
        badge.textContent = `${Math.round(data.similarity.score)}% Similar`;
      } else {
        badge.textContent = 'No similarity available';
      }
    }

    const metrics = (data.metrics || []).filter(
      (m) =>
        m.raw_value_a !== null &&
        m.raw_value_a !== undefined &&
        m.raw_value_b !== null &&
        m.raw_value_b !== undefined
    );

    if (!metrics.length) {
      comparisonBody.innerHTML =
        '<tr><td colspan="3" class="text-center empty-state">No shared metrics available.</td></tr>';
      winnerTarget.innerHTML = '';
      return;
    }

    let rowsHTML = '';
    let winsA = 0;
    let winsB = 0;

    metrics.forEach((metric) => {
      const valueA = metric.raw_value_a;
      const valueB = metric.raw_value_b;
      const lowerIsBetter = metric.lower_is_better;

      let isWinnerA = false;
      let isWinnerB = false;
      if (valueA !== null && valueB !== null) {
        if (lowerIsBetter) {
          isWinnerA = valueA < valueB;
          isWinnerB = valueB < valueA;
        } else {
          isWinnerA = valueA > valueB;
          isWinnerB = valueB > valueA;
        }
      }

      if (isWinnerA) winsA++;
      if (isWinnerB) winsB++;

      const classA = isWinnerA ? 'h2h-value winner' : 'h2h-value loser';
      const classB = isWinnerB ? 'h2h-value winner' : 'h2h-value loser';
      const displayA = metric.display_value_a ?? '—';
      const displayB = metric.display_value_b ?? '—';
      const unit = metric.unit || '';

      rowsHTML += `
        <tr>
          <td class="text-right ${classA}">${displayA}${unit}</td>
          <td class="text-center">${metric.metric}</td>
          <td class="text-left ${classB}">${displayB}${unit}</td>
        </tr>
      `;
    });

    comparisonBody.innerHTML = rowsHTML;

    const totalMetrics = metrics.length;
    let bannerText = '';
    let bannerClass = '';

    if (winsA > winsB) {
      bannerText = `🏆 ${this.resolveName(this.selectedPlayerA)} wins — ${winsA}/${totalMetrics} categories`;
      bannerClass = 'h2h-winner-banner winner-a';
    } else if (winsB > winsA) {
      bannerText = `🏆 ${this.resolveName(this.selectedPlayerB)} wins — ${winsB}/${totalMetrics} categories`;
      bannerClass = 'h2h-winner-banner winner-b';
    } else {
      bannerText = `⚔️ Tie — ${winsA}/${totalMetrics} categories each`;
      bannerClass = 'h2h-winner-banner tie';
    }

    winnerTarget.innerHTML = `<div class="${bannerClass}">${bannerText}</div>`;
  }
};

/**
 * ============================================================================
 * PLAYER FEED MODULE
 * Renders player-specific news feed
 * ============================================================================
 */
const PlayerFeedModule = {
  /**
   * Initialize the feed
   */
  init() {
    const feedContainer = document.getElementById('playerFeedContainer');
    if (!feedContainer || !window.PLAYER_FEED) return;

    feedContainer.innerHTML = this.renderFeed();
  },

  /**
   * Render all feed items
   */
  renderFeed() {
    return window.PLAYER_FEED.map((item) => {
      const tagClass = item.tag.toLowerCase().replace(' ', '-');

      return `
        <div class="feed-item">
          <p class="feed-title">${item.title}</p>
          <div class="feed-meta">
            ${item.source}
            <svg class="icon" viewBox="0 0 24 24" style="width: 0.75rem; height: 0.75rem;">
              <polyline points="9 18 15 12 9 6"></polyline>
            </svg>
            ${item.time}
            <span class="feed-tag ${tagClass}">${item.tag}</span>
          </div>
        </div>
      `;
    }).join('');
  }
};

/**
 * ============================================================================
 * APPLICATION INITIALIZATION
 * Initialize all modules when DOM is ready
 * ============================================================================
 */
document.addEventListener('DOMContentLoaded', () => {
  ScoreboardModule.init();
  PerformanceModule.init();
  PlayerComparisonsModule.init();
  HeadToHeadModule.init();
  PlayerFeedModule.init();
});
