# Top 100 Dev Merge Log - 2026-04-26

Environment: dev database.

Source plan: `scraper/output/top100_dev_merge_plan_2026-04-26.csv`

## Execution Summary

- Dry-run completed before writes.
- Merge execution completed and committed.
- Post-merge verifier result: 100 matched, 0 merge-required, 0 create-stub, 0 needs-manual-review.

## Applied Merge Groups

| Source rank | Source name | Keep ID | Discarded IDs | Child rows reassigned | Conflict rows discarded |
|---:|---|---:|---|---:|---:|
| 10 | Mikel Brown Jr. | 5389 | 5710 | 10 | 2 |
| 13 | Labaron Philon | 1709 | 5572 | 4 | 1 |
| 16 | Darius Acuff Jr. | 5384 | 5681, 6027 | 16 | 4 |
| 23 | Chris Cenac Jr. | 5387 | 5791 | 6 | 2 |
| 43 | Tarris Reed Jr. | 5483 | 5542, 6037 | 8 | 4 |
| 52 | Morez Johnson Jr. | 5529 | 5706, 6038 | 12 | 4 |
| 61 | Kwame Evans Jr. | 5640 | 5452 | 6 | 2 |
| 72 | Ja'Kobi Gillespie | 5467 | 6033 | 2 | 2 |
| 78 | William Kyle | 5798 | 5826 | 2 | 2 |

## Notes

- Discarded display names were preserved as aliases on the kept player records.
- Conflict rows were discarded only when a unique constraint already had an equivalent keep-player row, such as duplicate season stats or duplicate lifecycle rows.
- Keep-player canonical fields were updated from the reviewed Top 100 source plan: display name, school, school_raw, and draft_year.
- The Ja'Kobi Gillespie duplicate surfaced after the shared normalizer began treating curly and straight apostrophe variants as the same identity key.
