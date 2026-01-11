/**
 * Export Modal - Handles share card image generation and download
 */

const ExportModal = {
  modalEl: null,
  contentEl: null,
  currentUrl: null,
  currentFilename: null,

  /**
   * Initialize the export modal
   */
  init() {
    this.createModalHTML();
    this.attachEventListeners();
  },

  /**
   * Create and append the modal HTML to the document
   */
  createModalHTML() {
    const modal = document.createElement('div');
    modal.id = 'exportModal';
    modal.className = 'export-modal';
    modal.innerHTML = `
      <div class="export-modal__backdrop"></div>
      <div class="export-modal__content">
        <div class="export-modal__header">
          <h3 class="export-modal__title">Save as Image</h3>
          <button class="export-modal__close" aria-label="Close">
            <svg viewBox="0 0 24 24" width="24" height="24" stroke="currentColor" stroke-width="2" fill="none">
              <path d="M18 6L6 18M6 6l12 12"/>
            </svg>
          </button>
        </div>
        <div class="export-modal__body">
          <div class="export-modal__loading">
            <div class="export-modal__spinner"></div>
            <p>Generating image...</p>
          </div>
          <div class="export-modal__preview" style="display: none;">
            <img class="export-modal__image" src="" alt="Preview" />
          </div>
          <div class="export-modal__error" style="display: none;">
            <p class="export-modal__error-text"></p>
            <button class="export-modal__retry btn btn--secondary">Retry</button>
          </div>
        </div>
        <div class="export-modal__footer" style="display: none;">
          <a class="export-modal__download btn btn--primary" download="">
            <svg viewBox="0 0 24 24" width="18" height="18" stroke="currentColor" stroke-width="2" fill="none">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
            Download
          </a>
        </div>
      </div>
    `;
    document.body.appendChild(modal);
    this.modalEl = modal;
    this.contentEl = modal.querySelector('.export-modal__content');
  },

  /**
   * Attach event listeners for modal interactions
   */
  attachEventListeners() {
    // Close button
    this.modalEl.querySelector('.export-modal__close').addEventListener('click', () => {
      this.hide();
    });

    // Backdrop click
    this.modalEl.querySelector('.export-modal__backdrop').addEventListener('click', () => {
      this.hide();
    });

    // Retry button
    this.modalEl.querySelector('.export-modal__retry').addEventListener('click', () => {
      if (this.lastRequest) {
        this.export(
          this.lastRequest.component,
          this.lastRequest.playerIds,
          this.lastRequest.context
        );
      }
    });

    // Escape key
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.modalEl.classList.contains('active')) {
        this.hide();
      }
    });
  },

  /**
   * Export an image for a component
   * @param {string} component - Component type (vs_arena, performance, h2h, comps)
   * @param {number[]} playerIds - Array of player IDs
   * @param {Object} context - Export context options
   */
  async export(component, playerIds, context = {}) {
    this.lastRequest = { component, playerIds, context };
    this.show();
    this.showLoading();

    try {
      const response = await fetch('/api/export/image', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          component,
          player_ids: playerIds,
          context: {
            comparison_group: context.comparisonGroup || 'current_draft',
            same_position: context.samePosition || false,
            metric_group: context.metricGroup || 'anthropometrics',
          },
        }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP ${response.status}`);
      }

      const data = await response.json();
      this.showPreview(data.url, data.filename, data.title);
    } catch (err) {
      console.error('Export failed:', err);
      this.showError(err.message || 'Failed to generate image');
    }
  },

  /**
   * Show the modal
   */
  show() {
    this.modalEl.classList.add('active');
    document.body.style.overflow = 'hidden';
  },

  /**
   * Hide the modal
   */
  hide() {
    this.modalEl.classList.remove('active');
    document.body.style.overflow = '';
    this.currentUrl = null;
    this.currentFilename = null;
  },

  /**
   * Show the loading state
   */
  showLoading() {
    this.modalEl.querySelector('.export-modal__loading').style.display = 'flex';
    this.modalEl.querySelector('.export-modal__preview').style.display = 'none';
    this.modalEl.querySelector('.export-modal__error').style.display = 'none';
    this.modalEl.querySelector('.export-modal__footer').style.display = 'none';
  },

  /**
   * Show the preview with image
   * @param {string} url - Image URL
   * @param {string} filename - Download filename
   * @param {string} title - Image title
   */
  showPreview(url, filename, title) {
    this.currentUrl = url;
    this.currentFilename = filename;

    const img = this.modalEl.querySelector('.export-modal__image');
    const downloadBtn = this.modalEl.querySelector('.export-modal__download');
    const titleEl = this.modalEl.querySelector('.export-modal__title');

    img.src = url;
    downloadBtn.href = url;
    downloadBtn.download = filename;
    titleEl.textContent = title || 'Save as Image';

    this.modalEl.querySelector('.export-modal__loading').style.display = 'none';
    this.modalEl.querySelector('.export-modal__preview').style.display = 'block';
    this.modalEl.querySelector('.export-modal__error').style.display = 'none';
    this.modalEl.querySelector('.export-modal__footer').style.display = 'flex';
  },

  /**
   * Show an error state
   * @param {string} message - Error message
   */
  showError(message) {
    this.modalEl.querySelector('.export-modal__loading').style.display = 'none';
    this.modalEl.querySelector('.export-modal__preview').style.display = 'none';
    this.modalEl.querySelector('.export-modal__error').style.display = 'flex';
    this.modalEl.querySelector('.export-modal__footer').style.display = 'none';
    this.modalEl.querySelector('.export-modal__error-text').textContent = message;
  },
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  ExportModal.init();
});

// Export for global access
window.ExportModal = ExportModal;
