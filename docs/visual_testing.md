# Visual Testing with Playwright

This document describes the workflow for screenshot-based visual verification, designed for AI agents working on UI changes.

## Overview

Visual tests use Playwright to capture screenshots of rendered pages. These screenshots serve as visual references that an AI agent can read and evaluate using multimodal capabilities.

**Key concept**: There is no automated pixel comparison. The AI captures screenshots and visually inspects them to verify:
- The UI looks correct after changes
- Nothing unintended broke
- The implementation matches design mockups

## Prerequisites

### One-Time Setup

Install Playwright browsers (required once):
```bash
make playwright.install
```

### Before Capturing Screenshots

Visual tests run against a live server. Start the server first:
```bash
make dev
```

## AI Workflow for Visual Verification

### Use Case 1: Verifying UI Changes Don't Break Things

When making CSS, template, or JS changes:

```bash
# 1. Capture screenshots before changes
make visual

# 2. Make your UI changes

# 3. Capture screenshots after changes
make visual

# 4. Read the screenshots to visually verify
#    (AI uses Read tool on tests/visual/screenshots/*.png)
```

The AI can read PNG files directly and visually evaluate whether the changes look correct and nothing else broke.

### Use Case 2: Implementing from Mockups

Design mockups are located in `/mockups`:
- `mockups/draftguru_homepage.html` - Homepage design reference
- `mockups/draftguru_player.html` - Player page design reference
- `mockups/draftguru_news_homepage.html` - News section design reference

When implementing UI to match a mockup:

1. **Read the mockup** to understand the target design
2. **Implement the changes** in templates/CSS
3. **Capture screenshots** with `make visual`
4. **Compare visually** - read both the mockup and screenshot to verify alignment

### Use Case 3: Quick Visual Check

For a fast sanity check during development:

```bash
# Capture specific page screenshot
make visual TEST=homepage_full

# Read the screenshot to verify
# (screenshot saved to tests/visual/screenshots/homepage_full.png)
```

## Commands

| Command | Description |
|---------|-------------|
| `make visual` | Run all visual tests, save screenshots to `screenshots/` |
| `make visual TEST=<name>` | Run specific test by name (partial match) |
| `make visual.headed` | Run with visible browser (for debugging) |
| `make playwright.install` | Install Playwright browsers (one-time) |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TEST_BASE_URL` | `http://localhost:8000` | Server URL to test against |
| `PLAYWRIGHT_HEADLESS` | `1` | Set to `0` for visible browser |

## Screenshot Locations

After running `make visual`, screenshots are saved to:

```
tests/visual/screenshots/
├── homepage_full.png        # Full homepage
├── homepage_desktop.png     # Desktop viewport (1280x800)
├── homepage_tablet.png      # Tablet viewport (900x800)
├── homepage_mobile.png      # Mobile viewport (375x667)
├── sidebar_desktop.png      # Sidebar component
├── news_hero.png            # News hero section
├── news_grid_sidebar.png    # News grid with sidebar
└── ...
```

This location is excluded from linting and version control.

## Writing New Visual Tests

### Directory Structure

```
tests/visual/
├── __init__.py
├── conftest.py          # Shared fixtures
└── test_homepage.py     # Homepage tests (add more test_*.py files as needed)
```

### Example: Adding Tests for a New Page

```python
# tests/visual/test_player_page.py
"""Visual tests for player detail pages."""

from playwright.sync_api import Page, expect


class TestPlayerPage:
    """Visual tests for player detail page."""

    def test_player_page_loads(self, page: Page, goto) -> None:
        """Verify player page structure."""
        goto("/players/cooper-flagg")
        expect(page.locator(".player-bio")).to_be_visible()
        expect(page.locator(".stats-section")).to_be_visible()

    def test_player_page_screenshot(self, page: Page, goto, screenshot) -> None:
        """Capture player page for visual review."""
        goto("/players/cooper-flagg")
        screenshot.capture_full_page("player_cooper_flagg")
```

### Available Fixtures

```python
# Navigation - handles base URL and waits for load
goto("/path")

# Viewport presets
desktop_page   # 1280x800
tablet_page    # 900x800
mobile_page    # 375x667

# Screenshot helper
screenshot.capture_full_page("name")      # Full scrollable page
screenshot.capture_viewport("name")       # Visible viewport only
screenshot.capture_element(".sel", "name") # Specific element
```

## AI Instructions for Visual Verification

When an AI agent needs to verify visual changes:

1. **Capture**: Run `make visual` to generate screenshots
2. **Read**: Use the Read tool on `tests/visual/screenshots/<name>.png` to view the image
3. **Evaluate**: Visually assess whether:
   - The intended changes are present
   - No unintended visual regressions occurred
   - The layout matches expectations or mockups
4. **Report**: Describe any issues found or confirm the UI looks correct

For mockup comparison:
1. Read the mockup HTML file from `mockups/`
2. Read the captured screenshot
3. Compare key elements: layout, spacing, typography, colors
4. Note any significant deviations

## Troubleshooting

### "Browser not found"
```bash
make playwright.install
```

### Connection refused
Ensure the server is running:
```bash
make dev  # In another terminal
```

### Screenshots look wrong
- Check viewport settings in test
- Verify CSS is loading (use `make visual.headed` to debug)
- Add wait time if async JS needs to render
