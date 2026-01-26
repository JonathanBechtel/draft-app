/**
 * Admin Players Page JavaScript
 * Handles image URL preview and validation
 */

(function () {
  'use strict';

  // Debounce helper
  function debounce(fn, delay) {
    let timeoutId;
    return function (...args) {
      clearTimeout(timeoutId);
      timeoutId = setTimeout(() => fn.apply(this, args), delay);
    };
  }

  // State for tracking validation
  let validationAbortController = null;

  /**
   * Preview an image from a URL
   * @param {string} url - The image URL to preview
   * @param {HTMLElement} previewContainer - The container to show the preview in
   */
  function previewImage(url, previewContainer) {
    const img = previewContainer.querySelector('.admin-image-preview__img');
    const placeholder = previewContainer.querySelector('.admin-image-preview__placeholder');
    const errorEl = previewContainer.querySelector('.admin-image-preview__error');

    // Reset state
    if (img) img.style.display = 'none';
    if (placeholder) placeholder.style.display = 'flex';
    if (errorEl) errorEl.style.display = 'none';

    if (!url || !url.trim()) {
      return;
    }

    // Create a temporary image to test loading
    const testImg = new Image();
    testImg.onload = function () {
      if (img) {
        img.src = url;
        img.style.display = 'block';
      }
      if (placeholder) placeholder.style.display = 'none';
    };
    testImg.onerror = function () {
      if (errorEl) {
        errorEl.textContent = 'Failed to load image';
        errorEl.style.display = 'block';
      }
      if (placeholder) placeholder.style.display = 'none';
    };
    testImg.src = url;
  }

  /**
   * Validate an image URL via the server API
   * @param {string} url - The URL to validate
   * @param {HTMLElement} statusEl - The status element to update
   */
  async function validateImageUrl(url, statusEl) {
    // Cancel any pending validation
    if (validationAbortController) {
      validationAbortController.abort();
    }

    if (!url || !url.trim()) {
      updateValidationStatus(statusEl, 'empty', null);
      return;
    }

    updateValidationStatus(statusEl, 'loading', null);

    validationAbortController = new AbortController();

    try {
      const formData = new FormData();
      formData.append('url', url);

      const response = await fetch('/admin/players/validate-image-url', {
        method: 'POST',
        body: formData,
        signal: validationAbortController.signal,
      });

      const result = await response.json();

      if (result.valid) {
        updateValidationStatus(statusEl, 'valid', result.content_type);
      } else {
        updateValidationStatus(statusEl, 'invalid', result.error);
      }
    } catch (error) {
      if (error.name === 'AbortError') {
        return; // Ignore aborted requests
      }
      updateValidationStatus(statusEl, 'error', 'Validation request failed');
    } finally {
      validationAbortController = null;
    }
  }

  /**
   * Update the validation status display
   * @param {HTMLElement} statusEl - The status element
   * @param {string} status - 'empty', 'loading', 'valid', 'invalid', 'error'
   * @param {string|null} message - Optional message to display
   */
  function updateValidationStatus(statusEl, status, message) {
    if (!statusEl) return;

    statusEl.className = 'admin-image-preview__status';
    statusEl.classList.add(`admin-image-preview__status--${status}`);

    switch (status) {
      case 'empty':
        statusEl.textContent = '';
        break;
      case 'loading':
        statusEl.textContent = 'Validating...';
        break;
      case 'valid':
        statusEl.textContent = 'Valid image';
        if (message) {
          statusEl.textContent += ` (${message})`;
        }
        break;
      case 'invalid':
      case 'error':
        statusEl.textContent = message || 'Invalid URL';
        break;
    }
  }

  /**
   * Initialize image preview functionality for a form
   */
  function initImagePreview() {
    const urlInput = document.getElementById('reference_image_url');
    const previewContainer = document.getElementById('image-preview-container');
    const statusEl = document.getElementById('image-validation-status');
    const validateBtn = document.getElementById('validate-image-btn');

    if (!urlInput || !previewContainer) {
      return;
    }

    // Debounced handlers
    const debouncedPreview = debounce(function (url) {
      previewImage(url, previewContainer);
    }, 500);

    const debouncedValidate = debounce(function (url) {
      validateImageUrl(url, statusEl);
    }, 1000);

    // Handle input changes
    urlInput.addEventListener('input', function (e) {
      const url = e.target.value;
      debouncedPreview(url);
      debouncedValidate(url);
    });

    // Handle validate button click
    if (validateBtn) {
      validateBtn.addEventListener('click', function (e) {
        e.preventDefault();
        const url = urlInput.value;
        previewImage(url, previewContainer);
        validateImageUrl(url, statusEl);
      });
    }

    // Initial preview if URL is already set
    if (urlInput.value) {
      previewImage(urlInput.value, previewContainer);
      validateImageUrl(urlInput.value, statusEl);
    }
  }

  // Initialize on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initImagePreview);
  } else {
    initImagePreview();
  }
})();
