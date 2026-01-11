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
 * Uses S3 URLs with format: {base}/players/{id}_{slug}_{style}.png
 * ============================================================================
 */
const ImageUtils = {
  /**
   * Generate player photo URL based on player ID, slug, and current style.
   * Uses S3 URLs when S3_IMAGE_BASE_URL is configured.
   * @param {number} playerId - Player database ID
   * @param {string} displayName - Player display name (for placeholder)
   * @param {string} [slug] - Player URL slug (optional, will lookup from ID_TO_SLUG_MAP)
   * @returns {string} Image URL
   */
  getPhotoUrl(playerId, displayName, slug) {
    const style = window.IMAGE_STYLE || 'default';
    const s3Base = window.S3_IMAGE_BASE_URL;

    if (playerId) {
      // Resolve slug from map if not provided
      const playerSlug = slug || (window.ID_TO_SLUG_MAP ? window.ID_TO_SLUG_MAP[playerId] : null);

      if (s3Base && playerSlug) {
        // Use S3 URL format: {base}/players/{id}_{slug}_{style}.png
        return `${s3Base}/players/${playerId}_${playerSlug}_${style}.png`;
      }

      // Fallback to local static path - use consistent format with slug when available
      if (playerSlug) {
        return `/static/img/players/${playerId}_${playerSlug}_${style}.png`;
      }

      // Legacy fallback only when slug is unavailable
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
  },

  /**
   * Get player slug from ID using the server-provided map
   * @param {number} playerId - Player database ID
   * @returns {string|null} Player slug or null if not found
   */
  getSlugFromPlayerId(playerId) {
    return window.ID_TO_SLUG_MAP ? window.ID_TO_SLUG_MAP[playerId] : null;
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
        this.updateExportButtonState();
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
          photo: ImageUtils.getPhotoUrl(p.id, p.display_name, p.slug)
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
        this.updateExportButtonState();
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
      return ImageUtils.getPhotoUrl(player.id, player.name, slug);
    }
    // Fallback: try the server-provided player ID map
    const playerId = ImageUtils.getPlayerIdFromSlug(slug);
    if (playerId) {
      return ImageUtils.getPhotoUrl(playerId, slug, slug);
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
   * Update export button state based on player selection
   */
  updateExportButtonState() {
    const exportBtn = document.getElementById('vsArenaExportBtn');
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
 * Renders the live draft buzz news feed with enhanced cards and pagination
 * ============================================================================
 */
const FeedModule = {
  itemsPerPage: 10,
  currentPage: 1,
  totalPages: 1,

  /**
   * Initialize the feed
   */
  init() {
    const feedContainer = document.getElementById('feedContainer');
    if (!feedContainer) return;

    // Handle both old format (FEED_ITEMS) and empty state
    if (!window.FEED_ITEMS || window.FEED_ITEMS.length === 0) {
      feedContainer.innerHTML = this.renderEmptyState();
      return;
    }

    this.totalPages = Math.ceil(window.FEED_ITEMS.length / this.itemsPerPage);
    this.render();
  },

  /**
   * Render feed with pagination
   */
  render() {
    const feedContainer = document.getElementById('feedContainer');
    if (!feedContainer) return;

    const startIndex = (this.currentPage - 1) * this.itemsPerPage;
    const endIndex = startIndex + this.itemsPerPage;
    const pageItems = window.FEED_ITEMS.slice(startIndex, endIndex);

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
    const feedContainer = document.getElementById('feedContainer');
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
   * @returns {string} HTML string
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
   * @param {string} tag - Tag name
   * @returns {string} CSS class
   */
  getTagClass(tag) {
    const tagMap = {
      'Scouting Report': 'scouting-report',
      'Big Board': 'big-board',
      'Mock Draft': 'mock-draft',
      'Tier Update': 'tier-update',
      'Game Recap': 'game-recap',
      'Film Study': 'film-study',
      'Skill Theme': 'skill-theme',
      'Team Fit': 'team-fit',
      'Draft Intel': 'draft-intel',
      'Statistical Analysis': 'stats-analysis'
    };
    return tagMap[tag] || 'scouting-report';
  },

  /**
   * Render feed items (subset for current page)
   * @param {Array} items - Feed items to render
   * @returns {string} HTML string
   */
  renderFeedItems(items) {
    return items.map((item) => {
      const tagClass = this.getTagClass(item.tag);
      const hasImage = item.image_url && item.image_url.trim() !== '';
      const imageHtml = hasImage
        ? `<img src="${item.image_url}" alt="" class="feed-card__image" loading="lazy" />`
        : `<div class="feed-card__image feed-card__image--placeholder"></div>`;

      // Build author/time meta line
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
 * EXPORT FUNCTIONS
 * Handle exporting share card images for VS Arena
 * ============================================================================
 */

/**
 * Get current VS Arena context
 */
function getVSArenaContext() {
  const activeTab = document.querySelector('.h2h-tab.active');

  const categoryMap = {
    'anthropometrics': 'anthropometrics',
    'combinePerformance': 'combine',
    'shooting': 'shooting'
  };

  return {
    comparisonGroup: 'current_draft',
    samePosition: false,
    metricGroup: categoryMap[activeTab?.dataset?.category] || 'anthropometrics'
  };
}

/**
 * Export VS Arena comparison share card
 */
function exportVSArena() {
  const playerASlug = HeadToHeadModule.selectedPlayerA;
  const playerBSlug = HeadToHeadModule.selectedPlayerB;
  const playerA = HeadToHeadModule.players?.[playerASlug];
  const playerB = HeadToHeadModule.players?.[playerBSlug];

  if (!playerA?.id || !playerB?.id) {
    console.warn('VS Arena export requires both players selected');
    return;
  }

  const context = getVSArenaContext();
  ExportModal.export('vs_arena', [playerA.id, playerB.id], context);
}

/**
 * ============================================================================
 * APPLICATION INITIALIZATION
 * Initialize all modules when DOM is ready
 * ============================================================================
 */
document.addEventListener('DOMContentLoaded', () => {
  ProspectsModule.init();
  HeadToHeadModule.init();
  FeedModule.init();
});
