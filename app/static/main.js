/**
 * ============================================================================
 * DraftGuru Main JavaScript
 * Global utilities and shared behaviors
 * ============================================================================
 */

/**
 * DraftGuru global namespace
 */
const DraftGuru = {
  /**
   * Feature flags (loaded from server-side config)
   */
  features: {},

  /**
   * Initialize the application
   */
  init() {
    this.setupSearch();
    this.setupAccessibility();
  },

  /**
   * Setup search functionality
   */
  setupSearch() {
    const searchInput = document.getElementById('search');
    const searchButton = document.querySelector('.search-button');

    if (!searchInput) return;

    // Handle search on Enter key
    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        this.performSearch(searchInput.value);
      }
    });

    // Handle search button click
    if (searchButton) {
      searchButton.addEventListener('click', () => {
        this.performSearch(searchInput.value);
      });
    }
  },

  /**
   * Perform search (placeholder for future implementation)
   * @param {string} query - Search query
   */
  performSearch(query) {
    if (!query.trim()) return;

    // TODO: Implement search functionality
    // For now, navigate to search results page
    window.location.href = `/search?q=${encodeURIComponent(query)}`;
  },

  /**
   * Setup accessibility features
   */
  setupAccessibility() {
    // Add skip to main content link for keyboard users
    const skipLink = document.createElement('a');
    skipLink.href = '#main-content';
    skipLink.className = 'sr-only';
    skipLink.textContent = 'Skip to main content';
    skipLink.style.cssText = `
      position: absolute;
      top: -40px;
      left: 0;
      background: var(--color-primary);
      color: white;
      padding: 8px;
      z-index: 1001;
    `;
    skipLink.addEventListener('focus', () => {
      skipLink.style.top = '0';
    });
    skipLink.addEventListener('blur', () => {
      skipLink.style.top = '-40px';
    });
    document.body.insertBefore(skipLink, document.body.firstChild);
  }
};

/**
 * ============================================================================
 * UTILITY FUNCTIONS
 * Helper functions used across the application
 * ============================================================================
 */

/**
 * Debounce function to limit execution rate
 * @param {Function} func - Function to debounce
 * @param {number} wait - Wait time in milliseconds
 * @returns {Function} Debounced function
 */
function debounce(func, wait) {
  let timeout;
  return function executedFunction(...args) {
    const later = () => {
      clearTimeout(timeout);
      func(...args);
    };
    clearTimeout(timeout);
    timeout = setTimeout(later, wait);
  };
}

/**
 * Throttle function to limit execution rate
 * @param {Function} func - Function to throttle
 * @param {number} limit - Limit in milliseconds
 * @returns {Function} Throttled function
 */
function throttle(func, limit) {
  let inThrottle;
  return function(...args) {
    if (!inThrottle) {
      func.apply(this, args);
      inThrottle = true;
      setTimeout(() => inThrottle = false, limit);
    }
  };
}

/**
 * Format a number with commas for thousands
 * @param {number} num - Number to format
 * @returns {string} Formatted number string
 */
function formatNumber(num) {
  return num.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/**
 * Format a decimal as a percentage
 * @param {number} value - Value to format (0-100 or 0-1)
 * @param {number} decimals - Number of decimal places
 * @returns {string} Formatted percentage string
 */
function formatPercent(value, decimals = 1) {
  // Handle both 0-100 and 0-1 formats
  const pct = value > 1 ? value : value * 100;
  return `${pct.toFixed(decimals)}%`;
}

/**
 * Format height in inches to feet and inches
 * @param {number} inches - Height in inches
 * @returns {string} Formatted height string (e.g., "6'8\"")
 */
function formatHeight(inches) {
  const feet = Math.floor(inches / 12);
  const remainingInches = inches % 12;
  return `${feet}'${remainingInches}"`;
}

/**
 * Get CSS variable value
 * @param {string} name - CSS variable name (without --)
 * @returns {string} CSS variable value
 */
function getCSSVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim();
}

/**
 * Create an element with attributes and children
 * @param {string} tag - HTML tag name
 * @param {Object} attrs - Attributes object
 * @param {Array|string} children - Child elements or text content
 * @returns {HTMLElement} Created element
 */
function createElement(tag, attrs = {}, children = []) {
  const element = document.createElement(tag);

  // Set attributes
  Object.entries(attrs).forEach(([key, value]) => {
    if (key === 'className') {
      element.className = value;
    } else if (key === 'style' && typeof value === 'object') {
      Object.assign(element.style, value);
    } else if (key.startsWith('on') && typeof value === 'function') {
      element.addEventListener(key.substring(2).toLowerCase(), value);
    } else if (key === 'dataset' && typeof value === 'object') {
      Object.entries(value).forEach(([dataKey, dataValue]) => {
        element.dataset[dataKey] = dataValue;
      });
    } else {
      element.setAttribute(key, value);
    }
  });

  // Add children
  const childArray = Array.isArray(children) ? children : [children];
  childArray.forEach(child => {
    if (typeof child === 'string') {
      element.appendChild(document.createTextNode(child));
    } else if (child instanceof Node) {
      element.appendChild(child);
    }
  });

  return element;
}

/**
 * Make a fetch request with standard error handling
 * @param {string} url - URL to fetch
 * @param {Object} options - Fetch options
 * @returns {Promise<Object>} Response data
 */
async function fetchJSON(url, options = {}) {
  const defaultOptions = {
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    },
  };

  const response = await fetch(url, { ...defaultOptions, ...options });

  if (!response.ok) {
    const error = new Error(`HTTP error! status: ${response.status}`);
    error.status = response.status;
    error.response = response;
    throw error;
  }

  return response.json();
}

/**
 * Generate placeholder image URL
 * @param {string} name - Name to display on placeholder
 * @param {number} width - Image width
 * @param {number} height - Image height
 * @param {string} bgColor - Background color (hex without #)
 * @param {string} textColor - Text color (hex without #)
 * @returns {string} Placeholder URL
 */
function getPlaceholderImage(name, width = 320, height = 420, bgColor = 'edf2f7', textColor = '1f2937') {
  return `https://placehold.co/${width}x${height}/${bgColor}/${textColor}?text=${encodeURIComponent(name)}`;
}

/**
 * Format relative time (e.g., "3m ago", "2h ago")
 * @param {Date|string} date - Date to format
 * @returns {string} Relative time string
 */
function formatRelativeTime(date) {
  const now = new Date();
  const then = new Date(date);
  const diffMs = now - then;
  const diffSecs = Math.floor(diffMs / 1000);
  const diffMins = Math.floor(diffSecs / 60);
  const diffHours = Math.floor(diffMins / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffSecs < 60) return 'just now';
  if (diffMins < 60) return `${diffMins}m`;
  if (diffHours < 24) return `${diffHours}h`;
  if (diffDays < 7) return `${diffDays}d`;

  return then.toLocaleDateString();
}

/**
 * Animate a number counting up
 * @param {HTMLElement} element - Element to animate
 * @param {number} end - End value
 * @param {number} duration - Animation duration in ms
 * @param {string} suffix - Suffix to append (e.g., '%')
 */
function animateNumber(element, end, duration = 1000, suffix = '') {
  const start = 0;
  const startTime = performance.now();

  function update(currentTime) {
    const elapsed = currentTime - startTime;
    const progress = Math.min(elapsed / duration, 1);

    // Easing function (ease-out)
    const easeOut = 1 - Math.pow(1 - progress, 3);
    const current = Math.round(start + (end - start) * easeOut);

    element.textContent = current + suffix;

    if (progress < 1) {
      requestAnimationFrame(update);
    }
  }

  requestAnimationFrame(update);
}

/**
 * Get percentile tier classification
 * @param {number} percentile - Percentile value (0-100)
 * @returns {string} Tier classification
 */
function getPercentileTier(percentile) {
  if (percentile >= 90) return 'elite';
  if (percentile >= 75) return 'above-average';
  if (percentile >= 50) return 'average';
  if (percentile >= 25) return 'below-average';
  return 'poor';
}

/**
 * Get similarity badge CSS class based on score
 * @param {number} score - Similarity score (0-100)
 * @returns {string} CSS class name
 */
function getSimilarityBadgeClass(score) {
  if (score >= 90) return 'similarity-badge--high';
  if (score >= 75) return 'similarity-badge--good';
  if (score >= 60) return 'similarity-badge--moderate';
  return 'similarity-badge--weak';
}

/**
 * ============================================================================
 * APPLICATION INITIALIZATION
 * Initialize when DOM is ready
 * ============================================================================
 */
document.addEventListener('DOMContentLoaded', () => {
  DraftGuru.init();
});
