"""The scraped-bio CSV row shape and its reader.

``BioRow`` mirrors the columns ``bbref_scrape.scrape_letters`` writes, which is
the seam between the scrape half of this package and the ingest half: the
scraper emits a CSV, the ingester reads one back.
"""

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


def _coerce_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    return value.strip().lower() == "true"


@dataclass
class BioRow:
    slug: str
    url: str
    full_name: str
    birth_date: Optional[str]
    birth_city: Optional[str]
    birth_state_province: Optional[str]
    birth_country: Optional[str]
    shoots: Optional[str]
    school: Optional[str]
    high_school: Optional[str]
    draft_year: Optional[int]
    draft_round: Optional[int]
    draft_pick: Optional[int]
    draft_team: Optional[str]
    nba_debut_date: Optional[str]
    nba_debut_season: Optional[str]
    is_active_nba: Optional[bool]
    current_team: Optional[str]
    nba_last_season: Optional[str]
    position: Optional[str]
    height_in: Optional[int]
    weight_lb: Optional[int]
    social_x_handle: Optional[str]
    social_x_url: Optional[str]
    social_instagram_handle: Optional[str]
    social_instagram_url: Optional[str]
    source_url: str
    scraped_at: str


def load_bio_rows(csv_path: Path) -> List[BioRow]:
    """Read a scraped-bio CSV into ``BioRow`` records.

    Args:
        csv_path: Path to a CSV written by the bbref bio scraper.

    Returns:
        One ``BioRow`` per CSV data row, in file order.
    """
    rows: List[BioRow] = []
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for d in reader:
            rows.append(
                BioRow(
                    slug=d.get("slug") or "",
                    url=d.get("url") or d.get("source_url") or "",
                    full_name=d.get("full_name") or "",
                    birth_date=d.get("birth_date") or None,
                    birth_city=d.get("birth_city") or None,
                    birth_state_province=d.get("birth_state_province") or None,
                    birth_country=d.get("birth_country") or None,
                    shoots=d.get("shoots") or None,
                    school=d.get("school") or None,
                    high_school=d.get("high_school") or None,
                    draft_year=int(d["draft_year"]) if d.get("draft_year") else None,
                    draft_round=int(d["draft_round"]) if d.get("draft_round") else None,
                    draft_pick=int(d["draft_pick"]) if d.get("draft_pick") else None,
                    draft_team=d.get("draft_team") or None,
                    nba_debut_date=d.get("nba_debut_date") or None,
                    nba_debut_season=d.get("nba_debut_season") or None,
                    is_active_nba=_coerce_bool(d.get("is_active_nba")),
                    current_team=d.get("current_team") or None,
                    nba_last_season=d.get("nba_last_season") or None,
                    position=d.get("position") or None,
                    height_in=int(float(d["height_in"]))
                    if d.get("height_in")
                    else None,
                    weight_lb=int(float(d["weight_lb"]))
                    if d.get("weight_lb")
                    else None,
                    social_x_handle=d.get("social_x_handle") or None,
                    social_x_url=d.get("social_x_url") or None,
                    social_instagram_handle=d.get("social_instagram_handle") or None,
                    social_instagram_url=d.get("social_instagram_url") or None,
                    source_url=d.get("source_url") or d.get("url") or "",
                    scraped_at=d.get("scraped_at") or "",
                )
            )
    return rows
