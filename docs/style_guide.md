# DraftGuru Design Style Guide

## Design Philosophy

DraftGuru embraces a **"light retro analytics"** aesthetic—combining the precision and data-density of modern sports analytics platforms with playful retro computing vibes. The design should feel simultaneously **authoritative and fun**, like a sophisticated scouting department that doesn't take itself too seriously.

## Core Aesthetic Principles

### 1. **Retro-Modern Fusion**
The interface walks a line between vintage computing aesthetics and contemporary web design. Think "what if a 1980s sports statistician had access to modern data visualization?"

**How this manifests:**
- Monospace fonts for data and metrics (Azeret Mono)
- Bold display fonts for headings (Russo One)
- Subtle scanline overlays and dot-matrix patterns
- Pixel-corner accent elements on hover states
- Ticker-style scrolling elements reminiscent of sports scoreboards

### 2. **Data First, But Friendly**
Analytics platforms can feel intimidating. DraftGuru presents dense information in digestible, visual chunks with personality.

**How this manifests:**
- Tabular numbers for clean metric alignment
- Color-coded changes (green for positive, rose for negative)
- Generous whitespace around data tables
- Card-based layouts that chunk information
- Playful icons (sparkles, swords, activity waves) that add character without overwhelming

### 3. **Soft Color Palette with Punchy Accents**
The base palette is soft and neutral (slate grays, off-whites) but sections are identified by vibrant accent colors that create visual landmarks.

**Color strategy:**
- **Base:** White to slate-50 gradient background, slate-900 text
- **Primary brand:** Cool blue (#4A7FB8) for navigation, warm peach (#E8B4A8) for footer
- **Section accents:**
  - Emerald green → Consensus data, positive changes, "official" info
  - Indigo → Interactive elements, prospects, clickable items
  - Fuchsia → Special features (VS Arena comparisons)
  - Cyan → Live updates and feeds
  - Amber → Promotions and warnings

### 4. **Playful Interactivity**
Interactions should feel responsive and delightful without being distracting. Subtle animations reward exploration.

**How this manifests:**
- Smooth transitions on hover states (0.2s timing)
- Pixel corner accents that appear on card hovers
- Toggle buttons with clear active/inactive states
- Color-coded winner/loser highlighting in comparisons
- Gentle shadows that lift elements on hover

### 5. **Sports Scoreboard Influence**
Visual references to traditional sports media—tickers, bold statistics, rapid updates—create familiarity for sports fans.

**How this manifests:**
- Animated ticker for market moves
- ALL-CAPS section headers with monospace fonts
- Colored rings around content cards (like TV graphics packages)
- Grid-based stat displays with labels + values
- "Live" feed elements that feel real-time

## Typography System

### Heading Font: Russo One
Bold, geometric, attention-grabbing. Used sparingly for brand identity and major section headers. Evokes sports jerseys and retro arcade games.

### Monospace Font: Azeret Mono
Clean, readable, technical. Used for:
- All numerical data
- Section headers (uppercase)
- Labels and metadata
- Any content that benefits from fixed-width alignment

### Body Font: System UI
Native system fonts ensure fast loading and readability for longer text content. Stays out of the way.

## Layout Patterns

### Container Strategy
Content lives in a centered container (max-width ~1152px, 80% width on large screens) with ample padding. This creates focus and prevents eye strain from edge-to-edge layouts.

### Section Dividers
Subtle gradient dividers (transparent → gray → transparent) create breathing room between major sections without harsh lines.

### Card System
Nearly all content lives in white cards with rounded corners and subtle shadows. Cards can have:
- Colored outline rings for emphasis
- Special background patterns (dot-matrix tiles)
- Hover effects (transform, shadow)
- Internal structure (header, content sections)

### Grid Flexibility
Responsive grids adapt gracefully:
- Prospect cards: auto-fill columns based on available space
- Footer: 4 columns → 1 column on mobile
- VS Arena: 3-column layout → single column on small screens

## Component Patterns

### Data Tables
- Clean borders (1px slate-200)
- Hover states on rows (light background)
- Uppercase monospace headers
- Tabular numbers in data columns
- Links styled with indigo color + underline on hover

### Badges & Pills
Small UI elements use:
- Rounded corners (full rounded for badges, slight for pills)
- Background + border in related colors (emerald for positive, rose for negative)
- Tight padding
- Uppercase micro-typography

### Buttons & Toggles
- Clear active/inactive states with color + border changes
- Hover states that invite interaction
- Consistent sizing and padding
- CTA buttons use emerald green with white text

### Icons
Inline SVG icons using stroke (not fill) for consistency. Icons add visual interest and aid scanning but never replace clear text labels.

## Visual Hierarchy

1. **Brand/Navigation** - Fixed blue bar at top with search
2. **Section Headers** - Large, bold, colored left-border accent
3. **Card Containers** - White backgrounds with colored rings
4. **Content Within Cards** - Tables, grids, comparison tools
5. **Metadata/Labels** - Small, gray, monospace

## Responsive Philosophy

Mobile-first approach: elements stack vertically and simplify on smaller screens. The design prioritizes **content accessibility** over preserving desktop layouts. Complex features (like VS Arena) adapt to single-column layouts rather than becoming unusable.

## Animation Guidelines

- **Duration:** 0.2s for most transitions (fast, not sluggish)
- **Easing:** Default ease for most cases
- **What to animate:** background colors, transforms, opacity, shadows
- **What NOT to animate:** Layout shifts, width/height changes that cause reflow
- **Special cases:** Ticker uses linear infinite animation (35s loop)

## Accessibility Considerations

- Sufficient color contrast for all text
- Semantic HTML structure (nav, main, section, footer)
- ARIA labels on interactive elements
- Hover states don't rely solely on color changes
- Focus states visible on keyboard navigation

## Future Expansion

When building new features, ask:
- **Does this feel like sports media?** (scoreboard aesthetic, bold stats)
- **Does this balance data with personality?** (retro elements, playful accents)
- **Is the hierarchy clear?** (section colors, card structure)
- **Does it reward interaction?** (hover states, smooth transitions)
- **Is it mobile-friendly?** (stacks well, remains readable)

The goal is not rigid consistency but **cohesive personality**—every new feature should feel like it belongs in the DraftGuru universe.
