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
      shooting: 'shooting',
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
      const itemPopulation = item.population_size ?? populationSize;

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
            ${(rank !== null && rank !== undefined && itemPopulation) ? `<span class="perf-rank-value">#${rank} of ${itemPopulation}</span>` : ''}
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
  cache: {},
  isLoading: false,
  imageStyles: ['default', 'vector', 'comic', 'retro'],

  /**
   * Initialize comparisons module
   */
  init() {
    const grid = document.getElementById('compResultsGrid');
    if (!grid || !window.PLAYER_DATA) return;

    this.setupEventListeners();
    this.fetchAndRender();
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
        this.fetchAndRender();
      });
    });

    // Pool dropdown
    const poolSelect = document.getElementById('compPool');
    if (poolSelect) {
      poolSelect.addEventListener('change', (e) => {
        this.currentPool = e.target.value;
        this.fetchAndRender();
      });
    }

    // Position checkbox
    const posCheckbox = document.getElementById('compPositionFilter');
    if (posCheckbox) {
      posCheckbox.addEventListener('change', (e) => {
        this.positionFilter = e.target.checked;
        this.fetchAndRender();
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
   * Map UI category to API dimension
   */
  mapCategoryToDimension() {
    const map = {
      anthropometrics: 'anthro',
      combinePerformance: 'combine',
      shooting: 'shooting'
    };
    return map[this.currentType] || 'anthro';
  },

  /**
   * Map UI category to head-to-head API category
   */
  mapCategoryToH2HCategory() {
    const map = {
      anthropometrics: 'anthropometrics',
      combinePerformance: 'combine_performance',
      shooting: 'shooting'
    };
    return map[this.currentType] || 'anthropometrics';
  },

  /**
   * Get filter params based on current pool selection
   */
  getPoolFilters() {
    switch (this.currentPool) {
      case 'currentDraft':
        return { same_draft_year: true, nba_only: false };
      case 'nbaPlayers':
        return { same_draft_year: false, nba_only: true };
      case 'historical':
      default:
        return { same_draft_year: false, nba_only: false };
    }
  },

  /**
   * Cache key for current filter state
   */
  cacheKey() {
    const dimension = this.mapCategoryToDimension();
    const poolFilters = this.getPoolFilters();
    return `${dimension}|${this.positionFilter}|${poolFilters.same_draft_year}|${poolFilters.nba_only}`;
  },

  /**
   * Fetch and render similarity data from API
   */
  async fetchAndRender() {
    const key = this.cacheKey();
    if (this.cache[key]) {
      this.renderResults(this.cache[key]);
      return;
    }

    const slug = window.PLAYER_DATA.slug;
    const dimension = this.mapCategoryToDimension();
    const poolFilters = this.getPoolFilters();
    const params = new URLSearchParams({
      dimension: dimension,
      same_position: this.positionFilter ? 'true' : 'false',
      same_draft_year: poolFilters.same_draft_year ? 'true' : 'false',
      nba_only: poolFilters.nba_only ? 'true' : 'false',
      limit: '10'
    });

    this.isLoading = true;
    this.renderLoading();

    try {
      const response = await fetch(
        `/api/players/${encodeURIComponent(slug)}/similar?${params.toString()}`
      );
      if (!response.ok) {
        this.renderResults({ players: [] });
        return;
      }
      const data = await response.json();
      this.cache[key] = data;
      this.renderResults(data);
    } catch (err) {
      console.error('Failed to load similarity data', err);
      this.renderResults({ players: [] });
    } finally {
      this.isLoading = false;
    }
  },

  /**
   * Render loading state
   */
  renderLoading() {
    const grid = document.getElementById('compResultsGrid');
    if (!grid) return;
    grid.innerHTML = '<div class="loading-state">Loading similar players...</div>';
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
   * Get similarity score level class
   */
  getSimilarityScoreLevel(score) {
    if (score >= 90) return 'high';
    if (score >= 80) return 'good';
    return 'moderate';
  },

  resolveStyle() {
    const style = window.IMAGE_STYLE || 'default';
    return this.imageStyles.includes(style) ? style : 'default';
  },

  resolvePlaceholderUrl(name) {
    return `https://placehold.co/320x420/edf2f7/1f2937?text=${encodeURIComponent(name)}`;
  },

  hydrateCardPhotos(container) {
    if (!container) return;

    const style = this.resolveStyle();
    const images = container.querySelectorAll('img.comp-player-photo');
    images.forEach((img) => {
      const playerId = img.dataset.playerId;
      const name = img.dataset.playerName || 'Player';
      const placeholder = this.resolvePlaceholderUrl(name);

      if (!playerId) {
        img.onerror = null;
        img.src = placeholder;
        return;
      }

      const preferred = `/static/img/players/${playerId}_${style}.jpg`;

      if (style !== 'default') {
        img.onerror = () => {
          img.onerror = () => {
            img.src = placeholder;
          };
          img.src = `/static/img/players/${playerId}_default.jpg`;
        };
      } else {
        img.onerror = () => {
          img.src = placeholder;
        };
      }

      img.src = preferred;
    });
  },

  /**
   * Render comparison card for a single player
   */
  renderCompCard(player) {
    const scoreLevel = this.getSimilarityScoreLevel(player.similarity_score);
    const photoUrl = this.resolvePlaceholderUrl(player.display_name);
    const position = player.position || 'N/A';
    const school = player.school || 'Unknown';
    const draftYear = player.draft_year;
    const schoolDisplay = draftYear ? `${school} (${draftYear})` : school;
    const playerUrl = `/players/${player.slug}`;

    return `
      <div class="prospect-card" data-player-slug="${player.slug}">
        <div class="prospect-card-similarity-row">
          <span class="prospect-card-similarity-score ${scoreLevel}">${Math.round(player.similarity_score)}% Match</span>
        </div>
        <a href="${playerUrl}" class="prospect-image-wrapper prospect-image-link">
          <img
            src="${photoUrl}"
            alt="${player.display_name}"
            class="prospect-image comp-player-photo"
            data-player-id="${player.id || ''}"
            data-player-name="${player.display_name}"
          />
        </a>
        <div class="prospect-info">
          <h4 class="prospect-name">${player.display_name}</h4>
          <p class="prospect-meta">${position} • ${schoolDisplay}</p>
          <button class="comp-compare-btn" onclick="PlayerComparisonsModule.showComparison('${player.slug}', '${player.display_name.replace(/'/g, "\\'")}', ${player.id || 'null'})">
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
  renderResults(data) {
    const grid = document.getElementById('compResultsGrid');
    if (!grid) return;

    const players = data.players || [];

    if (!players.length) {
      grid.innerHTML = '<div class="empty-state">No similar players found for this view.</div>';
      return;
    }

    const html = players.map((player) => this.renderCompCard(player)).join('');
    grid.innerHTML = html;
    this.hydrateCardPhotos(grid);
  },

  /**
   * Show detailed comparison modal with head-to-head data
   */
  async showComparison(playerSlug, playerName, playerId) {
    this.selectedCompPlayer = { slug: playerSlug, name: playerName, id: playerId };

    const modal = document.getElementById('compComparisonView');
    const title = document.getElementById('compComparisonTitle');
    const body = document.getElementById('compComparisonBody');

    if (title) {
      title.textContent = `${window.PLAYER_DATA.name} vs ${playerName}`;
    }

    if (body) {
      body.innerHTML = '<div class="loading-state">Loading comparison...</div>';
    }

    if (modal) {
      modal.classList.add('active');
      document.body.style.overflow = 'hidden';
    }

    // Fetch head-to-head comparison data
    const category = this.mapCategoryToH2HCategory();
    const params = new URLSearchParams({
      player_a: window.PLAYER_DATA.slug,
      player_b: playerSlug,
      category: category
    });

    try {
      const response = await fetch(`/api/players/head-to-head?${params.toString()}`);
      if (!response.ok) {
        if (body) {
          body.innerHTML = '<div class="empty-state">Unable to load comparison data.</div>';
        }
        return;
      }
      const h2hData = await response.json();
      if (body) {
        body.innerHTML = this.renderComparisonTable(h2hData);
        this.hydrateComparisonPhotos(body);
      }
    } catch (err) {
      console.error('Failed to load head-to-head data', err);
      if (body) {
        body.innerHTML = '<div class="empty-state">Unable to load comparison data.</div>';
      }
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
   * Get similarity badge level for modal badge
   */
  getSimilarityBadgeLevel(score) {
    if (score >= 90) return 'high';
    if (score >= 80) return 'good';
    return 'moderate';
  },

  resolveSquarePlaceholderUrl(name) {
    return `https://placehold.co/200x200/edf2f7/1f2937?text=${encodeURIComponent(name)}`;
  },

  hydrateComparisonPhotos(container) {
    if (!container) return;

    const style = this.resolveStyle();
    const images = container.querySelectorAll('img.comp-comparison-photo');
    images.forEach((img) => {
      const playerId = img.dataset.playerId;
      const name = img.dataset.playerName || 'Player';
      const placeholder = this.resolveSquarePlaceholderUrl(name);

      if (!playerId) {
        img.onerror = null;
        img.src = placeholder;
        return;
      }

      const preferred = `/static/img/players/${playerId}_${style}.jpg`;

      if (style !== 'default') {
        img.onerror = () => {
          img.onerror = () => {
            img.src = placeholder;
          };
          img.src = `/static/img/players/${playerId}_default.jpg`;
        };
      } else {
        img.onerror = () => {
          img.src = placeholder;
        };
      }

      img.src = preferred;
    });
  },

  /**
   * Render comparison table for modal using head-to-head API data
   */
  renderComparisonTable(h2hData) {
    if (!h2hData || !h2hData.metrics || h2hData.metrics.length === 0) {
      return '<div class="empty-state">No shared metrics available for comparison.</div>';
    }

    const playerA = h2hData.player_a;
    const playerB = h2hData.player_b;
    const similarity = h2hData.similarity;

    const anchor = window.PLAYER_DATA || {};
    const comp = this.selectedCompPlayer || {};
    const playerAName = anchor.name || playerA.name;
    const playerBName = comp.name || playerB.name;
    const playerAId = anchor.id;
    const playerBId = comp.id;

    let winsA = 0;
    let winsB = 0;

    const rows = h2hData.metrics.map((m) => {
      const valueA = m.raw_value_a;
      const valueB = m.raw_value_b;
      const lowerIsBetter = m.lower_is_better;

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

      const tdClassA = isWinnerA ? 'text-right winner' : 'text-right';
      const tdClassB = isWinnerB ? 'text-left winner' : 'text-left';
      const valueClassA = isWinnerA ? 'comp-table-value winner' : 'comp-table-value';
      const valueClassB = isWinnerB ? 'comp-table-value winner' : 'comp-table-value';

      return `
        <tr>
          <td class="${tdClassA}"><span class="${valueClassA}">${m.display_value_a}</span></td>
          <td class="text-center">${m.metric}</td>
          <td class="${tdClassB}"><span class="${valueClassB}">${m.display_value_b}</span></td>
        </tr>
      `;
    }).join('');

    // Prominent similarity badge at top
    const similarityBadge = similarity ? `
      <div class="comp-similarity-display">
        <div class="comp-similarity-badge ${this.getSimilarityBadgeLevel(similarity.score)}">
          ${Math.round(similarity.score)}% Similar
        </div>
      </div>
    ` : '';

    // Determine winner banner
    let winnerBanner = '';
    if (winsA > 0 || winsB > 0) {
      let bannerText, bannerClass;
      if (winsA > winsB) {
        bannerText = `${playerAName} leads ${winsA}-${winsB}`;
        bannerClass = 'comp-winner-banner winner-a';
      } else if (winsB > winsA) {
        bannerText = `${playerBName} leads ${winsB}-${winsA}`;
        bannerClass = 'comp-winner-banner winner-b';
      } else {
        bannerText = `Tied ${winsA}-${winsB}`;
        bannerClass = 'comp-winner-banner tie';
      }
      winnerBanner = `<div class="${bannerClass}">${bannerText}</div>`;
    }

    const photosRow = `
      <div class="comp-photos-row">
        <div class="comp-photo-col">
          <img
            class="comp-comparison-photo"
            src="${this.resolveSquarePlaceholderUrl(playerAName)}"
            alt="${playerAName}"
            data-player-id="${playerAId || ''}"
            data-player-name="${playerAName}"
          />
          <div class="comp-photo-name">${playerAName}</div>
        </div>
        <div class="comp-photos-spacer">VS</div>
        <div class="comp-photo-col">
          <img
            class="comp-comparison-photo"
            src="${this.resolveSquarePlaceholderUrl(playerBName)}"
            alt="${playerBName}"
            data-player-id="${playerBId || ''}"
            data-player-name="${playerBName}"
          />
          <div class="comp-photo-name">${playerBName}</div>
        </div>
      </div>
    `;

    return `
      <div class="scanlines"></div>
      ${winnerBanner}
      ${similarityBadge}
      ${photosRow}
      <table class="comp-table" style="position: relative;">
        <thead>
          <tr>
            <th class="text-right">${playerAName}</th>
            <th class="text-center">Metric</th>
            <th class="text-left">${playerBName}</th>
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
  imageStyles: ['default', 'vector', 'comic', 'retro'],

  /**
   * Initialize the H2H module
   */
  init() {
    const playerBInput = document.getElementById('h2hPlayerB');
    if (!playerBInput || !window.PLAYER_DATA) return;

    this.selectedPlayerA = window.PLAYER_DATA.slug;
    this.players[this.selectedPlayerA] = {
      id: window.PLAYER_DATA.id,
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
          id: p.id,
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
      combinePerformance: 'combine_performance',
      shooting: 'shooting'
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

  resolveStyle() {
    const style = window.IMAGE_STYLE || 'default';
    return this.imageStyles.includes(style) ? style : 'default';
  },

  resolvePlaceholderUrl(name) {
    return `https://placehold.co/200x200/edf2f7/1f2937?text=${encodeURIComponent(name)}`;
  },

  resolvePhoto(slug) {
    const player = this.players[slug];
    if (!player) return this.resolvePlaceholderUrl(slug);
    if (!player.id) return this.resolvePlaceholderUrl(player.name || slug);
    const style = this.resolveStyle();
    return `/static/img/players/${player.id}_${style}.jpg`;
  },

  resolveName(slug) {
    const player = this.players[slug];
    return player ? player.name : slug;
  },

  setPhotoWithFallback(imgEl, slug) {
    if (!imgEl) return;

    const player = this.players[slug];
    const name = player?.name || slug;
    const playerId = player?.id;
    const style = this.resolveStyle();

    imgEl.alt = name;

    if (!playerId) {
      imgEl.onerror = null;
      imgEl.src = this.resolvePlaceholderUrl(name);
      return;
    }

    const placeholder = this.resolvePlaceholderUrl(name);
    const preferred = `/static/img/players/${playerId}_${style}.jpg`;

    if (style !== 'default') {
      imgEl.onerror = () => {
        imgEl.onerror = () => {
          imgEl.src = placeholder;
        };
        imgEl.src = `/static/img/players/${playerId}_default.jpg`;
      };
    } else {
      imgEl.onerror = () => {
        imgEl.src = placeholder;
      };
    }

    imgEl.src = preferred;
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
    this.setPhotoWithFallback(document.getElementById('h2hPhotoA'), this.selectedPlayerA);
    this.setPhotoWithFallback(document.getElementById('h2hPhotoB'), this.selectedPlayerB);
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
 * Renders player-specific news feed with enhanced cards
 * ============================================================================
 */
const PlayerFeedModule = {
  /**
   * Initialize the feed
   */
  init() {
    const feedContainer = document.getElementById('playerFeedContainer');
    if (!feedContainer) return;

    if (!window.PLAYER_FEED || window.PLAYER_FEED.length === 0) {
      feedContainer.innerHTML = this.renderEmptyState();
      return;
    }

    feedContainer.innerHTML = this.renderFeed();
  },

  /**
   * Render empty state when no news items
   */
  renderEmptyState() {
    return `
      <div class="feed-empty">
        <p>No news items yet. Check back soon!</p>
      </div>
    `;
  },

  /**
   * Get tag class based on tag type
   */
  getTagClass(tag) {
    const tagMap = {
      'Riser': 'riser',
      'Faller': 'faller',
      'Analysis': 'analysis',
      'Highlight': 'highlight'
    };
    return tagMap[tag] || 'analysis';
  },

  /**
   * Render all feed items with enhanced card layout
   */
  renderFeed() {
    return window.PLAYER_FEED.map((item) => {
      const tagClass = this.getTagClass(item.tag);
      const hasImage = item.image_url && item.image_url.trim() !== '';
      const imageHtml = hasImage
        ? `<img src="${item.image_url}" alt="" class="feed-card__image" loading="lazy" />`
        : `<div class="feed-card__image feed-card__image--placeholder"></div>`;

      const authorPart = item.author ? `${item.author} • ` : '';
      const summaryHtml = item.summary
        ? `<p class="feed-card__summary">${item.summary}</p>`
        : '';

      return `
        <article class="feed-card">
          <div class="feed-card__image-wrapper">
            ${imageHtml}
          </div>
          <div class="feed-card__content">
            <h4 class="feed-card__title">${item.title}</h4>
            ${summaryHtml}
            <div class="feed-card__meta">
              <span class="feed-card__tag ${tagClass}">${item.tag}</span>
              <span class="feed-card__author-time">${authorPart}${item.time}</span>
            </div>
            <a href="${item.url}" target="_blank" rel="noopener noreferrer" class="feed-card__cta">
              ${item.read_more_text || 'Read More'}
              <svg class="icon" viewBox="0 0 24 24" style="width: 0.875rem; height: 0.875rem;">
                <polyline points="9 18 15 12 9 6"></polyline>
              </svg>
            </a>
          </div>
        </article>
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
