/**
 * Tweet Share - Generates a share card and opens an X/Twitter intent
 */
const TweetShare = {
  /**
   * Format comparison group for display.
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
   * Format metric group for display.
   * @param {string} metric
   * @returns {string}
   */
  formatMetricGroup(metric) {
    const map = {
      anthropometrics: 'Anthropometrics',
      combine: 'Combine Performance',
      shooting: 'Shooting',
      advanced: 'Advanced'
    };
    return map[metric] || 'Metrics';
  },

  /**
   * Format position filter for display.
   * @param {boolean} samePosition
   * @returns {string}
   */
  formatPositionScope(samePosition) {
    return samePosition ? 'Same Position' : 'All Positions';
  },

  /**
   * Build context summary for tweet text.
   * @param {Object} context
   * @returns {string}
   */
  formatContextSummary(context) {
    if (!context) return '';
    const group = this.formatComparisonGroup(context.comparisonGroup);
    const metric = this.formatMetricGroup(context.metricGroup);
    const position = this.formatPositionScope(context.samePosition);
    return `${group} • ${metric} • ${position}`;
  },

  /**
   * Build tweet text from a headline.
   * @param {Object} options
   * @param {string} [options.text]
   * @returns {string}
   */
  buildTweetText({ text }) {
    const headline = (text || 'DraftGuru').trim();
    return headline;
  },

  /**
   * Build a share URL that renders a Twitter card image, and redirects humans back
   * to the original page.
   * @param {Object} options
   * @param {string} [options.shareUrl]
   * @param {string} [options.title]
   * @param {string} options.pageUrl
   * @returns {string}
   */
  buildShareUrl({ shareUrl, title, pageUrl }) {
    if (!shareUrl) return pageUrl;

    return new URL(shareUrl, window.location.origin).toString();
  },

  /**
   * Open the X/Twitter intent window.
   * @param {string} text - Tweet text.
   * @param {string} url - URL to include in the tweet.
   */
  openTweetIntent(text, url) {
    const intentUrl = new URL('https://twitter.com/intent/tweet');
    if (text) intentUrl.searchParams.set('text', text);
    if (url) intentUrl.searchParams.set('url', url);

    const opened = window.open(intentUrl.toString(), '_blank', 'noopener,noreferrer');
    if (!opened) {
      window.location.href = intentUrl.toString();
    }
  },

  /**
   * Generate an export image and open a tweet intent with it.
   * Falls back to sharing the page URL if export fails.
   * @param {Object} options
   * @param {string} options.component
   * @param {number[]} options.playerIds
   * @param {Object} [options.context]
   * @param {string} [options.text]
   * @param {string} [options.pageUrl]
   */
  async share({ component, playerIds, context, text, pageUrl }) {
    const pageLink = pageUrl || window.location.href;
    try {
      const page = new URL(pageLink, window.location.origin);
      const redirectPath = `${page.pathname}${page.search}`;

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
          },
          redirect_path: redirectPath
        }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP ${response.status}`);
      }

      const data = await response.json();
      const tweetText = this.buildTweetText({ text });
      const shareUrl = this.buildShareUrl({
        shareUrl: data.share_url,
        title: data.title,
        pageUrl: pageLink
      });
      this.openTweetIntent(tweetText, shareUrl);
    } catch (err) {
      console.error('Tweet share failed:', err);
      const tweetText = this.buildTweetText({ text });
      this.openTweetIntent(tweetText, pageLink);
    }
  },
};

// Export for global access
window.TweetShare = TweetShare;
