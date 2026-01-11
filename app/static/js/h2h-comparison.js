/**
 * ============================================================================
 * H2H COMPARISON MODULE
 * Shared module for Head-to-Head comparisons used by VS Arena and Player Detail
 * ============================================================================
 */
const H2HComparison = {
  // Configuration (set by calling page via init())
  config: {
    playerAFixed: false,        // true = Player A not editable (player-detail mode)
    playerASlug: null,          // Pre-set Player A slug (for fixed mode)
    playerAName: null,          // Pre-set Player A name (for fixed mode)
    playerAId: null,            // Pre-set Player A ID (for fixed mode)
    playerAPhoto: null,         // Pre-set Player A photo URL (for fixed mode)
    defaultPlayerA: null,       // Default slug when not fixed (vs-arena mode)
    defaultPlayerB: null,       // Default Player B slug
    exportComponent: 'h2h',     // 'vs_arena' or 'h2h'
    exportBtnId: 'h2hExportBtn' // Export button element ID
  },

  // State
  currentCategory: 'anthropometrics',
  selectedPlayerA: null,
  selectedPlayerB: null,
  players: {},
  cache: {},
  searchTimeoutA: null,
  searchTimeoutB: null,
  imageStyles: ['default', 'vector', 'comic', 'retro'],

  /**
   * Initialize the H2H comparison module
   * @param {Object} config - Configuration options
   */
  async init(config = {}) {
    // Merge config
    Object.assign(this.config, config);

    // Check for required elements
    const playerBInput = document.getElementById('h2hPlayerB');
    if (!playerBInput) return;

    try {
      // If Player A is fixed, set it from config
      if (this.config.playerAFixed && this.config.playerASlug) {
        this.selectedPlayerA = this.config.playerASlug;
        this.players[this.config.playerASlug] = {
          id: this.config.playerAId,
          slug: this.config.playerASlug,
          name: this.config.playerAName,
          photo: this.config.playerAPhoto
        };
      }

      // Load available players
      await this.loadPlayers();
      this.setupEventListeners();

      // Handle initial player selection based on mode
      if (this.config.playerAFixed) {
        // Player-detail mode: pre-select first available player as Player B
        const first = Object.keys(this.players).find((slug) => slug !== this.selectedPlayerA);
        if (first) {
          this.selectedPlayerB = first;
          document.getElementById('h2hPlayerB').value = this.players[first].name;
          this.updateExportButtonState();
        }
      } else {
        // VS Arena mode: pre-select default players
        const defaultA = this.config.defaultPlayerA;
        const defaultB = this.config.defaultPlayerB;
        if (defaultA && this.players[defaultA]) {
          this.selectedPlayerA = defaultA;
          const inputA = document.getElementById('h2hPlayerA');
          if (inputA) inputA.value = this.players[defaultA].name;
        }
        if (defaultB && this.players[defaultB]) {
          this.selectedPlayerB = defaultB;
          document.getElementById('h2hPlayerB').value = this.players[defaultB].name;
        }
        this.updateExportButtonState();
      }

      // Render initial comparison
      await this.renderComparison();
    } catch (err) {
      console.error('Failed to initialize H2H comparison module', err);
    }
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
        // Skip if this is the fixed Player A (already added)
        if (this.config.playerAFixed && p.slug === this.selectedPlayerA) return;
        this.players[p.slug] = {
          id: p.id,
          slug: p.slug,
          name: p.display_name,
          photo: typeof ImageUtils !== 'undefined'
            ? ImageUtils.getPhotoUrl(p.id, p.display_name, p.slug)
            : null
        };
      });
    } catch (err) {
      console.error('Failed to load players list', err);
    }
  },

  /**
   * Setup event listeners for search inputs and tabs
   */
  setupEventListeners() {
    // Player A search (only if not fixed)
    if (!this.config.playerAFixed) {
      const inputA = document.getElementById('h2hPlayerA');
      const resultsA = document.getElementById('h2hPlayerAResults');
      if (inputA && resultsA) {
        inputA.addEventListener('input', (e) => {
          const term = e.target.value.trim();
          if (this.searchTimeoutA) clearTimeout(this.searchTimeoutA);
          this.searchTimeoutA = setTimeout(() => this.searchPlayers(term, 'A'), 150);
        });

        resultsA.addEventListener('click', (e) => {
          const option = e.target.closest('[data-slug]');
          if (!option) return;
          const slug = option.getAttribute('data-slug');
          const name = option.getAttribute('data-name');
          this.selectedPlayerA = slug;
          inputA.value = name;
          resultsA.innerHTML = '';
          resultsA.classList.remove('active');
          this.renderComparison();
          this.updateExportButtonState();
        });
      }
    }

    // Player B search
    const inputB = document.getElementById('h2hPlayerB');
    const resultsB = document.getElementById('h2hPlayerBResults') || document.getElementById('h2hPlayerResults');
    if (inputB && resultsB) {
      inputB.addEventListener('input', (e) => {
        const term = e.target.value.trim();
        if (this.searchTimeoutB) clearTimeout(this.searchTimeoutB);
        this.searchTimeoutB = setTimeout(() => this.searchPlayers(term, 'B'), 150);
      });

      resultsB.addEventListener('click', (e) => {
        const option = e.target.closest('[data-slug]');
        if (!option) return;
        const slug = option.getAttribute('data-slug');
        const name = option.getAttribute('data-name');
        this.selectedPlayerB = slug;
        inputB.value = name;
        resultsB.innerHTML = '';
        resultsB.classList.remove('active');
        this.renderComparison();
        this.updateExportButtonState();
      });
    }

    // Category tabs
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

  /**
   * Search players via API and render dropdown
   * @param {string} term - Search term
   * @param {string} target - 'A' or 'B' indicating which player input
   */
  async searchPlayers(term, target) {
    // Determine results container ID based on target and mode
    let resultsId;
    if (target === 'A') {
      resultsId = 'h2hPlayerAResults';
    } else {
      resultsId = document.getElementById('h2hPlayerBResults') ? 'h2hPlayerBResults' : 'h2hPlayerResults';
    }
    const results = document.getElementById(resultsId);
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

      // Filter out Player A if searching for Player B (in fixed mode)
      const filtered = this.config.playerAFixed && target === 'B'
        ? matches.filter((p) => p.slug !== this.selectedPlayerA)
        : matches;

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

  /**
   * Map UI category to API category format
   */
  mapCategoryToApi() {
    const map = {
      anthropometrics: 'anthropometrics',
      combinePerformance: 'combine_performance',
      shooting: 'shooting'
    };
    return map[this.currentCategory] || 'anthropometrics';
  },

  /**
   * Generate cache key for current comparison
   */
  cacheKey() {
    return `${this.selectedPlayerA}|${this.selectedPlayerB}|${this.currentCategory}`;
  },

  /**
   * Fetch comparison data from API with caching
   */
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

  /**
   * Resolve the current image style preference
   */
  resolveStyle() {
    const style = window.IMAGE_STYLE || 'default';
    return this.imageStyles.includes(style) ? style : 'default';
  },

  /**
   * Generate placeholder URL for player
   */
  resolvePlaceholderUrl(name) {
    return `https://placehold.co/200x200/edf2f7/1f2937?text=${encodeURIComponent(name)}`;
  },

  /**
   * Resolve player name from slug
   */
  resolveName(slug) {
    const player = this.players[slug];
    return player ? player.name : slug;
  },

  /**
   * Set player photo with fallback chain
   * @param {HTMLImageElement} imgEl - Image element
   * @param {string} slug - Player slug
   */
  setPhotoWithFallback(imgEl, slug) {
    if (!imgEl) return;

    const s3Base = window.S3_IMAGE_BASE_URL;
    const player = this.players[slug];
    const name = player?.name || slug;
    const playerId = player?.id;
    const playerSlug = player?.slug;
    const style = this.resolveStyle();

    imgEl.alt = name;

    if (!playerId || !playerSlug) {
      imgEl.onerror = null;
      imgEl.src = this.resolvePlaceholderUrl(name);
      return;
    }

    const placeholder = this.resolvePlaceholderUrl(name);

    if (s3Base) {
      // Use S3 URL format: {base}/players/{id}_{slug}_{style}.png
      const s3Url = `${s3Base}/players/${playerId}_${playerSlug}_${style}.png`;
      imgEl.onerror = () => {
        // Fallback to default style on S3
        if (style !== 'default') {
          imgEl.onerror = () => {
            imgEl.src = placeholder;
          };
          imgEl.src = `${s3Base}/players/${playerId}_${playerSlug}_default.png`;
        } else {
          imgEl.src = placeholder;
        }
      };
      imgEl.src = s3Url;
    } else {
      // Fallback to local static path
      const preferred = `/static/img/players/${playerId}_${playerSlug}_${style}.png`;
      if (style !== 'default') {
        imgEl.onerror = () => {
          imgEl.onerror = () => {
            imgEl.src = placeholder;
          };
          imgEl.src = `/static/img/players/${playerId}_${playerSlug}_default.png`;
        };
      } else {
        imgEl.onerror = () => {
          imgEl.src = placeholder;
        };
      }
      imgEl.src = preferred;
    }
  },

  /**
   * Update export button state based on player selection
   */
  updateExportButtonState() {
    const exportBtn = document.getElementById(this.config.exportBtnId);
    if (!exportBtn) return;

    const playerA = this.players?.[this.selectedPlayerA];
    const playerB = this.players?.[this.selectedPlayerB];

    if (playerA?.id && playerB?.id) {
      exportBtn.disabled = false;
    } else {
      exportBtn.disabled = true;
    }
  },

  /**
   * Render the complete comparison from API data
   */
  async renderComparison() {
    if (!this.selectedPlayerA || !this.selectedPlayerB) return;

    const data = await this.fetchComparison();
    const comparisonBody = document.getElementById('h2hComparisonBody');
    const winnerTarget = document.getElementById('h2hWinnerDeclaration');
    if (!data || !comparisonBody || !winnerTarget) return;

    // Update photos
    this.setPhotoWithFallback(document.getElementById('h2hPhotoA'), this.selectedPlayerA);
    this.setPhotoWithFallback(document.getElementById('h2hPhotoB'), this.selectedPlayerB);

    // Update names
    const nameA = this.resolveName(this.selectedPlayerA);
    const nameB = this.resolveName(this.selectedPlayerB);
    const photoNameA = document.getElementById('h2hPhotoNameA');
    const photoNameB = document.getElementById('h2hPhotoNameB');
    const headerA = document.getElementById('h2hHeaderA');
    const headerB = document.getElementById('h2hHeaderB');

    if (photoNameA) photoNameA.textContent = nameA;
    if (photoNameB) photoNameB.textContent = nameB;
    if (headerA) headerA.textContent = nameA;
    if (headerB) headerB.textContent = nameB;

    // Update similarity badge
    const badge = document.getElementById('h2hSimilarityBadge');
    if (badge) {
      if (data.similarity && data.similarity.score !== undefined && data.similarity.score !== null) {
        badge.textContent = `${Math.round(data.similarity.score)}% Similar`;
      } else {
        badge.textContent = 'No similarity available';
      }
    }

    // Filter to metrics with data for both players
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

    // Render winner banner
    const totalMetrics = metrics.length;
    let bannerText = '';
    let bannerClass = '';

    if (winsA > winsB) {
      bannerText = `🏆 ${nameA} wins — ${winsA}/${totalMetrics} categories`;
      bannerClass = 'h2h-winner-banner winner-a';
    } else if (winsB > winsA) {
      bannerText = `🏆 ${nameB} wins — ${winsB}/${totalMetrics} categories`;
      bannerClass = 'h2h-winner-banner winner-b';
    } else {
      bannerText = `⚔️ Tie — ${winsA}/${totalMetrics} categories each`;
      bannerClass = 'h2h-winner-banner tie';
    }

    winnerTarget.innerHTML = `<div class="${bannerClass}">${bannerText}</div>`;
  },

  /**
   * Export the current comparison as an image
   */
  export() {
    const playerA = this.players[this.selectedPlayerA];
    const playerB = this.players[this.selectedPlayerB];
    if (!playerA?.id || !playerB?.id) return;

    const categoryMap = {
      anthropometrics: 'anthropometrics',
      combinePerformance: 'combine',
      shooting: 'shooting'
    };

    const context = {
      comparisonGroup: 'current_draft',
      samePosition: false,
      metricGroup: categoryMap[this.currentCategory] || 'anthropometrics'
    };

    if (typeof ExportModal !== 'undefined') {
      ExportModal.export(this.config.exportComponent, [playerA.id, playerB.id], context);
    }
  }
};

// Export for global access
window.H2HComparison = H2HComparison;
