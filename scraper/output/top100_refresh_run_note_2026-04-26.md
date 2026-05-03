# Top 100 Refresh Run Note - 2026-04-26

## Primary Source

- Source: The Athletic 2026 NBA Draft Top 100 via NBA.com
- URL: https://www.nba.com/news/the-athletic-2026-nba-draft-top-100-prospects
- Publication date: 2026-01-13
- Retrieval date: 2026-04-26

## Secondary Context

- ESPN 2026 NBA draft big board rankings: Top 100 prospects, published April 2026; used only as secondary context because the full table is not available in this repo. Basketball Reference is the preferred downstream player-data source whenever a prospect has an available BBRef player page.

## Generated Artifacts

- `scraper/output/top100_source_snapshot_2026-04-26.csv`
- `scraper/output/school_resolution_review_2026-04-26.csv`
- `scraper/output/player_resolution_plan_2026-04-26.csv`

## Known Limitations

- The primary source is an accessible NBA.com republication of The Athletic's first
  2026 draft-cycle Top 100 board. A newer ESPN board was visible in search snippets
  but was not used as the frozen source because the complete structured table was
  not available for review in this repo.
- Ages are source-provided as whole years on 2026 draft day.
- Heights and affiliations preserve source spelling in the immutable snapshot.
- Professional and international clubs are intentionally mapped as non-college
  affiliations with blank canonical college names in the school review artifact.
- Player resolution is DB-backed when `DATABASE_URL` is set. Without DB access,
  the generated plan marks rows for manual review instead of creating duplicate
  stubs.
- Use Basketball Reference as the preferred identity, bio, and statistical data
  source whenever a prospect has an available BBRef page. For prospects without a
  BBRef page, use reviewed official school/team roster pages before other sources.
