"""Basketball-Reference player-bio scraping and ingestion.

The Summer League roster cron (``app/cli/summer_league_roster_runner.py``) and
the operator CLIs (``scripts/bbref_bio_scraper.py``,
``scripts/ingest_player_bios.py``) both drive this package; neither owns the
logic. Modules:

* ``bbref_parse`` -- pure HTML -> dataclass parsing of index and player pages.
* ``bbref_scrape`` -- HTTP fetch + on-disk page cache around those parsers.
* ``rows`` -- the ingest-side CSV row shape and its reader.
* ``matching`` -- resolving a scraped row to a canonical ``players_master`` id.
* ``persistence`` -- the writes a resolved row performs.
* ``ingest`` -- the CSV -> database ingest run itself.
"""
