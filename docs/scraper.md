# Draft Combine Scraper

This repo includes a CLI scraper that collects NBA Draft Combine data from nba.com for:

- Shooting drills
- Anthropometric measurements (anthro)
- Strength and agility drills

It normalizes column names, splits player names into components, and converts height-like values to inches.

## Installation

- Ensure dependencies are installed (from the project root):
  - `pip install -r requirements.txt`
  - For headless page rendering (default), install the browser once:
    - `python -m playwright install chromium`

## Running

You can use either the Make target or call the script directly.

- All seasons (2000-01 through 2025-26), all sources:
  - `make scrape`
- Single season:
  - `make scrape YEAR=2024-25`
- Single source (one of `shooting`, `anthro`, `agility`):
  - `make scrape SOURCE=anthro`
- Extra CLI args (e.g., longer timeout, verbose logging):
  - `make scrape YEAR=2023-24 ARGS="--verbose --timeout 90"`

Direct calls:
- `python scripts/nba_draft_scraper.py --year 2024-25 --source all`

Outputs are written to `scraper/output/{season}_{source}.csv`. The output folder is ignored by git.

## CLI Options

- `--year` Optional season in `YYYY-YY`. Default scrapes all seasons.
- `--source` Data source: `all` (default), `shooting`, `anthro`, or `agility`.
- `--out-dir` Output directory (default: `scraper/output`).
- `--timeout` Per-request timeout in seconds (default: 30). Increase if pages render slowly.
- `--verbose` Print progress logs and row counts.
- `--no-headless` Disable headless rendering and use raw HTML fetch (useful with local files).
- `--from-file-shooting|--from-file-anthro|--from-file-agility` Optional local HTML overrides; if provided, the script parses those files instead of fetching.

## How It Works

- The scraper first attempts to load the nba.com page (headless Chromium) and parse the HTML table with BeautifulSoup.
- If the table fails to load (due to client-side rendering or bot protection) the scraper falls back to the NBA Stats API for that source.
- Column names are normalized to lowercase snake_case. For example:
  - `PLAYER_NAME` → `player_name`
  - `offTheDribbleShootingMade` → `off_the_dribble_shooting_made`
- Player names are split into: `prefix`, `first_name`, `middle_name`, `last_name`, `suffix`. Apostrophes in surnames are preserved.
- Height-like columns (e.g., height without shoes, wingspan, standing reach) are converted to total inches.

## Season and URL Mapping

- `anthro` and `agility` endpoints use the full season: `YYYY-YY`.
- `shooting` uses the end year for its `SeasonYear` parameter:
  - Example: `2023-24` season maps to `SeasonYear=2024` for shooting drills.
- For older seasons, the scraper also tries the “spot shooting” variants when needed (HTML and API).

## Tips & Troubleshooting

- If CSVs are empty, increase `--timeout` and use `--verbose` to see where it’s waiting. Example:
  - `python scripts/nba_draft_scraper.py --year 2023-24 --source all --timeout 90 --verbose`
- If your network blocks headless/API, you can provide local HTML tables via the `--from-file-*` flags. Sample HTML fixtures live under `scraper/`.
- You can disable headless with `--no-headless`, but for live pages headless is recommended so the parser sees fully rendered tables.

## Output Schema Notes

- Each CSV includes `season` and (when present) `pos`.
- All original table columns are preserved, normalized to snake_case.
- Missing values (e.g., `-`) are saved as empty fields.

## Examples

- Parse 2024-25 using local samples (offline testing):
  - `python scripts/nba_draft_scraper.py --year 2024-25 --source all --from-file-shooting scraper/combine_shooting.txt --from-file-anthro scraper/combine_anthro.txt --from-file-agility scraper/combine_agility.txt --no-headless`
- Parse 2023-24 (headless + API fallback, verbose):
  - `python scripts/nba_draft_scraper.py --year 2023-24 --source all --timeout 90 --verbose`

## Notes

- nba.com content is client-rendered and sometimes protected; headless + API fallback is used to improve reliability.
- For dependable historical reruns, consider caching full HTML pages locally and pointing the script at those files.

