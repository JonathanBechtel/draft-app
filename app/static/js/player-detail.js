/**
 * ============================================================================
 * PLAYER-DETAIL.JS - Player Detail Page JavaScript Modules
 * All interactive functionality and data rendering for the player detail page
 * ============================================================================
 */

/**
 * ============================================================================
 * IMAGE UTILS
 * Utility functions for generating player image URLs with style support
 * Uses S3 URLs with format: {base}/players/{id}_{slug}_{style}.png
 * ============================================================================
 */
const ImageUtils = {
  /**
   * Generate player photo URL based on player ID, slug, and current style.
   * Uses S3 URLs when S3_IMAGE_BASE_URL is configured.
   * @param {number} playerId - Player database ID
   * @param {string} displayName - Player display name (for placeholder)
   * @param {string} [slug] - Player URL slug
   * @returns {string} Image URL
   */
  getPhotoUrl(playerId, displayName, slug) {
    const style = window.IMAGE_STYLE || 'default';
    const s3Base = window.S3_IMAGE_BASE_URL;

    if (playerId && slug && s3Base) {
      // Use S3 URL format: {base}/players/{id}_{slug}_{style}.png
      return `${s3Base}/players/${playerId}_${slug}_${style}.png`;
    }

    // Fallback to placeholder if S3 not configured or missing data
    const name = displayName || 'Player';
    return `https://placehold.co/200x200/edf2f7/1f2937?text=${encodeURIComponent(name)}`;
  },

  /**
   * Get placeholder URL for a player
   * @param {string} name - Player name for placeholder text
   * @param {number} [width=200] - Width in pixels
   * @param {number} [height=200] - Height in pixels
   * @returns {string} Placeholder URL
   */
  getPlaceholderUrl(name, width = 200, height = 200) {
    return `https://placehold.co/${width}x${height}/edf2f7/1f2937?text=${encodeURIComponent(name || 'Player')}`;
  }
};

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
        return { same_draft_year: true, nba_only: false, all_time_nba: false };
      case 'nbaPlayers':
        return { same_draft_year: false, nba_only: true, all_time_nba: false };
      case 'allTimeNba':
        return { same_draft_year: false, nba_only: false, all_time_nba: true };
      case 'historical':
      default:
        return { same_draft_year: false, nba_only: false, all_time_nba: false };
    }
  },

  /**
   * Cache key for current filter state
   */
  cacheKey() {
    const dimension = this.mapCategoryToDimension();
    const poolFilters = this.getPoolFilters();
    return `${dimension}|${this.positionFilter}|${poolFilters.same_draft_year}|${poolFilters.nba_only}|${poolFilters.all_time_nba}`;
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
      all_time_nba: poolFilters.all_time_nba ? 'true' : 'false',
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

    const s3Base = window.S3_IMAGE_BASE_URL;
    const style = this.resolveStyle();
    const images = container.querySelectorAll('img.comp-player-photo');
    images.forEach((img) => {
      const playerId = img.dataset.playerId;
      const playerSlug = img.dataset.playerSlug;
      const name = img.dataset.playerName || 'Player';
      const placeholder = this.resolvePlaceholderUrl(name);

      if (!playerId || !playerSlug) {
        img.onerror = null;
        img.src = placeholder;
        return;
      }

      if (s3Base) {
        // Use S3 URL format: {base}/players/{id}_{slug}_{style}.png
        const s3Url = `${s3Base}/players/${playerId}_${playerSlug}_${style}.png`;
        img.onerror = () => {
          // Fallback to default style on S3
          if (style !== 'default') {
            img.onerror = () => {
              img.src = placeholder;
            };
            img.src = `${s3Base}/players/${playerId}_${playerSlug}_default.png`;
          } else {
            img.src = placeholder;
          }
        };
        img.src = s3Url;
      } else {
        // Fallback to local static path - use consistent format with slug
        const preferred = `/static/img/players/${playerId}_${playerSlug}_${style}.png`;
        if (style !== 'default') {
          img.onerror = () => {
            img.onerror = () => {
              img.src = placeholder;
            };
            img.src = `/static/img/players/${playerId}_${playerSlug}_default.png`;
          };
        } else {
          img.onerror = () => {
            img.src = placeholder;
          };
        }
        img.src = preferred;
      }
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
            data-player-slug="${player.slug || ''}"
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

    const s3Base = window.S3_IMAGE_BASE_URL;
    const style = this.resolveStyle();
    const images = container.querySelectorAll('img.comp-comparison-photo');
    images.forEach((img) => {
      const playerId = img.dataset.playerId;
      const playerSlug = img.dataset.playerSlug;
      const name = img.dataset.playerName || 'Player';
      const placeholder = this.resolveSquarePlaceholderUrl(name);

      if (!playerId || !playerSlug) {
        img.onerror = null;
        img.src = placeholder;
        return;
      }

      if (s3Base) {
        // Use S3 URL format: {base}/players/{id}_{slug}_{style}.png
        const s3Url = `${s3Base}/players/${playerId}_${playerSlug}_${style}.png`;
        img.onerror = () => {
          // Fallback to default style on S3
          if (style !== 'default') {
            img.onerror = () => {
              img.src = placeholder;
            };
            img.src = `${s3Base}/players/${playerId}_${playerSlug}_default.png`;
          } else {
            img.src = placeholder;
          }
        };
        img.src = s3Url;
      } else {
        // Fallback to local static path - use consistent format with slug
        const preferred = `/static/img/players/${playerId}_${playerSlug}_${style}.png`;
        if (style !== 'default') {
          img.onerror = () => {
            img.onerror = () => {
              img.src = placeholder;
            };
            img.src = `/static/img/players/${playerId}_${playerSlug}_default.png`;
          };
        } else {
          img.onerror = () => {
            img.src = placeholder;
          };
        }
        img.src = preferred;
      }
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
    const playerASlug = anchor.slug || '';
    const playerBSlug = comp.slug || '';

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
            data-player-slug="${playerASlug}"
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
            data-player-slug="${playerBSlug}"
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
 * PLAYER FEED MODULE
 * Renders player-specific news feed with enhanced cards and pagination
 * ============================================================================
 */
const PlayerFeedModule = {
  itemsPerPage: 10,
  currentPage: 1,
  totalPages: 1,

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

    // If no articles mention this player, update the heading to generic "Draft News"
    this.hasPlayerSpecific = window.PLAYER_FEED.some(i => i.is_player_specific);
    if (!this.hasPlayerSpecific) {
      const heading = document.getElementById('newsFeedHeading');
      if (heading) heading.textContent = 'Draft News';
    }

    this.totalPages = Math.ceil(window.PLAYER_FEED.length / this.itemsPerPage);
    this.render();
  },

  /**
   * Render feed with pagination
   */
  render() {
    const feedContainer = document.getElementById('playerFeedContainer');
    if (!feedContainer) return;

    const startIndex = (this.currentPage - 1) * this.itemsPerPage;
    const endIndex = startIndex + this.itemsPerPage;
    const pageItems = window.PLAYER_FEED.slice(startIndex, endIndex);

    let html = this.renderFeedItems(pageItems);

    // Add pagination if more than one page
    if (this.totalPages > 1) {
      html += this.renderPagination();
    }

    feedContainer.innerHTML = html;

    // Attach event listeners to pagination pills
    this.attachPaginationListeners();
  },

  /**
   * Attach click listeners to pagination buttons
   */
  attachPaginationListeners() {
    const pills = document.querySelectorAll('.feed-pagination__pill');
    pills.forEach((pill) => {
      pill.addEventListener('click', (e) => {
        const page = parseInt(e.target.dataset.page, 10);
        if (!isNaN(page) && page !== this.currentPage) {
          this.goToPage(page);
        }
      });
    });

    const prevBtn = document.querySelector('.feed-pagination__arrow--prev');
    const nextBtn = document.querySelector('.feed-pagination__arrow--next');

    if (prevBtn) {
      prevBtn.addEventListener('click', () => this.goToPage(this.currentPage - 1));
    }
    if (nextBtn) {
      nextBtn.addEventListener('click', () => this.goToPage(this.currentPage + 1));
    }
  },

  /**
   * Navigate to a specific page
   */
  goToPage(page) {
    if (page < 1 || page > this.totalPages) return;
    this.currentPage = page;
    this.render();

    // Scroll to feed section
    const feedContainer = document.getElementById('playerFeedContainer');
    if (feedContainer) {
      feedContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  },

  /**
   * Render pagination pills
   */
  renderPagination() {
    const pills = [];
    const maxVisiblePills = 5;

    // Calculate range of page pills to show
    let startPage = Math.max(1, this.currentPage - Math.floor(maxVisiblePills / 2));
    let endPage = Math.min(this.totalPages, startPage + maxVisiblePills - 1);

    // Adjust start if we're near the end
    if (endPage - startPage + 1 < maxVisiblePills) {
      startPage = Math.max(1, endPage - maxVisiblePills + 1);
    }

    // Previous arrow
    const prevDisabled = this.currentPage === 1 ? 'disabled' : '';
    pills.push(`
      <button class="feed-pagination__arrow feed-pagination__arrow--prev" ${prevDisabled} aria-label="Previous page">
        <svg class="icon" viewBox="0 0 24 24" style="width: 1rem; height: 1rem;">
          <polyline points="15 18 9 12 15 6"></polyline>
        </svg>
      </button>
    `);

    // First page + ellipsis if needed
    if (startPage > 1) {
      pills.push(`<button class="feed-pagination__pill" data-page="1">1</button>`);
      if (startPage > 2) {
        pills.push(`<span class="feed-pagination__ellipsis">…</span>`);
      }
    }

    // Page pills
    for (let i = startPage; i <= endPage; i++) {
      const activeClass = i === this.currentPage ? 'active' : '';
      pills.push(`<button class="feed-pagination__pill ${activeClass}" data-page="${i}">${i}</button>`);
    }

    // Last page + ellipsis if needed
    if (endPage < this.totalPages) {
      if (endPage < this.totalPages - 1) {
        pills.push(`<span class="feed-pagination__ellipsis">…</span>`);
      }
      pills.push(`<button class="feed-pagination__pill" data-page="${this.totalPages}">${this.totalPages}</button>`);
    }

    // Next arrow
    const nextDisabled = this.currentPage === this.totalPages ? 'disabled' : '';
    pills.push(`
      <button class="feed-pagination__arrow feed-pagination__arrow--next" ${nextDisabled} aria-label="Next page">
        <svg class="icon" viewBox="0 0 24 24" style="width: 1rem; height: 1rem;">
          <polyline points="9 18 15 12 9 6"></polyline>
        </svg>
      </button>
    `);

    return `
      <nav class="feed-pagination" aria-label="News feed pagination">
        ${pills.join('')}
      </nav>
    `;
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
   * Get tag class based on tag type (delegates to shared DraftGuru utility)
   */
  getTagClass(tag) {
    return DraftGuru.getTagClass(tag);
  },

  /**
   * Render feed items (subset for current page)
   * Inserts a divider between player-specific and backfill articles.
   */
  renderFeedItems(items) {
    let insertedDivider = false;
    return items.map((item) => {
      let dividerHtml = '';
      if (!item.is_player_specific && !insertedDivider) {
        insertedDivider = true;
        // Only show divider when there are player-specific articles above;
        // otherwise the heading already reads "Draft News".
        if (this.hasPlayerSpecific) {
          dividerHtml = `
            <div class="feed-divider">
              <span class="feed-divider__label">More Draft News</span>
            </div>
          `;
        }
      }

      const tagClass = this.getTagClass(item.tag);
      const hasImage = item.image_url && item.image_url.trim() !== '';
      const imageHtml = hasImage
        ? `<img src="${item.image_url}" alt="" class="feed-card__image" loading="lazy" />`
        : `<div class="feed-card__image feed-card__image--placeholder"></div>`;

      const authorPart = item.author ? `${item.author} • ` : '';
      const summaryHtml = item.summary
        ? `<p class="feed-card__summary">${item.summary}</p>`
        : '';

      const mentionBadge = item.is_player_specific
        ? '<span class="feed-card__mention-badge">Mentioned</span>'
        : '';

      const cardClass = item.is_player_specific
        ? 'feed-card feed-card--player-specific'
        : 'feed-card';

      return `
        ${dividerHtml}
        <article class="${cardClass}">
          <div class="feed-card__image-wrapper">
            ${imageHtml}
          </div>
          <div class="feed-card__content">
            <h4 class="feed-card__title">${item.title}</h4>
            ${summaryHtml}
            <div class="feed-card__meta">
              <span class="feed-card__tag ${tagClass}">${item.tag}</span>
              ${mentionBadge}
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
 * EXPORT FUNCTIONS
 * Handle exporting share card images for different components
 * ============================================================================
 */

/**
 * Get current performance section context
 */
function getPerformanceContext() {
  const cohortSelect = document.getElementById('perfCohort');
  const positionAdjusted = document.getElementById('perfPositionAdjusted');
  const activeTab = document.querySelector('.perf-tab.active');

  const cohortMap = {
    'currentDraft': 'current_draft',
    'historical': 'all_time_draft',
    'nbaPlayers': 'current_nba',
    'allTimeNba': 'all_time_nba'
  };

  const categoryMap = {
    'anthropometrics': 'anthropometrics',
    'combinePerformance': 'combine',
    'shooting': 'shooting'
  };

  return {
    comparisonGroup: cohortMap[cohortSelect?.value] || 'current_draft',
    samePosition: positionAdjusted?.checked || false,
    metricGroup: categoryMap[activeTab?.dataset?.category] || 'anthropometrics'
  };
}

/**
 * Export performance metrics share card
 */
function exportPerformance() {
  const player = window.PLAYER_DATA;
  if (!player?.id) return;

  const context = getPerformanceContext();
  ExportModal.export('performance', [player.id], context);
}

/**
 * Share performance metrics to X
 */
function sharePerformanceTweet() {
  const player = window.PLAYER_DATA;
  if (!player?.id || typeof TweetShare === 'undefined') return;

  const context = getPerformanceContext();
  const summary = TweetShare.formatContextSummary(context);
  const headline = `DraftGuru: ${player.name} — Performance`;
  const text = summary ? `${headline} • ${summary}` : headline;

  TweetShare.share({
    component: 'performance',
    playerIds: [player.id],
    context,
    text,
    pageUrl: window.location.href
  });
}

/**
 * Export head-to-head comparison share card
 */
function exportH2H() {
  H2HComparison.export();
}

/**
 * Share head-to-head comparison to X
 */
function shareH2HTweet() {
  H2HComparison.shareTweet();
}

/**
 * Get current comps section context
 */
function getCompsContext() {
  const poolSelect = document.getElementById('compPool');
  const positionFilter = document.getElementById('compPositionFilter');
  const activeTab = document.querySelector('.comp-tab.active');

  const cohortMap = {
    'currentDraft': 'current_draft',
    'historical': 'all_time_draft',
    'nbaPlayers': 'current_nba',
    'allTimeNba': 'all_time_nba'
  };

  const categoryMap = {
    'anthropometrics': 'anthropometrics',
    'combinePerformance': 'combine',
    'shooting': 'shooting'
  };

  return {
    comparisonGroup: cohortMap[poolSelect?.value] || 'current_draft',
    samePosition: positionFilter?.checked || false,
    metricGroup: categoryMap[activeTab?.dataset?.category] || 'anthropometrics'
  };
}

/**
 * Export player comparisons share card
 */
function exportComps() {
  const player = window.PLAYER_DATA;
  if (!player?.id) return;

  const context = getCompsContext();
  ExportModal.export('comps', [player.id], context);
}

/**
 * Share player comparisons to X
 */
function shareCompsTweet() {
  const player = window.PLAYER_DATA;
  if (!player?.id || typeof TweetShare === 'undefined') return;

  const context = getCompsContext();
  const summary = TweetShare.formatContextSummary(context);
  const headline = `DraftGuru: ${player.name} — Comparisons`;
  const text = summary ? `${headline} • ${summary}` : headline;

  TweetShare.share({
    component: 'comps',
    playerIds: [player.id],
    context,
    text,
    pageUrl: window.location.href
  });
}

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

  // Initialize shared H2H module for player-detail page (Player A fixed)
  if (window.PLAYER_DATA) {
    H2HComparison.init({
      playerAFixed: true,
      playerASlug: window.PLAYER_DATA.slug,
      playerAName: window.PLAYER_DATA.name,
      playerAId: window.PLAYER_DATA.id,
      playerAPhoto: window.PLAYER_DATA.photo_url,
      exportComponent: 'h2h',
      exportBtnId: 'h2hExportBtn',
      tweetBtnId: 'h2hTweetBtn'
    });
  }

  PlayerFeedModule.init();
});
