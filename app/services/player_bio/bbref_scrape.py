"""Fetch Basketball-Reference index and player pages, with an on-disk cache.

The cache under ``data/scraper-cache/`` is what keeps re-runs deterministic and
offline: a page already on disk is reused unless ``refresh=True``, and a failed
fetch falls back to the cached copy when one exists.
"""

import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

import httpx

from app.services.player_bio.bbref_parse import (
    IndexRow,
    derive_season_from_index,
    parse_index_html,
    parse_player_html,
    parse_slug_from_player_html,
)
from app.utils.network_guard import guarded_httpx_event_hooks

USER_AGENT = "nbadraft-bio-scraper/0.1"


def _client(timeout: float = 30.0) -> httpx.Client:
    headers = {"User-Agent": USER_AGENT}
    return httpx.Client(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
        event_hooks=guarded_httpx_event_hooks(),
    )


def _save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fetch_player_html(
    slug: str,
    source_url: str,
    cache_dir: Path,
    client: Optional[httpx.Client],
    refresh: bool,
    throttle: float,
    verbose: bool,
) -> str:
    cache_path = cache_dir / f"{slug}.html"
    if not refresh and cache_path.exists():
        if verbose:
            print(f"[cache] player {slug}")
        return cache_path.read_text(encoding="utf-8", errors="ignore")
    if client is None:
        return (
            cache_path.read_text(encoding="utf-8", errors="ignore")
            if cache_path.exists()
            else ""
        )
    try:
        if verbose:
            print(f"[info] fetch player {source_url}")
        resp = client.get(source_url)
        resp.raise_for_status()
        html = resp.text
        _save_text(cache_path, html)
        if throttle > 0:
            time.sleep(throttle)
        return html
    except Exception as exc:
        if verbose:
            print(f"[warn] failed to fetch {source_url}: {exc}")
        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8", errors="ignore")
        return ""


def scrape_letters(
    letters: Iterable[str],
    out_dir: Path,
    throttle: float = 3.0,
    from_index_dir: Optional[Path] = None,
    from_player_dir: Optional[Path] = None,
    from_index_file: Optional[Path] = None,
    from_player_file: Optional[Path] = None,
    verbose: bool = False,
    timeout: float = 30.0,
    refresh: bool = False,
    extra_slugs: Optional[Iterable[str]] = None,
) -> List[Dict[str, object]]:
    client = _client(timeout=timeout)
    cache_dir = Path("data/scraper-cache")
    player_cache_dir = cache_dir / "players"
    rows_out: List[Dict[str, object]] = []
    seen_slugs: Set[str] = set()
    # Optional: sample slug restriction when using a single player sample file
    sample_slug: Optional[str] = None
    sample_letter: Optional[str] = None
    if from_player_file and from_player_file.exists():
        sample_raw = from_player_file.read_text(encoding="utf-8", errors="ignore")
        parsed = parse_slug_from_player_html(sample_raw)
        if parsed:
            sample_letter, sample_slug = parsed

    for letter in letters:
        # Load index HTML
        if from_index_file and from_index_file.exists():
            raw = from_index_file.read_text(encoding="utf-8", errors="ignore")
        elif from_index_dir:
            path_specific = from_index_dir / f"players_{letter}.html"
            if path_specific.exists():
                raw = path_specific.read_text(encoding="utf-8", errors="ignore")
            else:
                # fallback to example name
                example = from_index_dir / "index_page_example.html"
                raw = example.read_text(encoding="utf-8", errors="ignore")
        else:
            url = f"https://www.basketball-reference.com/players/{letter}/"
            cache_path = cache_dir / f"players_{letter}.html"
            if not refresh and cache_path.exists():
                raw = cache_path.read_text(encoding="utf-8", errors="ignore")
                if verbose:
                    print(f"[cache] index players_{letter}.html")
            else:
                if verbose:
                    print(f"[info] fetch index {url}")
                resp = client.get(url)
                resp.raise_for_status()
                raw = resp.text
                _save_text(cache_path, raw)
                time.sleep(throttle)
        idx_rows = parse_index_html(letter, raw)
        # If a sample player file is provided, restrict to that slug only
        if sample_slug:
            idx_rows = [r for r in idx_rows if r.slug == sample_slug]
            # If still empty, synthesize a minimal row to proceed
            if not idx_rows and sample_letter:
                # build minimal based on player page content
                source_url_guess = f"https://www.basketball-reference.com/players/{sample_letter}/{sample_slug}.html"
                # Use the player page file to fill some details
                if from_player_file and from_player_file.exists():
                    phtml = from_player_file.read_text(
                        encoding="utf-8", errors="ignore"
                    )
                    bio = parse_player_html(
                        sample_letter, sample_slug, phtml, source_url_guess
                    )
                    # Create minimal index row
                    idx_rows = [
                        IndexRow(
                            letter=sample_letter,
                            slug=sample_slug,
                            name=bio.full_name,
                            pos=bio.position,
                            year_min=None,
                            year_max=None,
                            height_in=bio.height_in,
                            weight_lb=bio.weight_lb,
                            birth_date=bio.birth_date,
                            colleges=bio.school,
                            active_flag=True,
                        )
                    ]

        for idx in idx_rows:
            if not idx.slug:
                continue
            # Build basic record from index
            is_active, last_season = derive_season_from_index(idx)
            slug_letter = idx.slug[0]
            source_url = f"https://www.basketball-reference.com/players/{slug_letter}/{idx.slug}.html"
            # Fetch or read player page
            if from_player_file and from_player_file.exists():
                phtml = from_player_file.read_text(encoding="utf-8", errors="ignore")
            elif from_player_dir and (from_player_dir / f"{idx.slug}.html").exists():
                phtml = (from_player_dir / f"{idx.slug}.html").read_text(
                    encoding="utf-8", errors="ignore"
                )
            else:
                phtml = _fetch_player_html(
                    slug=idx.slug,
                    source_url=source_url,
                    cache_dir=player_cache_dir,
                    client=client,
                    refresh=refresh,
                    throttle=throttle,
                    verbose=verbose,
                )

            bio = parse_player_html(letter, idx.slug, phtml, source_url)
            # Carry index hints
            bio.is_active_nba = is_active
            bio.nba_last_season = last_season
            # Prefer index height/weight when meta parsing failed
            if bio.height_in is None:
                bio.height_in = idx.height_in
            if bio.weight_lb is None:
                bio.weight_lb = idx.weight_lb
            # Prefer index birth_date when meta missing
            if not bio.birth_date:
                bio.birth_date = idx.birth_date
            # Prefer index position if missing
            if not bio.position:
                bio.position = idx.pos

            rows_out.append(bio.__dict__)
            seen_slugs.add(idx.slug)

    if extra_slugs:
        for slug in extra_slugs:
            normalized = slug.strip().lower()
            if not normalized or normalized in seen_slugs:
                continue
            slug_letter = normalized[0]
            source_url = f"https://www.basketball-reference.com/players/{slug_letter}/{normalized}.html"
            if from_player_dir and (from_player_dir / f"{normalized}.html").exists():
                phtml = (from_player_dir / f"{normalized}.html").read_text(
                    encoding="utf-8", errors="ignore"
                )
            else:
                phtml = _fetch_player_html(
                    slug=normalized,
                    source_url=source_url,
                    cache_dir=player_cache_dir,
                    client=client,
                    refresh=refresh,
                    throttle=throttle,
                    verbose=verbose,
                )
            if not phtml:
                if verbose:
                    print(f"[warn] no HTML for slug {normalized}; skipping")
                continue
            bio = parse_player_html(slug_letter, normalized, phtml, source_url)
            rows_out.append(bio.__dict__)
            seen_slugs.add(normalized)
    return rows_out
