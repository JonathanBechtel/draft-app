/**
 * ============================================================================
 * HOME.JS - Homepage JavaScript Modules
 * All interactive functionality and data rendering for the homepage
 * ============================================================================
 */

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
 * Handles player comparison functionality with photos and category tabs
 * ============================================================================
 */
const HeadToHeadModule = {
  selectedPlayerA: null,
  selectedPlayerB: null,
  currentCategory: 'anthropometrics',

  // Player data with full stats
  playerData: {
    'Cooper Flagg': {
      img: 'https://placehold.co/200x200/d946ef/ffffff?text=Cooper+Flagg',
      anthropometrics: { height: 81, weight: 205, wingspan: 86, standingReach: 108 },
      combinePerformance: { verticalLeap: 38.5, lane: 10.8, shuttle: 2.9, bench: 12 },
      advancedStats: { per: 28.4, ts: 61.2, orb: 12.8, drb: 18.5, ast: 23.1, stl: 3.2, blk: 5.8, tov: 12.4 }
    },
    'Ace Bailey': {
      img: 'https://placehold.co/200x200/d946ef/ffffff?text=Ace+Bailey',
      anthropometrics: { height: 79, weight: 196, wingspan: 83, standingReach: 104 },
      combinePerformance: { verticalLeap: 41.0, lane: 10.5, shuttle: 2.8, bench: 10 },
      advancedStats: { per: 26.8, ts: 58.7, orb: 8.4, drb: 15.2, ast: 18.6, stl: 2.8, blk: 3.1, tov: 14.2 }
    },
    'Zachary Rioux': {
      img: 'https://placehold.co/200x200/d946ef/ffffff?text=Zachary+Rioux',
      anthropometrics: { height: 84, weight: 245, wingspan: 89, standingReach: 112 },
      combinePerformance: { verticalLeap: 32.5, lane: 11.8, shuttle: 3.2, bench: 15 },
      advancedStats: { per: 24.2, ts: 64.5, orb: 18.2, drb: 22.4, ast: 8.5, stl: 1.4, blk: 8.9, tov: 9.8 }
    },
    'Matas Buzelis': {
      img: 'https://placehold.co/200x200/d946ef/ffffff?text=Matas+Buzelis',
      anthropometrics: { height: 82, weight: 210, wingspan: 87, standingReach: 109 },
      combinePerformance: { verticalLeap: 36.0, lane: 11.0, shuttle: 3.0, bench: 11 },
      advancedStats: { per: 25.6, ts: 59.8, orb: 10.2, drb: 16.8, ast: 20.4, stl: 2.5, blk: 4.2, tov: 11.6 }
    },
    'KJ Evans Jr.': {
      img: 'https://placehold.co/200x200/d946ef/ffffff?text=KJ+Evans+Jr',
      anthropometrics: { height: 80, weight: 215, wingspan: 84, standingReach: 106 },
      combinePerformance: { verticalLeap: 35.0, lane: 11.2, shuttle: 3.1, bench: 13 },
      advancedStats: { per: 23.8, ts: 56.4, orb: 14.6, drb: 19.2, ast: 14.8, stl: 2.1, blk: 3.8, tov: 10.2 }
    }
  },

  // Category metrics definitions
  categoryMetrics: {
    anthropometrics: [
      { key: 'height', label: 'Height', unit: '"', higherIsBetter: true },
      { key: 'weight', label: 'Weight', unit: ' lbs', higherIsBetter: true },
      { key: 'wingspan', label: 'Wingspan', unit: '"', higherIsBetter: true },
      { key: 'standingReach', label: 'Standing Reach', unit: '"', higherIsBetter: true }
    ],
    combinePerformance: [
      { key: 'verticalLeap', label: 'Vertical Leap', unit: '"', higherIsBetter: true },
      { key: 'lane', label: 'Lane Agility', unit: 's', higherIsBetter: false },
      { key: 'shuttle', label: '3/4 Shuttle', unit: 's', higherIsBetter: false },
      { key: 'bench', label: 'Bench Press', unit: ' reps', higherIsBetter: true }
    ],
    advancedStats: [
      { key: 'per', label: 'PER', unit: '', higherIsBetter: true },
      { key: 'ts', label: 'TS%', unit: '%', higherIsBetter: true },
      { key: 'orb', label: 'ORB%', unit: '%', higherIsBetter: true },
      { key: 'drb', label: 'DRB%', unit: '%', higherIsBetter: true },
      { key: 'ast', label: 'AST%', unit: '%', higherIsBetter: true },
      { key: 'stl', label: 'STL%', unit: '%', higherIsBetter: true },
      { key: 'blk', label: 'BLK%', unit: '%', higherIsBetter: true },
      { key: 'tov', label: 'TOV%', unit: '%', higherIsBetter: false }
    ]
  },

  /**
   * Initialize Head-to-Head module
   */
  init() {
    const playerASelect = document.getElementById('playerA');
    if (!playerASelect) return;

    this.populateSelectors();
    this.setupEventListeners();
    this.renderComparison();
  },

  /**
   * Populate player dropdown selectors
   */
  populateSelectors() {
    const playerASelect = document.getElementById('playerA');
    const playerBSelect = document.getElementById('playerB');

    Object.keys(this.playerData).forEach((playerName) => {
      const optionA = document.createElement('option');
      optionA.value = playerName;
      optionA.textContent = playerName;
      playerASelect.appendChild(optionA);

      const optionB = document.createElement('option');
      optionB.value = playerName;
      optionB.textContent = playerName;
      playerBSelect.appendChild(optionB);
    });

    // Set default selections
    const playerNames = Object.keys(this.playerData);
    playerASelect.value = playerNames[0];
    playerBSelect.value = playerNames[1];
    this.selectedPlayerA = playerNames[0];
    this.selectedPlayerB = playerNames[1];
  },

  /**
   * Setup event listeners
   */
  setupEventListeners() {
    // Player selectors
    document.getElementById('playerA').addEventListener('change', (e) => {
      this.selectedPlayerA = e.target.value;
      this.renderComparison();
    });

    document.getElementById('playerB').addEventListener('change', (e) => {
      this.selectedPlayerB = e.target.value;
      this.renderComparison();
    });

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
   * Calculate similarity score between two players
   */
  calculateSimilarity(playerAData, playerBData) {
    const metrics = this.categoryMetrics[this.currentCategory];
    let totalDiff = 0;
    let count = 0;

    metrics.forEach((metric) => {
      const valA = playerAData[metric.key];
      const valB = playerBData[metric.key];
      if (valA !== undefined && valB !== undefined) {
        const diff = Math.abs(valA - valB);
        const avg = (valA + valB) / 2;
        const percentDiff = avg !== 0 ? (diff / avg) * 100 : 0;
        totalDiff += percentDiff;
        count++;
      }
    });

    const avgDiff = count > 0 ? totalDiff / count : 0;
    const similarity = Math.max(0, 100 - avgDiff);
    return Math.round(similarity);
  },

  /**
   * Render the complete comparison
   */
  renderComparison() {
    if (!this.selectedPlayerA || !this.selectedPlayerB) return;

    const playerAData = this.playerData[this.selectedPlayerA][this.currentCategory];
    const playerBData = this.playerData[this.selectedPlayerB][this.currentCategory];

    // Update photos
    document.getElementById('h2hPhotoA').src = this.playerData[this.selectedPlayerA].img;
    document.getElementById('h2hPhotoB').src = this.playerData[this.selectedPlayerB].img;
    document.getElementById('h2hPhotoNameA').textContent = this.selectedPlayerA;
    document.getElementById('h2hPhotoNameB').textContent = this.selectedPlayerB;

    // Update similarity badge
    const similarity = this.calculateSimilarity(playerAData, playerBData);
    document.getElementById('h2hSimilarityBadge').textContent = `${similarity}% Similar`;

    // Update table headers
    document.getElementById('headerA').textContent = this.selectedPlayerA;
    document.getElementById('headerB').textContent = this.selectedPlayerB;

    // Render comparison table
    const metrics = this.categoryMetrics[this.currentCategory];
    let rowsHTML = '';
    let winsA = 0;
    let winsB = 0;

    metrics.forEach((metric) => {
      const valueA = playerAData[metric.key];
      const valueB = playerBData[metric.key];

      // Determine winner based on metric type
      let isWinnerA, isWinnerB;
      if (metric.higherIsBetter) {
        isWinnerA = valueA > valueB;
        isWinnerB = valueB > valueA;
      } else {
        isWinnerA = valueA < valueB;
        isWinnerB = valueB < valueA;
      }

      if (isWinnerA) winsA++;
      if (isWinnerB) winsB++;

      // Build classes for styling with fuchsia winner theme
      const classA = isWinnerA ? 'h2h-value winner' : 'h2h-value loser';
      const classB = isWinnerB ? 'h2h-value winner' : 'h2h-value loser';

      rowsHTML += `
        <tr>
          <td class="text-right ${classA}">${valueA}${metric.unit}</td>
          <td class="text-center">${metric.label}</td>
          <td class="text-left ${classB}">${valueB}${metric.unit}</td>
        </tr>
      `;
    });

    document.getElementById('comparisonBody').innerHTML = rowsHTML;

    // Render winner banner
    const totalMetrics = metrics.length;
    let bannerText = '';
    let bannerClass = '';

    if (winsA > winsB) {
      bannerText = `🏆 ${this.selectedPlayerA} wins — ${winsA}/${totalMetrics} categories`;
      bannerClass = 'h2h-winner-banner winner-a';
    } else if (winsB > winsA) {
      bannerText = `🏆 ${this.selectedPlayerB} wins — ${winsB}/${totalMetrics} categories`;
      bannerClass = 'h2h-winner-banner winner-b';
    } else {
      bannerText = `⚔️ Tie — ${winsA}/${totalMetrics} categories each`;
      bannerClass = 'h2h-winner-banner tie';
    }

    document.getElementById('winnerDeclaration').innerHTML = `<div class="${bannerClass}">${bannerText}</div>`;
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
