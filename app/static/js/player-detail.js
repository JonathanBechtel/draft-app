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
      playerPrimaryMeta.textContent = `${player.position} • ${player.college} • ${player.height} • ${player.weight}`;
    }

    // Update secondary meta
    const updates = {
      'playerAge': player.age,
      'playerClass': player.class,
      'playerHometown': player.hometown,
      'playerWingspan': player.wingspan
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

  /**
   * Initialize performance module
   */
  init() {
    const perfContainer = document.getElementById('perfBarsContainer');
    if (!perfContainer || !window.PERCENTILE_DATA) return;

    this.setupEventListeners();
    this.renderPercentiles();
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
        this.renderPercentiles();
      });
    }

    // Position checkbox
    const positionCheckbox = document.getElementById('perfPositionAdjusted');
    if (positionCheckbox) {
      positionCheckbox.addEventListener('change', (e) => {
        this.positionAdjusted = e.target.checked;
        this.renderPercentiles();
      });
    }

    // Category tabs
    const tabs = document.querySelectorAll('.perf-tab');
    tabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        tabs.forEach((t) => t.classList.remove('active'));
        tab.classList.add('active');
        this.currentCategory = tab.dataset.category;
        this.renderPercentiles();
      });
    });
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
  renderPercentiles() {
    const container = document.getElementById('perfBarsContainer');
    if (!container) return;

    const data = window.PERCENTILE_DATA[this.currentCategory] || [];

    const html = data.map((item) => {
      const percentileClass = this.getPercentileClass(item.percentile);

      return `
        <div class="perf-bar-row">
          <div class="perf-metric-label">${item.metric}</div>
          <div class="perf-bar-track">
            <div class="perf-bar-fill ${percentileClass}" style="width: ${item.percentile}%;"></div>
          </div>
          <div class="perf-values">
            <span class="perf-actual-value">${item.value}${item.unit}</span>
            <span class="perf-percentile-value ${percentileClass}">${item.percentile}th</span>
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

  /**
   * Initialize comparisons module
   */
  init() {
    const grid = document.getElementById('compResultsGrid');
    if (!grid || !window.COMPARISON_DATA) return;

    this.setupEventListeners();
    this.renderResults();
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
        this.renderResults();
      });
    });

    // Pool dropdown
    const poolSelect = document.getElementById('compPool');
    if (poolSelect) {
      poolSelect.addEventListener('change', (e) => {
        this.currentPool = e.target.value;
        this.renderResults();
      });
    }

    // Position checkbox
    const posCheckbox = document.getElementById('compPositionFilter');
    if (posCheckbox) {
      posCheckbox.addEventListener('change', (e) => {
        this.positionFilter = e.target.checked;
        this.renderResults();
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
   * Get similarity badge class
   */
  getSimilarityBadgeClass(similarity) {
    if (similarity >= 90) return 'similarity-badge-high';
    if (similarity >= 80) return 'similarity-badge-good';
    if (similarity >= 70) return 'similarity-badge-moderate';
    return 'similarity-badge-weak';
  },

  /**
   * Render comparison card
   */
  renderCompCard(player) {
    const badgeClass = this.getSimilarityBadgeClass(player.similarity);

    return `
      <div class="prospect-card" data-player="${player.name}">
        <div class="prospect-image-wrapper">
          <img src="${player.img}" alt="${player.name}" class="prospect-image" />
          <span class="similarity-badge ${badgeClass}">${player.similarity}%</span>
        </div>
        <div class="prospect-info">
          <h4 class="prospect-name">${player.name}</h4>
          <p class="prospect-meta">${player.position} • ${player.school}</p>
          <div class="prospect-stats">
            <div class="stat-pill">
              <span class="label">HT</span>
              <span class="value">${player.stats.ht}"</span>
            </div>
            <div class="stat-pill">
              <span class="label">WS</span>
              <span class="value">${player.stats.ws}"</span>
            </div>
            <div class="stat-pill">
              <span class="label">VRT</span>
              <span class="value">${player.stats.vert}"</span>
            </div>
          </div>
          <button class="comp-compare-btn" onclick="PlayerComparisonsModule.showComparison('${player.name}')">
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
  renderResults() {
    const grid = document.getElementById('compResultsGrid');
    if (!grid) return;

    const html = window.COMPARISON_DATA.map((player) => this.renderCompCard(player)).join('');
    grid.innerHTML = html;
  },

  /**
   * Show detailed comparison modal
   */
  showComparison(playerName) {
    this.selectedCompPlayer = window.COMPARISON_DATA.find((p) => p.name === playerName);
    if (!this.selectedCompPlayer) return;

    const modal = document.getElementById('compComparisonView');
    const title = document.getElementById('compComparisonTitle');
    const body = document.getElementById('compComparisonBody');

    if (title) {
      title.textContent = `${window.PLAYER_DATA.name} vs ${this.selectedCompPlayer.name}`;
    }

    if (body) {
      body.innerHTML = this.renderComparisonTable();
    }

    if (modal) {
      modal.classList.add('active');
      document.body.style.overflow = 'hidden';
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
   * Render comparison table for modal
   */
  renderComparisonTable() {
    if (!this.selectedCompPlayer) return '';

    const player = window.PLAYER_DATA;
    const comp = this.selectedCompPlayer;

    // Sample metrics for comparison
    const metrics = [
      { label: 'Height', playerA: player.height, playerB: `${comp.stats.ht}"` },
      { label: 'Wingspan', playerA: player.wingspan, playerB: `${comp.stats.ws}"` },
      { label: 'Vertical', playerA: '36"', playerB: `${comp.stats.vert}"` }
    ];

    const rows = metrics.map((m) => `
      <tr>
        <td class="text-right comp-table-value">${m.playerA}</td>
        <td class="text-center">${m.label}</td>
        <td class="text-left comp-table-value">${m.playerB}</td>
      </tr>
    `).join('');

    return `
      <div class="scanlines"></div>
      <table class="comp-table" style="position: relative;">
        <thead>
          <tr>
            <th class="text-right">${player.name}</th>
            <th class="text-center">Metric</th>
            <th class="text-left">${comp.name}</th>
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
 * H2H comparison with swap functionality and fixed Player A
 * ============================================================================
 */
const HeadToHeadModule = {
  selectedPlayerA: null,
  selectedPlayerB: null,
  currentCategory: 'anthropometrics',

  // Player data with full stats (reuses data from homepage)
  playerData: {},

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
      { key: 'ast', label: 'AST%', unit: '%', higherIsBetter: true },
      { key: 'blk', label: 'BLK%', unit: '%', higherIsBetter: true }
    ]
  },

  /**
   * Initialize the H2H module
   */
  init() {
    const playerBSelect = document.getElementById('h2hPlayerB');
    if (!playerBSelect) return;

    // Build player data from comparison data
    this.buildPlayerData();
    this.populateSelector();
    this.setupEventListeners();
    this.renderComparison();
  },

  /**
   * Build player data from various sources
   */
  buildPlayerData() {
    // Add current player
    if (window.PLAYER_DATA) {
      const p = window.PLAYER_DATA;
      this.playerData[p.name] = {
        img: p.photo_url,
        anthropometrics: { height: 81, weight: 205, wingspan: 86, standingReach: 108 },
        combinePerformance: { verticalLeap: 38.5, lane: 10.8, shuttle: 2.9, bench: 12 },
        advancedStats: { per: 28.4, ts: 61.2, ast: 23.1, blk: 5.8 }
      };
      this.selectedPlayerA = p.name;
    }

    // Add comparison players
    if (window.COMPARISON_DATA) {
      window.COMPARISON_DATA.forEach((comp) => {
        this.playerData[comp.name] = {
          img: comp.img,
          anthropometrics: {
            height: parseInt(comp.stats.ht) || 80,
            weight: 210,
            wingspan: parseInt(comp.stats.ws) || 84,
            standingReach: 106
          },
          combinePerformance: {
            verticalLeap: parseInt(comp.stats.vert) || 34,
            lane: 11.0,
            shuttle: 3.0,
            bench: 10
          },
          advancedStats: { per: 24.0, ts: 58.0, ast: 18.0, blk: 4.0 }
        };
      });
    }
  },

  /**
   * Populate player B selector
   */
  populateSelector() {
    const select = document.getElementById('h2hPlayerB');
    if (!select) return;

    select.innerHTML = '';
    Object.keys(this.playerData).forEach((name) => {
      if (name !== this.selectedPlayerA) {
        const option = document.createElement('option');
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
      }
    });

    if (select.options.length > 0) {
      this.selectedPlayerB = select.options[0].value;
    }
  },

  /**
   * Setup event listeners
   */
  setupEventListeners() {
    // Player B selector
    const select = document.getElementById('h2hPlayerB');
    if (select) {
      select.addEventListener('change', (e) => {
        this.selectedPlayerB = e.target.value;
        this.renderComparison();
      });
    }

    // Swap button
    const swapBtn = document.getElementById('h2hSwapBtn');
    if (swapBtn) {
      swapBtn.addEventListener('click', () => {
        const temp = this.selectedPlayerA;
        this.selectedPlayerA = this.selectedPlayerB;
        this.selectedPlayerB = temp;
        this.populateSelector();
        document.getElementById('h2hPlayerB').value = this.selectedPlayerB;
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
   * Calculate similarity score
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
    return Math.max(0, Math.round(100 - avgDiff));
  },

  /**
   * Render comparison
   */
  renderComparison() {
    if (!this.selectedPlayerA || !this.selectedPlayerB) return;
    if (!this.playerData[this.selectedPlayerA] || !this.playerData[this.selectedPlayerB]) return;

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
    document.getElementById('h2hHeaderA').textContent = this.selectedPlayerA;
    document.getElementById('h2hHeaderB').textContent = this.selectedPlayerB;

    // Render comparison table
    const metrics = this.categoryMetrics[this.currentCategory];
    let rowsHTML = '';
    let winsA = 0;
    let winsB = 0;

    metrics.forEach((metric) => {
      const valueA = playerAData[metric.key];
      const valueB = playerBData[metric.key];

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

    document.getElementById('h2hComparisonBody').innerHTML = rowsHTML;

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

    document.getElementById('h2hWinnerDeclaration').innerHTML = `<div class="${bannerClass}">${bannerText}</div>`;
  }
};

/**
 * ============================================================================
 * PLAYER FEED MODULE
 * Renders player-specific news feed
 * ============================================================================
 */
const PlayerFeedModule = {
  /**
   * Initialize the feed
   */
  init() {
    const feedContainer = document.getElementById('playerFeedContainer');
    if (!feedContainer || !window.PLAYER_FEED) return;

    feedContainer.innerHTML = this.renderFeed();
  },

  /**
   * Render all feed items
   */
  renderFeed() {
    return window.PLAYER_FEED.map((item) => {
      const tagClass = item.tag.toLowerCase().replace(' ', '-');

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
