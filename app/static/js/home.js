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
    const esc = DraftGuru.escapeHtml.bind(DraftGuru);
    return window.PLAYERS.map((player) => {
      const badge = player.change !== 0
        ? this.renderBadge(player.change)
        : '';

	      return `
	        <a href="/players/${esc(player.slug)}" class="prospect-card" style="text-decoration: none; color: inherit;">
	          <div class="prospect-image-wrapper">
	            <img
	              src="${esc(player.img)}"
	              alt="${esc(player.name)}"
	              class="prospect-image"
	              onerror="if(!this.dataset.dgFallback){this.dataset.dgFallback='1';this.src='${esc(player.img_default)}';}else{this.onerror=null;this.src='${esc(player.img_placeholder)}';}"
	            />
	            ${badge}
	          </div>
	          <div class="prospect-info">
	            <h4 class="prospect-name">${esc(player.name)}</h4>
            <p class="prospect-meta">${esc(player.position)} • ${esc(player.college)}</p>
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
 * HERO MODULE
 * Handles hero article display with image-based mode detection
 * ============================================================================
 */
const HeroModule = {
  article: null,
  currentMode: 'gradient',

  // Category icons (SVG paths) for gradient fallback
  categoryIcons: {
    'Scouting Report': '<path d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/>',
    'Big Board': '<path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"/>',
    'Mock Draft': '<path d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/>',
    'Tier Update': '<path d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"/>',
    'Game Recap': '<path d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/><path d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>',
    'Film Study': '<path d="M7 4v16M17 4v16M3 8h4m10 0h4M3 12h18M3 16h4m10 0h4M4 20h16a1 1 0 001-1V5a1 1 0 00-1-1H4a1 1 0 00-1 1v14a1 1 0 001 1z"/>',
    'Skill Theme': '<path d="M13 10V3L4 14h7v7l9-11h-7z"/>',
    'Team Fit': '<path d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z"/>',
    'Draft Intel': '<path d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z"/>',
    'Statistical Analysis': '<path d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/>'
  },

  /**
   * Initialize the hero module
   */
  init() {
    this.article = window.HERO_ARTICLE;

    // If no hero article, hide the section
    if (!this.article) {
      const section = document.getElementById('newsHeroSection');
      if (section) section.style.display = 'none';
      return;
    }

    this.detectAndRender();
  },

  /**
   * Detect image size and render appropriate mode
   */
  async detectAndRender() {
    const mode = await this.detectDisplayMode();
    this.currentMode = mode;
    this.render(mode);
  },

  /**
   * Detect display mode based on image dimensions
   * @returns {Promise<string>} Display mode: 'full', 'split', 'blurred', or 'gradient'
   */
  detectDisplayMode() {
    return new Promise((resolve) => {
      if (!this.article?.image_url) {
        resolve('gradient');
        return;
      }

      const img = new Image();
      img.onload = () => {
        // 800px+ wide = full hero
        if (img.naturalWidth >= 800) {
          resolve('full');
        }
        // 400-799px = split layout
        else if (img.naturalWidth >= 400) {
          resolve('split');
        }
        // <400px = blurred background
        else {
          resolve('blurred');
        }
      };
      img.onerror = () => resolve('gradient');
      img.src = this.article.image_url;
    });
  },

  /**
   * Render the hero section based on mode
   * @param {string} mode - Display mode
   */
  render(mode) {
    const hero = document.getElementById('heroArticle');
    const article = this.article;
    const imageUrl = article.image_url;
    const tagClass = DraftGuru.getTagClass(article.tag);

    // Reset classes
    hero.className = 'news-hero';

    // Add click handler to open article
    hero.onclick = () => window.open(article.url, '_blank');

    const esc = DraftGuru.escapeHtml.bind(DraftGuru);
    const safeImageUrl = esc(imageUrl || '');
    const safeTitle = esc(article.title);

    if (mode === 'full' && imageUrl) {
      // Full wide image - best case
      hero.innerHTML = `
        <img class="news-hero__image" src="${safeImageUrl}" alt="${safeTitle}" />
        ${this.renderOverlay(article, tagClass)}
      `;
    } else if (mode === 'split' && imageUrl) {
      // Split layout - text dominant with side image
      hero.classList.add('news-hero--split');
      hero.innerHTML = `
        <div class="news-hero__text-area">
          <span class="news-hero__tag tag--${tagClass}">${esc(article.tag)}</span>
          <h2 class="news-hero__title">${safeTitle}</h2>
          <p class="news-hero__summary">${esc(article.summary || '')}</p>
          <div class="news-hero__meta">
            <span class="news-hero__source">${esc(article.source)}</span>
            ${article.author ? `<span class="news-hero__author">by ${esc(article.author)}</span>` : ''}
            <span class="news-hero__time">${esc(article.time)}</span>
          </div>
        </div>
        <div class="news-hero__image-area">
          <img class="news-hero__image" src="${safeImageUrl}" alt="${safeTitle}" />
        </div>
      `;
    } else if (mode === 'blurred' && imageUrl) {
      // Blurred background with contained image
      hero.classList.add('news-hero--blurred');
      hero.innerHTML = `
        <div class="news-hero__background" style="background-image: url('${safeImageUrl}');"></div>
        <div class="news-hero__image-container">
          <img class="news-hero__image" src="${safeImageUrl}" alt="${safeTitle}" />
        </div>
        ${this.renderOverlay(article, tagClass)}
      `;
    } else {
      // Gradient fallback - no suitable image
      hero.classList.add('news-hero--gradient', `news-hero--${tagClass}`);
      const iconPath = this.categoryIcons[article.tag] || this.categoryIcons['Scouting Report'];
      hero.innerHTML = `
        <div class="news-hero__pattern"></div>
        <svg class="news-hero__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          ${iconPath}
        </svg>
        <div class="news-hero__spacer"></div>
        ${this.renderOverlay(article, tagClass)}
      `;
    }
  },

  /**
   * Render the overlay content (tag, title, summary, meta)
   * @param {Object} article - Article data
   * @param {string} tagClass - CSS class for tag
   * @returns {string} HTML string
   */
  renderOverlay(article, tagClass) {
    const esc = DraftGuru.escapeHtml.bind(DraftGuru);
    return `
      <div class="news-hero__overlay">
        <span class="news-hero__tag tag--${tagClass}">${esc(article.tag)}</span>
        <h2 class="news-hero__title">${esc(article.title)}</h2>
        <p class="news-hero__summary">${esc(article.summary || '')}</p>
        <div class="news-hero__meta">
          <span class="news-hero__source">${esc(article.source)}</span>
          ${article.author ? `<span class="news-hero__author">by ${esc(article.author)}</span>` : ''}
          <span class="news-hero__time">${esc(article.time)}</span>
        </div>
      </div>
    `;
  }
};

/**
 * ============================================================================
 * SIDEBAR MODULE
 * Handles sources and authors filtering sidebar
 * ============================================================================
 */
const SidebarModule = {
  currentSourceFilter: null,
  currentAuthorFilter: null,
  currentTagFilter: null,

  /**
   * Initialize the sidebar
   */
  init() {
    this.renderSources();
    this.renderAuthors();
    this.setupClearButtons();
    this.setupTagFilters();
  },

  /**
   * Render sources list
   */
  renderSources() {
    const container = document.getElementById('sourcesList');
    if (!container) return;

    const limit = Number(window.SIDEBAR_LIMIT) || 8;
    const sources = (window.SOURCE_COUNTS || []).slice(0, limit);

    if (sources.length === 0) {
      container.innerHTML = '<div class="sources-list--empty">No sources available</div>';
      return;
    }

    container.innerHTML = sources.map(s => `
      <div class="source-item" data-source="${DraftGuru.escapeHtml(s.source_name)}">
        <span class="source-item__name">${DraftGuru.escapeHtml(s.source_name)}</span>
        <span class="source-item__count">${s.count}</span>
      </div>
    `).join('');

    // Attach click listeners
    container.querySelectorAll('.source-item').forEach(item => {
      item.addEventListener('click', () => {
        const source = item.dataset.source;
        this.filterBySource(source);
      });
    });
  },

  /**
   * Render authors list
   */
  renderAuthors() {
    const container = document.getElementById('authorsList');
    if (!container) return;

    const limit = Number(window.SIDEBAR_LIMIT) || 8;
    const authors = (window.AUTHOR_COUNTS || []).slice(0, limit);

    if (authors.length === 0) {
      container.innerHTML = '<div class="sources-list--empty">No authors available</div>';
      return;
    }

    container.innerHTML = authors.map(a => `
      <div class="source-item" data-author="${DraftGuru.escapeHtml(a.author)}">
        <span class="source-item__name">${DraftGuru.escapeHtml(a.author)}</span>
        <span class="source-item__count">${a.count}</span>
      </div>
    `).join('');

    // Attach click listeners
    container.querySelectorAll('.source-item').forEach(item => {
      item.addEventListener('click', () => {
        const author = item.dataset.author;
        this.filterByAuthor(author);
      });
    });
  },

  /**
   * Setup clear filter buttons
   */
  setupClearButtons() {
    const clearSourceBtn = document.getElementById('clearSourceFilter');
    const clearAuthorBtn = document.getElementById('clearAuthorFilter');

    if (clearSourceBtn) {
      clearSourceBtn.addEventListener('click', () => this.clearSourceFilter());
    }
    if (clearAuthorBtn) {
      clearAuthorBtn.addEventListener('click', () => this.clearAuthorFilter());
    }
  },

  /**
   * Filter by source
   * @param {string} source - Source name to filter by
   */
  filterBySource(source) {
    // Clear author and tag filters
    this.currentAuthorFilter = null;
    this.currentTagFilter = null;
    this.currentSourceFilter = source;

    // Update UI
    this.updateFilterUI('source', source);
    this.updateTagFilterUI(null);

    // Apply filter to grid
    NewsGridModule.applyFilter('source', source);
  },

  /**
   * Filter by author
   * @param {string} author - Author name to filter by
   */
  filterByAuthor(author) {
    // Clear source and tag filters
    this.currentSourceFilter = null;
    this.currentTagFilter = null;
    this.currentAuthorFilter = author;

    // Update UI
    this.updateFilterUI('author', author);
    this.updateTagFilterUI(null);

    // Apply filter to grid
    NewsGridModule.applyFilter('author', author);
  },

  /**
   * Update sidebar UI to reflect current filter
   * @param {string} type - 'source' or 'author'
   * @param {string} value - Filter value
   */
  updateFilterUI(type, value) {
    // Clear all active states
    document.querySelectorAll('.source-item').forEach(item => {
      item.classList.remove('active');
    });

    // Set active state on selected item
    const selector = type === 'source' ? `[data-source="${value}"]` : `[data-author="${value}"]`;
    const activeItem = document.querySelector(selector);
    if (activeItem) {
      activeItem.classList.add('active');
    }

    // Show/hide clear buttons
    const clearSourceBtn = document.getElementById('clearSourceFilter');
    const clearAuthorBtn = document.getElementById('clearAuthorFilter');

    if (clearSourceBtn) {
      clearSourceBtn.classList.toggle('visible', type === 'source');
    }
    if (clearAuthorBtn) {
      clearAuthorBtn.classList.toggle('visible', type === 'author');
    }

    // Add filtered class to section
    const sourcesSection = document.getElementById('sourcesSection');
    const authorsSection = document.getElementById('authorsSection');

    if (sourcesSection) {
      sourcesSection.classList.toggle('sidebar-section--filtered', type === 'source');
    }
    if (authorsSection) {
      authorsSection.classList.toggle('sidebar-section--filtered', type === 'author');
    }
  },

  /**
   * Clear source filter
   */
  clearSourceFilter() {
    this.currentSourceFilter = null;
    this.clearFilterUI();
    NewsGridModule.clearFilter();
  },

  /**
   * Clear author filter
   */
  clearAuthorFilter() {
    this.currentAuthorFilter = null;
    this.clearFilterUI();
    NewsGridModule.clearFilter();
  },

  /**
   * Clear all filter UI states
   */
  clearFilterUI() {
    document.querySelectorAll('.source-item').forEach(item => {
      item.classList.remove('active');
    });

    document.querySelectorAll('.sidebar-section__clear').forEach(btn => {
      btn.classList.remove('visible');
    });

    document.querySelectorAll('.sidebar-section').forEach(section => {
      section.classList.remove('sidebar-section--filtered');
    });
  },

  /**
   * Filter by tag (story type)
   * @param {string} tag - Tag name to filter by
   */
  filterByTag(tag) {
    // Clear other filters
    this.currentSourceFilter = null;
    this.currentAuthorFilter = null;
    this.currentTagFilter = tag;

    // Update UI
    this.updateTagFilterUI(tag);
    this.clearFilterUI(); // Clear sidebar highlights

    // Apply filter
    NewsGridModule.applyFilter('tag', tag);
  },

  /**
   * Clear tag filter
   */
  clearTagFilter() {
    this.currentTagFilter = null;
    this.updateTagFilterUI(null);
    NewsGridModule.clearFilter();
  },

  /**
   * Update tag filter button UI
   * @param {string|null} activeTag - Currently active tag or null
   */
  updateTagFilterUI(activeTag) {
    document.querySelectorAll('.story-filter').forEach(btn => {
      const isActive = (activeTag === null || activeTag === '')
        ? btn.dataset.tag === ''
        : btn.dataset.tag === activeTag;
      btn.classList.toggle('active', isActive);
    });
  },

  /**
   * Setup tag filter button click handlers
   */
  setupTagFilters() {
    document.querySelectorAll('.story-filter').forEach(btn => {
      btn.addEventListener('click', () => {
        const tag = btn.dataset.tag;
        if (tag === '') {
          this.clearTagFilter();
        } else {
          this.filterByTag(tag);
        }
      });
    });
  }
};

/**
 * ============================================================================
 * NEWS GRID MODULE
 * Renders the articles grid with pagination and filtering
 * ============================================================================
 */
const NewsGridModule = {
  itemsPerPage: 6,
  currentPage: 1,
  totalPages: 1,
  allItems: [],
  filteredItems: [],
  filterType: null,
  filterValue: null,

  /**
   * Initialize the news grid
   */
  init() {
    this.allItems = window.FEED_ITEMS || [];
    this.filteredItems = [...this.allItems];
    this.totalPages = Math.ceil(this.filteredItems.length / this.itemsPerPage);

    if (this.allItems.length === 0) {
      this.renderEmptyState();
      this.hidePagination();
      return;
    }

    this.render();
    this.setupPaginationEvents();
  },

  /**
   * Apply filter to articles
   * @param {string} type - 'source', 'author', or 'tag'
   * @param {string} value - Value to filter by
   */
  applyFilter(type, value) {
    this.filterType = type;
    this.filterValue = value;

    this.filteredItems = this.allItems.filter(item => {
      if (type === 'author') return item.author === value;
      if (type === 'source') return item.source === value;
      if (type === 'tag') return item.tag === value;
      return true;
    });

    this.currentPage = 1;
    this.totalPages = Math.ceil(this.filteredItems.length / this.itemsPerPage);
    this.render();
  },

  /**
   * Clear filter
   */
  clearFilter() {
    this.filterType = null;
    this.filterValue = null;
    this.filteredItems = [...this.allItems];
    this.currentPage = 1;
    this.totalPages = Math.ceil(this.filteredItems.length / this.itemsPerPage);
    this.render();
  },

  /**
   * Render the current page of articles
   */
  render() {
    const grid = document.getElementById('articlesGrid');
    if (!grid) return;

    if (this.filteredItems.length === 0) {
      grid.innerHTML = '<div class="articles-grid--empty">No articles match your filter</div>';
      this.hidePagination();
      return;
    }

    const startIndex = (this.currentPage - 1) * this.itemsPerPage;
    const endIndex = startIndex + this.itemsPerPage;
    const pageItems = this.filteredItems.slice(startIndex, endIndex);

    grid.innerHTML = pageItems.map(item => this.renderArticleCard(item)).join('');
    this.renderPagination();
  },

  /**
   * Render a single article card
   * @param {Object} item - Article data
   * @returns {string} HTML string
   */
  renderArticleCard(item) {
    const tagClass = DraftGuru.getTagClass(item.tag);
    const hasImage = item.image_url && item.image_url.trim() !== '';

    return `
      <article class="article-card" onclick="window.open('${DraftGuru.escapeHtml(item.url)}', '_blank')">
        <div class="article-card__image-wrapper">
          ${hasImage
            ? `<img src="${DraftGuru.escapeHtml(item.image_url)}" class="article-card__image" alt="" loading="lazy" />`
            : `<div class="article-card__image-placeholder">DG</div>`
          }
          <span class="article-card__tag tag--${tagClass}">${DraftGuru.escapeHtml(item.tag)}</span>
        </div>
        <div class="article-card__content">
          <h3 class="article-card__title">${DraftGuru.escapeHtml(item.title)}</h3>
          ${item.summary ? `<p class="article-card__summary">${DraftGuru.escapeHtml(item.summary)}</p>` : ''}
          <div class="article-card__meta">
            <span class="article-card__source">${DraftGuru.escapeHtml(item.source)}</span>
            <span class="article-card__time">${DraftGuru.escapeHtml(item.time)}</span>
          </div>
        </div>
      </article>
    `;
  },

  /**
   * Render pagination controls
   */
  renderPagination() {
    const paginationNumbers = document.getElementById('paginationNumbers');
    const prevBtn = document.getElementById('prevPage');
    const nextBtn = document.getElementById('nextPage');
    const paginationInfo = document.getElementById('paginationInfo');
    const pagination = document.getElementById('pagination');

    if (!paginationNumbers || !prevBtn || !nextBtn) return;

    // Show pagination
    if (pagination) pagination.style.display = '';

    // Update prev/next button states
    prevBtn.disabled = this.currentPage === 1;
    nextBtn.disabled = this.currentPage === this.totalPages;

    // Generate page number buttons
    const maxVisiblePages = 5;
    let startPage = Math.max(1, this.currentPage - Math.floor(maxVisiblePages / 2));
    let endPage = Math.min(this.totalPages, startPage + maxVisiblePages - 1);

    // Adjust start if we're near the end
    if (endPage - startPage < maxVisiblePages - 1) {
      startPage = Math.max(1, endPage - maxVisiblePages + 1);
    }

    let numbersHTML = '';

    // First page + ellipsis
    if (startPage > 1) {
      numbersHTML += `<button class="pagination__button" data-page="1">1</button>`;
      if (startPage > 2) {
        numbersHTML += `<span class="pagination__ellipsis">...</span>`;
      }
    }

    // Page numbers
    for (let i = startPage; i <= endPage; i++) {
      const activeClass = i === this.currentPage ? 'active' : '';
      numbersHTML += `<button class="pagination__button ${activeClass}" data-page="${i}">${i}</button>`;
    }

    // Last page + ellipsis
    if (endPage < this.totalPages) {
      if (endPage < this.totalPages - 1) {
        numbersHTML += `<span class="pagination__ellipsis">...</span>`;
      }
      numbersHTML += `<button class="pagination__button" data-page="${this.totalPages}">${this.totalPages}</button>`;
    }

    paginationNumbers.innerHTML = numbersHTML;

    // Update info text
    if (paginationInfo) {
      const startItem = (this.currentPage - 1) * this.itemsPerPage + 1;
      const endItem = Math.min(this.currentPage * this.itemsPerPage, this.filteredItems.length);
      paginationInfo.textContent = `${startItem}-${endItem} of ${this.filteredItems.length}`;
    }

    // Attach click events to page number buttons
    paginationNumbers.querySelectorAll('.pagination__button').forEach(btn => {
      btn.addEventListener('click', () => {
        this.goToPage(parseInt(btn.dataset.page, 10));
      });
    });
  },

  /**
   * Setup pagination button events
   */
  setupPaginationEvents() {
    const prevBtn = document.getElementById('prevPage');
    const nextBtn = document.getElementById('nextPage');

    if (prevBtn) {
      prevBtn.addEventListener('click', () => {
        if (this.currentPage > 1) {
          this.goToPage(this.currentPage - 1);
        }
      });
    }

    if (nextBtn) {
      nextBtn.addEventListener('click', () => {
        if (this.currentPage < this.totalPages) {
          this.goToPage(this.currentPage + 1);
        }
      });
    }
  },

  /**
   * Navigate to a specific page
   * @param {number} page - Page number
   */
  goToPage(page) {
    if (page < 1 || page > this.totalPages) return;
    this.currentPage = page;
    this.render();

    // Scroll to top of articles grid
    const grid = document.getElementById('articlesGrid');
    if (grid) {
      grid.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  },

  /**
   * Hide pagination when no items
   */
  hidePagination() {
    const pagination = document.getElementById('pagination');
    if (pagination) pagination.style.display = 'none';
  },

  /**
   * Render empty state
   */
  renderEmptyState() {
    const grid = document.getElementById('articlesGrid');
    if (grid) {
      grid.innerHTML = '<div class="articles-grid--empty">No news items yet. Check back soon!</div>';
    }
  }
};

/**
 * ============================================================================
 * TRENDING MODULE
 * Renders the trending players section based on recent mention volume
 * ============================================================================
 */
const TrendingModule = {
  /**
   * Initialize the trending players section
   */
  init() {
    const data = window.TRENDING_PLAYERS;
    if (!data || data.length === 0) return;

    const section = document.getElementById('trendingSection');
    const divider = document.getElementById('trendingDivider');
    const grid = document.getElementById('trendingGrid');
    if (!section || !grid) return;

    section.style.display = '';
    if (divider) divider.style.display = '';

    grid.innerHTML = data.map(player => this.renderCard(player)).join('');
  },

  /**
   * Render a single trending player card
   * @param {Object} player - Trending player data
   * @returns {string} HTML string
   */
  renderCard(player) {
    const esc = DraftGuru.escapeHtml.bind(DraftGuru);
    const school = player.school
      ? `<span class="trending-card__school">${esc(player.school)}</span>`
      : '';
    const thumbnail = this.renderThumbnail(player);
    const sparkline = this.renderSparkline(player.daily_counts || []);

    return `
      <a href="/players/${esc(player.slug)}" class="trending-card">
        ${thumbnail}
        <div class="trending-card__info">
          <span class="trending-card__name">${esc(player.display_name)}</span>
          ${school}
        </div>
        <div class="trending-card__visual">
          ${sparkline}
          <span class="trending-card__count">${player.mention_count}</span>
        </div>
      </a>
    `;
  },

  /**
   * Render a small circular player thumbnail
   * @param {Object} player - Trending player data
   * @returns {string} HTML string
   */
  renderThumbnail(player) {
    const esc = DraftGuru.escapeHtml.bind(DraftGuru);
    const imgUrl = ImageUtils.getPhotoUrl(player.player_id, player.display_name, player.slug);
    const initial = (player.display_name || '?').charAt(0).toUpperCase();
    return `
      <img
        src="${esc(imgUrl)}"
        alt="${esc(player.display_name)}"
        class="trending-card__thumb"
        onerror="this.onerror=null;this.replaceWith(Object.assign(document.createElement('span'),{className:'trending-card__thumb trending-card__thumb--placeholder',textContent:'${initial}'}))"
      />
    `;
  },

  /**
   * Render an inline SVG sparkline from daily mention counts
   * @param {number[]} counts - Array of daily counts (oldest-first)
   * @returns {string} SVG HTML string
   */
  renderSparkline(counts) {
    if (!counts || counts.length === 0) return '';

    const width = 64;
    const height = 24;
    const padding = 2;
    const maxVal = Math.max(...counts, 1);
    const step = (width - padding * 2) / Math.max(counts.length - 1, 1);

    const points = counts.map((v, i) => {
      const x = padding + i * step;
      const y = height - padding - ((v / maxVal) * (height - padding * 2));
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');

    return `
      <svg class="trending-card__sparkline" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
        <polyline
          points="${points}"
          fill="none"
          stroke="var(--color-accent-emerald, #10b981)"
          stroke-width="1.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
    `;
  },

};

/**
 * ============================================================================
 * HOME PODCAST MODULE
 * Renders the Latest Podcasts section on the homepage
 * Play buttons link to /podcasts (no inline audio on homepage)
 * ============================================================================
 */
const HomePodcastModule = {
  TAG_CLASSES: {
    'Draft Analysis': 'draft-analysis',
    'Mock Draft': 'mock-draft',
    'Game Breakdown': 'game-breakdown',
    'Interview': 'interview',
    'Trade & Intel': 'trade-intel',
    'Prospect Debate': 'prospect-debate',
    'Mailbag': 'mailbag',
    'Event Preview': 'event-preview',
  },

  init() {
    const episodes = window.PODCAST_EPISODES;
    if (!episodes || episodes.length === 0) return;

    const section = document.getElementById('podcastsSection');
    if (!section) return;

    section.style.display = '';
    this.renderHero(episodes[0]);
    this.renderList(episodes.slice(1));
  },

  renderHero(ep) {
    const container = document.getElementById('podcastHero');
    if (!container) return;

    const esc = DraftGuru.escapeHtml.bind(DraftGuru);
    const tagClass = this.TAG_CLASSES[ep.tag] || '';
    const episodeTag = tagClass
      ? `<span class="episode-tag episode-tag--lg episode-tag--${tagClass}" style="align-self: flex-start; margin-bottom: 0.375rem;">${esc(ep.tag)}</span>`
      : '';

    container.innerHTML = `
      <a href="/podcasts" class="podcast-featured" style="text-decoration: none; color: inherit;">
        <div class="podcast-featured__artwork">
          ${(ep.artwork_url || ep.show_artwork_url)
            ? `<img src="${esc(ep.artwork_url || ep.show_artwork_url)}" alt="${esc(ep.show_name)}" />`
            : `<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:var(--color-slate-200);font-family:var(--font-mono);color:var(--color-slate-500);font-size:2rem;">DG</div>`
          }
          <div class="podcast-featured__badge">
            <span class="pulse-dot"></span>
            Latest Episode
          </div>
        </div>
        <div class="podcast-featured__body">
          ${episodeTag}
          <div class="podcast-featured__show">${esc(ep.show_name)}</div>
          <div class="podcast-featured__title">${esc(ep.title)}</div>
          ${ep.summary ? `<div class="podcast-featured__summary">${esc(ep.summary)}</div>` : ''}
          <div class="podcast-featured__meta">
            <span>${esc(ep.duration)}</span>
            <span class="meta-dot"></span>
            <span>${esc(ep.time)}</span>
          </div>
          ${ep.mentioned_players && ep.mentioned_players.length > 0 ? `
          <div class="podcast-featured__tags">
            ${ep.mentioned_players.map(p => `<a href="/players/${esc(p.slug)}" class="player-tag" onclick="event.stopPropagation()">${esc(p.display_name)}</a>`).join('')}
          </div>` : ''}
        </div>
      </a>
    `;
  },

  renderList(episodes) {
    const container = document.getElementById('homeEpisodeList');
    if (!container) return;

    if (episodes.length === 0) return;

    const esc = DraftGuru.escapeHtml.bind(DraftGuru);
    container.innerHTML = episodes.map(ep => {
      const tagClass = this.TAG_CLASSES[ep.tag] || '';
      const episodeTag = tagClass ? `<span class="episode-tag episode-tag--${tagClass}">${esc(ep.tag)}</span>` : '';

      return `
        <a href="/podcasts" class="episode-row">
          <div class="episode-row__inner">
            ${(ep.artwork_url || ep.show_artwork_url)
              ? `<img class="episode-row__art" src="${esc(ep.artwork_url || ep.show_artwork_url)}" alt="${esc(ep.show_name)}" loading="lazy" />`
              : `<div class="episode-row__art" style="display:flex;align-items:center;justify-content:center;background:var(--color-slate-100);font-family:var(--font-mono);color:var(--color-slate-400);font-size:0.625rem;">DG</div>`
            }
            <div class="episode-row__info">
              <div class="episode-row__top-line">
                <span class="episode-row__show">${esc(ep.show_name)}</span>
                <span class="episode-row__title">${esc(ep.title)}</span>
              </div>
              <div class="episode-row__bottom-line">
                <span class="episode-row__meta">
                  <span>${esc(ep.duration)}</span>
                  <span class="meta-dot"></span>
                  <span>${esc(ep.time)}</span>
                </span>
                ${episodeTag}
              </div>
              ${ep.mentioned_players && ep.mentioned_players.length > 0 ? `
              <div class="episode-row__tags">
                ${ep.mentioned_players.slice(0, 3).map(p => `<a href="/players/${esc(p.slug)}" class="player-tag" onclick="event.stopPropagation()">${esc(p.display_name)}</a>`).join('')}
              </div>` : ''}
            </div>
            <span class="episode-row__play" aria-label="Listen on podcasts page">
              <svg viewBox="0 0 24 24"><polygon points="6,3 20,12 6,21"></polygon></svg>
            </span>
          </div>
        </a>
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
 * Export VS Arena comparison share card
 */
function exportVSArena() {
  H2HComparison.export();
}

/**
 * Share VS Arena comparison to X
 */
function shareVSArenaTweet() {
  H2HComparison.shareTweet();
}

/**
 * ============================================================================
 * APPLICATION INITIALIZATION
 * Initialize all modules when DOM is ready
 * ============================================================================
 */
document.addEventListener('DOMContentLoaded', () => {
  ProspectsModule.init();

  // Initialize shared H2H module for VS Arena
  H2HComparison.init({
    playerAFixed: false,
    defaultPlayerA: 'cooper-flagg',
    defaultPlayerB: 'ace-bailey',
    exportComponent: 'vs_arena',
    exportBtnId: 'vsArenaExportBtn',
    tweetBtnId: 'vsArenaTweetBtn'
  });

  // Initialize trending players and news section modules
  TrendingModule.init();
  HeroModule.init();
  SidebarModule.init();
  NewsGridModule.init();

  // Initialize podcast section
  HomePodcastModule.init();
});
