/**
 * ============================================================================
 * HOME.JS - Homepage JavaScript Modules
 * All interactive functionality and data rendering for the homepage
 * ============================================================================
 */

/**
 * ============================================================================
 * IMAGE UTILS
 * Utility functions for generating player image URLs with style support
 * ============================================================================
 */
const ImageUtils = {
  /**
   * Generate player photo URL based on player ID and current style
   * @param {number} playerId - Player database ID
   * @param {string} displayName - Player display name (for placeholder)
   * @returns {string} Image URL
   */
  getPhotoUrl(playerId, displayName) {
    const style = window.IMAGE_STYLE || 'default';

    if (playerId) {
      // Use the static image path pattern: /static/img/players/{id}_{style}.jpg
      // The server will return placeholder if file doesn't exist
      return `/static/img/players/${playerId}_${style}.jpg`;
    }

    // Fallback to placeholder
    const name = displayName || 'Player';
    return `https://placehold.co/200x200/edf2f7/1f2937?text=${encodeURIComponent(name)}`;
  },

  /**
   * Get player ID from slug using the server-provided map
   * @param {string} slug - Player slug
   * @returns {number|null} Player ID or null if not found
   */
  getPlayerIdFromSlug(slug) {
    return window.PLAYER_ID_MAP ? window.PLAYER_ID_MAP[slug] : null;
  }
};

/**
 * ============================================================================
 * TICKER MODULE
 * Renders and animates the market moves ticker
 * ============================================================================
 */
const TickerModule = {
  /**
   * Initialize the ticker with player change data
   */
  init() {
    const tickerElement = document.getElementById('ticker');
    if (!tickerElement || !window.MOCK_PICKS) return;

    const tickerData = this.prepareTickerData();
    tickerElement.innerHTML = this.renderTicker(tickerData);
  },

  /**
   * Prepare ticker data by sorting players by change magnitude
   * @returns {Array} Sorted ticker items
   */
  prepareTickerData() {
    return [...window.MOCK_PICKS]
      .map((player) => ({ name: player.name, change: player.change }))
      .sort((a, b) => Math.abs(b.change) - Math.abs(a.change));
  },

  /**
   * Render ticker HTML (duplicated for seamless infinite scroll)
   * @param {Array} data - Ticker items
   * @returns {string} HTML string
   */
  renderTicker(data) {
    const chunk = data.map((item) => {
      const changeClass = item.change > 0 ? 'positive' : 'negative';
      const changeSymbol = item.change > 0 ? '▲' : '▼';

      return `
        <span class="ticker-item">
          <strong>${item.name}</strong>
          <span class="ticker-change ${changeClass}">
            ${changeSymbol} ${Math.abs(item.change)}
          </span>
        </span>
      `;
    }).join('');

    // Duplicate content for seamless loop
    return chunk + chunk;
  }
};

/**
 * ============================================================================
 * MOCK DRAFT TABLE MODULE
 * Renders the consensus mock draft table
 * ============================================================================
 */
const MockTableModule = {
  /**
   * Initialize the mock draft table
   */
  init() {
    const tbody = document.getElementById('mockTableBody');
    if (!tbody || !window.MOCK_PICKS) return;

    tbody.innerHTML = this.renderTableRows();
  },

  /**
   * Render table rows for each mock pick
   * @returns {string} HTML string
   */
  renderTableRows() {
    return window.MOCK_PICKS.map((pick) => {
      const changeClass = pick.change > 0
        ? 'change-positive'
        : pick.change < 0
        ? 'change-negative'
        : 'change-neutral';

      const changeSymbol = pick.change > 0
        ? '▲'
        : pick.change < 0
        ? '▼'
        : '=';

      return `
        <tr>
          <td class="tabular-nums mono-font" style="font-weight: 500;">${pick.pick}</td>
          <td><a href="/players/${pick.slug}" class="table-link">${pick.name}</a></td>
          <td>${pick.position}</td>
          <td>${pick.college}</td>
          <td class="tabular-nums mono-font">${pick.avgRank.toFixed(1)}</td>
          <td class="tabular-nums mono-font ${changeClass}">
            ${changeSymbol} ${Math.abs(pick.change)}
          </td>
          <td>
            <a href="#" class="table-link">Bet −110</a>
          </td>
        </tr>
      `;
    }).join('');
  }
};

/**
 * ============================================================================
 * PROSPECTS GRID MODULE
 * Renders the grid of top prospect cards
 * ============================================================================
 */
const ProspectsModule = {
  /**
   * Initialize the prospects grid
   */
  init() {
    const grid = document.getElementById('prospectsGrid');
    if (!grid || !window.PLAYERS) return;

    grid.innerHTML = this.renderProspects();
  },

  /**
   * Render all prospect cards
   * @returns {string} HTML string
   */
  renderProspects() {
    return window.PLAYERS.map((player) => {
      const badge = player.change !== 0
        ? this.renderBadge(player.change)
        : '';

      return `
        <a href="/players/${player.slug}" class="prospect-card" style="text-decoration: none; color: inherit;">
          <div class="prospect-image-wrapper">
            <img
              src="${player.img}"
              alt="${player.name}"
              class="prospect-image"
            />
            ${badge}
          </div>
          <div class="prospect-info">
            <h4 class="prospect-name">${player.name}</h4>
            <p class="prospect-meta">${player.position} • ${player.college}</p>
            <div class="prospect-stats">
              ${this.renderStatPill('HT', `${player.measurables.ht}"`)}
              ${this.renderStatPill('WS', `${player.measurables.ws}"`)}
              ${this.renderStatPill('VRT', `${player.measurables.vert}"`)}
            </div>
          </div>
        </a>
      `;
    }).join('');
  },

  /**
   * Render riser/faller badge
   * @param {number} change - Position change value
   * @returns {string} HTML string
   */
  renderBadge(change) {
    const badgeClass = change > 0 ? 'riser' : 'faller';
    const badgeText = change > 0 ? 'Riser' : 'Faller';
    return `<span class="prospect-badge ${badgeClass}">${badgeText}</span>`;
  },

  /**
   * Render a single stat pill
   * @param {string} label - Stat label
   * @param {string} value - Stat value
   * @returns {string} HTML string
   */
  renderStatPill(label, value) {
    return `
      <div class="stat-pill">
        <span class="label">${label}</span>
        <span class="value">${value}</span>
      </div>
    `;
  }
};

/**
 * ============================================================================
 * HEAD-TO-HEAD COMPARISON MODULE (VS ARENA)
 * Handles player comparison functionality with live API data
 * ============================================================================
 */
const HeadToHeadModule = {
  currentCategory: 'anthropometrics',
  selectedPlayerA: null,
  selectedPlayerB: null,
  players: {},
  cache: {},
  searchTimeoutA: null,
  searchTimeoutB: null,

  /**
   * Initialize Head-to-Head module
   */
  async init() {
    const playerAInput = document.getElementById('h2hPlayerA');
    if (!playerAInput) return;

    try {
      await this.loadPlayers();
      this.setupEventListeners();

      // Pre-select default players for initial display
      const defaultA = 'cooper-flagg';
      const defaultB = 'ace-bailey';
      if (this.players[defaultA] && this.players[defaultB]) {
        this.selectedPlayerA = defaultA;
        this.selectedPlayerB = defaultB;
        document.getElementById('h2hPlayerA').value = this.players[defaultA].name;
        document.getElementById('h2hPlayerB').value = this.players[defaultB].name;
        await this.renderComparison();
      }
    } catch (err) {
      console.error('Failed to initialize head-to-head module', err);
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
        this.players[p.slug] = {
          id: p.id,
          slug: p.slug,
          name: p.display_name,
          photo: ImageUtils.getPhotoUrl(p.id, p.display_name)
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
    const inputA = document.getElementById('h2hPlayerA');
    const inputB = document.getElementById('h2hPlayerB');
    const resultsA = document.getElementById('h2hPlayerAResults');
    const resultsB = document.getElementById('h2hPlayerBResults');

    // Player A search input
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
      });
    }

    // Player B search input
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
    const results = document.getElementById(`h2hPlayer${target}Results`);
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

      if (!matches.length) {
        results.innerHTML = '<div class="search-results-empty">No matches</div>';
        results.classList.add('active');
        return;
      }

      results.innerHTML = matches
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
   * Resolve player photo URL from slug
   */
  resolvePhoto(slug) {
    const player = this.players[slug];
    if (player && player.id) {
      return ImageUtils.getPhotoUrl(player.id, player.name);
    }
    // Fallback: try the server-provided player ID map
    const playerId = ImageUtils.getPlayerIdFromSlug(slug);
    if (playerId) {
      return ImageUtils.getPhotoUrl(playerId, slug);
    }
    // Final fallback to placeholder
    const name = player ? player.name : slug;
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
   * Render the complete comparison from API data
   */
  async renderComparison() {
    if (!this.selectedPlayerA || !this.selectedPlayerB) return;

    const data = await this.fetchComparison();
    const comparisonBody = document.getElementById('h2hComparisonBody');
    const winnerTarget = document.getElementById('h2hWinnerDeclaration');
    if (!data || !comparisonBody || !winnerTarget) return;

    // Update photos and names
    const photoA = document.getElementById('h2hPhotoA');
    const photoB = document.getElementById('h2hPhotoB');
    const nameA = this.resolveName(this.selectedPlayerA);
    const nameB = this.resolveName(this.selectedPlayerB);

    // Set photos with onerror fallback to placeholder
    photoA.src = this.resolvePhoto(this.selectedPlayerA);
    photoA.onerror = () => {
      photoA.src = `https://placehold.co/200x200/edf2f7/1f2937?text=${encodeURIComponent(nameA)}`;
    };
    photoB.src = this.resolvePhoto(this.selectedPlayerB);
    photoB.onerror = () => {
      photoB.src = `https://placehold.co/200x200/edf2f7/1f2937?text=${encodeURIComponent(nameB)}`;
    };

    document.getElementById('h2hPhotoNameA').textContent = nameA;
    document.getElementById('h2hPhotoNameB').textContent = nameB;
    document.getElementById('h2hHeaderA').textContent = nameA;
    document.getElementById('h2hHeaderB').textContent = nameB;

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
 * FEED MODULE
 * Renders the live draft buzz news feed
 * ============================================================================
 */
const FeedModule = {
  /**
   * Initialize the feed
   */
  init() {
    const feedContainer = document.getElementById('feedContainer');
    if (!feedContainer || !window.FEED_ITEMS) return;

    feedContainer.innerHTML = this.renderFeed();
  },

  /**
   * Render all feed items
   * @returns {string} HTML string
   */
  renderFeed() {
    return window.FEED_ITEMS.map((item) => {
      const tagClass = item.tag === 'Riser' ? 'riser' : 'faller';

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
 * SPECIALS MODULE
 * Renders draft position betting specials
 * ============================================================================
 */
const SpecialsModule = {
  /**
   * Initialize the specials section
   */
  init() {
    const specialsList = document.getElementById('specialsList');
    if (!specialsList || !window.MOCK_PICKS) return;

    specialsList.innerHTML = this.renderSpecials();
  },

  /**
   * Render special betting items
   * @returns {string} HTML string
   */
  renderSpecials() {
    return window.MOCK_PICKS.slice(0, 4).map((pick) => {
      const odds = (pick.pick + 50) * 10;
      return `
        <div class="special-item">
          <span>${pick.name} Top ${pick.pick}</span>
          <span class="special-odds">+${odds}</span>
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
  TickerModule.init();
  MockTableModule.init();
  ProspectsModule.init();
  HeadToHeadModule.init();
  FeedModule.init();
  SpecialsModule.init();
});
