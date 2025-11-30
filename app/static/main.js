/**
 * DraftGuru Shared Utilities
 * Common functionality used across all pages
 */
const DraftGuru = {
  /**
   * Initialize search functionality
   */
  initSearch() {
    const searchInput = document.getElementById('search');
    const searchButton = document.querySelector('.search-button');

    if (searchInput && searchButton) {
      searchButton.addEventListener('click', () => this.performSearch(searchInput.value));
      searchInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          this.performSearch(searchInput.value);
        }
      });
    }
  },

  /**
   * Perform search action
   * @param {string} query - Search query
   */
  performSearch(query) {
    if (query.trim()) {
      // TODO: Wire to search API or redirect to search results
      console.log('Searching for:', query);
      window.location.href = `/search?q=${encodeURIComponent(query)}`;
    }
  }
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  DraftGuru.initSearch();
});
