/**
 * Tweet Share - Generate shareable tweet links with exported images
 */

const TweetShare = {
  /**
   * Format comparison group for display
   * @param {string} group
   * @returns {string}
   */
  formatComparisonGroup(group) {
    const map = {
      current_draft: 'Current Draft Class',
      all_time_draft: 'Historical Prospects',
      current_nba: 'Active NBA Players',
      all_time_nba: 'All-Time NBA'
    };
    return map[group] || 'Draft Prospects';
  },

  /**
   * Format metric group for display
   * @param {string} metric
   * @returns {string}
   */
  formatMetricGroup(metric) {
    const map = {
      anthropometrics: 'Anthropometrics',
      combine: 'Combine Performance',
      shooting: 'Shooting'
    };
    return map[metric] || 'Metrics';
  },

  /**
   * Format position filter for display
   * @param {boolean} samePosition
   * @returns {string}
   */
  formatPositionScope(samePosition) {
    return samePosition ? 'Same Position' : 'All Positions';
  },

  /**
   * Build context summary for tweet text
   * @param {Object} context
   * @returns {string}
   */
  formatContextSummary(context) {
    if (!context) return 'Draft Prospects';
    const group = this.formatComparisonGroup(context.comparisonGroup);
    const metric = this.formatMetricGroup(context.metricGroup);
    const position = this.formatPositionScope(context.samePosition);
    return `${group} • ${metric} • ${position}`;
  },

  /**
   * Open a tweet intent URL in a new window
   * @param {string} text
   * @param {string} url
   */
  openTweetIntent(text, url) {
    const tweetUrl = new URL('https://twitter.com/intent/tweet');
    if (text) {
      tweetUrl.searchParams.set('text', text);
    }
    if (url) {
      tweetUrl.searchParams.set('url', url);
    }
    window.open(tweetUrl.toString(), '_blank', 'noopener');
  },

  /**
   * Generate an export image and open a tweet intent with it
   * @param {Object} options
   * @param {string} options.component
   * @param {number[]} options.playerIds
   * @param {Object} options.context
   * @param {string} options.text
   * @param {string} [options.pageUrl]
   */
  async share({ component, playerIds, context, text, pageUrl }) {
    const pageLink = pageUrl || window.location.href;
    const tweetText = `${text || 'DraftGuru'} ${pageLink}`.trim();

    try {
      const response = await fetch('/api/export/image', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          component,
          player_ids: playerIds,
          context: {
            comparison_group: context?.comparisonGroup || 'current_draft',
            same_position: context?.samePosition || false,
            metric_group: context?.metricGroup || 'anthropometrics'
          }
        })
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP ${response.status}`);
      }

      const data = await response.json();
      this.openTweetIntent(tweetText, data.url);
    } catch (err) {
      console.error('Tweet share failed:', err);
      this.openTweetIntent(tweetText, pageLink);
    }
  }
};

window.TweetShare = TweetShare;
