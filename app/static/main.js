/**
 * DraftGuru Shared Utilities
 * Common functionality used across all pages
 */
const DraftGuru = {
  searchTimeout: null,
  selectedIndex: -1,
  searchResults: [],

  /**
   * Initialize search functionality with typeahead
   */
  initSearch() {
    const searchInput = document.getElementById('search');
    const searchButton = document.querySelector('.search-button');
    const searchResultsContainer = document.getElementById('search-results');

    if (!searchInput || !searchResultsContainer) return;

    // Typeahead on input with debounce
    searchInput.addEventListener('input', (e) => {
      const query = e.target.value.trim();
      this.selectedIndex = -1;

      if (this.searchTimeout) {
        clearTimeout(this.searchTimeout);
      }

      if (query.length === 0) {
        this.hideResults();
        return;
      }

      // Debounce search requests
      this.searchTimeout = setTimeout(() => {
        this.fetchSearchResults(query);
      }, 300);
    });

    // Keyboard navigation
    searchInput.addEventListener('keydown', (e) => {
      if (!searchResultsContainer.classList.contains('active')) {
        if (e.key === 'Enter' && searchInput.value.trim()) {
          // If no dropdown, trigger search on Enter
          this.fetchSearchResults(searchInput.value.trim());
        }
        return;
      }

      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault();
          this.navigateResults(1);
          break;
        case 'ArrowUp':
          e.preventDefault();
          this.navigateResults(-1);
          break;
        case 'Enter':
          e.preventDefault();
          this.selectResult();
          break;
        case 'Escape':
          this.hideResults();
          searchInput.blur();
          break;
      }
    });

    // Hide on blur (with delay to allow click)
    searchInput.addEventListener('blur', () => {
      setTimeout(() => this.hideResults(), 200);
    });

    // Show results on focus if there's a query
    searchInput.addEventListener('focus', () => {
      if (searchInput.value.trim() && this.searchResults.length > 0) {
        this.showResults();
      }
    });

    // Search button click
    if (searchButton) {
      searchButton.addEventListener('click', () => {
        const query = searchInput.value.trim();
        if (query) {
          this.fetchSearchResults(query);
          searchInput.focus();
        }
      });
    }
  },

  /**
   * Fetch search results from API
   */
  async fetchSearchResults(query) {
    try {
      const response = await fetch(`/players/search?q=${encodeURIComponent(query)}`);
      if (!response.ok) {
        console.error('Search request failed:', response.status);
        return;
      }

      const results = await response.json();
      this.searchResults = results;
      this.renderResults(results);
    } catch (error) {
      console.error('Search error:', error);
    }
  },

  /**
   * Render search results in dropdown
   */
  renderResults(results) {
    const container = document.getElementById('search-results');
    if (!container) return;

    if (results.length === 0) {
      container.innerHTML = '<div class="search-results-empty">No players found</div>';
      this.showResults();
      return;
    }

    container.innerHTML = results
      .map(
        (player, index) => `
        <div class="search-result-item"
             data-index="${index}"
             data-slug="${player.slug || ''}"
             role="option"
             aria-selected="false">
          <span class="search-result-name">${this.escapeHtml(player.display_name || 'Unknown')}</span>
          <span class="search-result-school">${this.escapeHtml(player.school || '')}</span>
        </div>
      `
      )
      .join('');

    // Add click handlers to results
    container.querySelectorAll('.search-result-item').forEach((item) => {
      item.addEventListener('click', (e) => {
        const slug = item.dataset.slug;
        if (slug) {
          this.navigateToPlayer(slug);
        }
      });

      item.addEventListener('mouseenter', () => {
        this.selectedIndex = parseInt(item.dataset.index, 10);
        this.updateSelectedState();
      });
    });

    this.showResults();
  },

  /**
   * Navigate through results with arrow keys
   */
  navigateResults(direction) {
    const maxIndex = this.searchResults.length - 1;
    if (maxIndex < 0) return;

    this.selectedIndex += direction;

    if (this.selectedIndex < 0) {
      this.selectedIndex = maxIndex;
    } else if (this.selectedIndex > maxIndex) {
      this.selectedIndex = 0;
    }

    this.updateSelectedState();
  },

  /**
   * Update visual selection state
   */
  updateSelectedState() {
    const container = document.getElementById('search-results');
    if (!container) return;

    container.querySelectorAll('.search-result-item').forEach((item, index) => {
      if (index === this.selectedIndex) {
        item.classList.add('selected');
        item.setAttribute('aria-selected', 'true');
        item.scrollIntoView({ block: 'nearest' });
      } else {
        item.classList.remove('selected');
        item.setAttribute('aria-selected', 'false');
      }
    });
  },

  /**
   * Select current result (on Enter key)
   */
  selectResult() {
    if (this.selectedIndex >= 0 && this.selectedIndex < this.searchResults.length) {
      const player = this.searchResults[this.selectedIndex];
      if (player.slug) {
        this.navigateToPlayer(player.slug);
      }
    } else if (this.searchResults.length === 1) {
      // If only one result, select it
      const player = this.searchResults[0];
      if (player.slug) {
        this.navigateToPlayer(player.slug);
      }
    }
  },

  /**
   * Navigate to player page
   */
  navigateToPlayer(slug) {
    window.location.href = `/players/${slug}`;
  },

  /**
   * Show results dropdown
   */
  showResults() {
    const container = document.getElementById('search-results');
    if (container) {
      container.classList.add('active');
    }
  },

  /**
   * Hide results dropdown
   */
  hideResults() {
    const container = document.getElementById('search-results');
    if (container) {
      container.classList.remove('active');
    }
    this.selectedIndex = -1;
  },

  /**
   * Escape HTML to prevent XSS
   */
  escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  },

  /**
   * Convert a tag name to its CSS class suffix.
   * @param {string} tag - Tag name like "Scouting Report"
   * @returns {string} CSS class like "scouting-report"
   */
  getTagClass(tag) {
    if (!tag) return 'scouting-report';
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
      'Statistical Analysis': 'stats-analysis',
    };
    return tagMap[tag] || 'scouting-report';
  }
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  DraftGuru.initSearch();
});
