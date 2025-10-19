import argparse
import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup, Tag
from playwright.sync_api import sync_playwright


# ------------------------------
# Web page URL templates (HTML tables)
# ------------------------------
SHOOTING_URL = "https://www.nba.com/stats/draft/combine-shooting-drills?SeasonYear={season_year_shooting}"
SHOOTING_SPOT_URL = "https://www.nba.com/stats/draft/combine-spot-shooting?SeasonYear={season_year}"
ANTHRO_URL = "https://www.nba.com/stats/draft/combine-anthro?SeasonYear={season_year}"
AGILITY_URL = "https://www.nba.com/stats/draft/combine-strength-agility?SeasonYear={season_year}"


# ------------------------------
# Helpers: cleaning and parsing
# ------------------------------
_NBSP_RE = re.compile(r"\xa0|&nbsp;", flags=re.IGNORECASE)
_SPACEY_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^0-9a-zA-Z_]+")


def to_snake(text: str) -> str:
    """Convert header labels to lowercase snake_case.

    - Normalizes common symbols and HTML entities.
    - Keeps numbers (e.g., 3pt)
    - Replaces separators with single underscore.
    """
    if text is None:
        return ""
    t = text.strip()
    t = _NBSP_RE.sub(" ", t)
    # Insert underscores for CamelCase boundaries before lowercasing
    t = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", t)
    # Normalize common NBA labels
    t = t.replace("W/O", "wo").replace("W/", "w_")
    t = t.replace("W/O ", "wo_").replace("W/ ", "w_")
    t = t.replace("%", " pct")
    t = t.replace("3PT", "3pt")
    t = t.replace("Freethrow", "freethrow")
    t = t.replace("/", " ")
    t = re.sub(r"[()\-]", " ", t)
    t = re.sub(r"\s+", " ", t)
    t = t.strip().lower()
    # replace non word with underscore then collapse
    t = _NON_WORD_RE.sub("_", t)
    t = re.sub(r"_+", "_", t).strip("_")
    return t


def parse_height_like_to_inches(value: str) -> Optional[float]:
    """Parse a height-like string to total inches.

    Accepts forms like:
    - 6' 7" or 6' 7.25'' or 6-7
    - 8' 10.5''  (standing reach, wingspan style)
    If it's already a numeric (e.g., 77.0) and reasonably sized, assume inches.
    Returns None for blanks or dash.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "-":
        return None
    # Normalize quotes (curly to straight)
    s = s.replace("’’", '"').replace("’", "'")
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("inches", "").replace("inch", "").strip()

    # Patterns: feet and inches
    ft_in_match = re.match(r"^(\d+)\s*['-]\s*(\d+(?:\.\d+)?)\s*\"?$", s)
    ft_only_match = re.match(r"^(\d+)\s*'$", s)
    ft_in_alt = re.match(r"^(\d+)\s*ft\s*(\d+(?:\.\d+)?)\s*in$", s, flags=re.IGNORECASE)
    if ft_in_match:
        ft = float(ft_in_match.group(1))
        inch = float(ft_in_match.group(2))
        return ft * 12 + inch
    if ft_in_alt:
        ft = float(ft_in_alt.group(1))
        inch = float(ft_in_alt.group(2))
        return ft * 12 + inch
    if ft_only_match:
        ft = float(ft_only_match.group(1))
        return ft * 12

    # Another common form: 6' 7.25''
    ft_in_match2 = re.match(r"^(\d+)\s*'\s*(\d+(?:\.\d+)?)\s*''$", s)
    if ft_in_match2:
        ft = float(ft_in_match2.group(1))
        inch = float(ft_in_match2.group(2))
        return ft * 12 + inch

    # Simple numeric: treat as inches if plausible
    try:
        num = float(s)
        # heights/wingspans/reaches in inches are generally > 40
        if 30 <= num <= 120:
            return num
    except ValueError:
        pass
    return None


SUFFIXES = {
    "jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v",
}
PREFIXES = {"mr", "mrs", "ms", "miss", "dr", "sir", "dame"}


@dataclass
class NameParts:
    prefix: Optional[str] = None
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    last_name: Optional[str] = None
    suffix: Optional[str] = None


def _tidy_token(tok: str) -> str:
    return tok.strip().strip(",").strip()


def parse_name_full(name: str, reversed_order: bool = False) -> NameParts:
    """Parse a full name into components.

    - Keeps apostrophes in surnames intact.
    - Handles suffixes like Jr., III.
    - Handles reversed order "Last Suffix, First Middle" used in shooting data.
    """
    if not name:
        return NameParts()
    n = name.strip()
    # remove multiple spaces
    n = _SPACEY_RE.sub(" ", n)

    if reversed_order and "," in n:
        # Example: "Clayton Jr., Walter A."
        last_side, first_side = [p.strip() for p in n.split(",", 1)]
        # suffix can be embedded on the last side
        last_tokens = [_tidy_token(t) for t in last_side.split()]
        suffix_local: Optional[str] = None
        if last_tokens and last_tokens[-1].lower().rstrip(".") in SUFFIXES:
            suffix_local = last_tokens.pop(-1)
        last_name = " ".join(last_tokens) if last_tokens else None

        first_tokens = [_tidy_token(t) for t in first_side.split() if _tidy_token(t)]
        prefix_local: Optional[str] = None
        if first_tokens and first_tokens[0].lower().rstrip(".") in PREFIXES:
            prefix_local = first_tokens.pop(0)
        first_name = first_tokens[0] if first_tokens else None
        middle_name = " ".join(first_tokens[1:]) if len(first_tokens) > 1 else None

        return NameParts(prefix=prefix_local, first_name=first_name, middle_name=middle_name, last_name=last_name, suffix=suffix_local)

    # Forward order: Prefix First Middle Last Suffix
    tokens = [_tidy_token(t) for t in n.split() if _tidy_token(t)]
    prefix: Optional[str] = None
    suffix: Optional[str] = None
    if tokens and tokens[0].lower().rstrip(".") in PREFIXES:
        prefix = tokens.pop(0)
    if tokens and tokens[-1].lower().rstrip(".") in SUFFIXES:
        suffix = tokens.pop(-1)
    if not tokens:
        return NameParts(prefix=prefix, suffix=suffix)
    if len(tokens) == 1:
        return NameParts(prefix=prefix, first_name=tokens[0], last_name=None, suffix=suffix)
    # Heuristic: first token = first_name, last token = last_name, rest = middle
    first_name = tokens[0]
    last_name = tokens[-1]
    middle_name = " ".join(tokens[1:-1]) if len(tokens) > 2 else None
    return NameParts(prefix=prefix, first_name=first_name, middle_name=middle_name, last_name=last_name, suffix=suffix)


def cell_text(td: Tag) -> str:
    return td.get_text(" ", strip=True)


def normalize_value(key: str, value: Optional[object]) -> Optional[object]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "-":
        return None
    # body fat like "10.0%" => 10.0
    if key.endswith("_pct"):
        s = s.replace("%", "").strip()
        try:
            return float(s)
        except ValueError:
            return None
    # numeric floats/ints
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", s):
        if "." in s:
            try:
                return float(s)
            except ValueError:
                return s
        try:
            return int(s)
        except ValueError:
            return s
    return s


def ensure_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def save_csv(rows: List[Dict[str, object]], out_path: Path) -> None:
    ensure_output_dir(out_path.parent)
    if not rows:
        # Write empty file with no headers (still creates a file)
        out_path.write_text("")
        return
    fieldnames = list(rows[0].keys())
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ------------------------------
# HTML table parsers (web scraping)
# ------------------------------

def parse_table_grouped_headers(table: Tag) -> List[str]:
    """Parse a table with two header rows: a group row and a subheader row."""
    thead = table.find("thead")
    rows = thead.find_all("tr") if thead else []
    if len(rows) < 2:
        # Prefer field attribute when present
        hdrs = []
        for th in rows[0].find_all("th"):
            field = th.get("field")
            label = field if field else th.get_text(" ", strip=True)
            hdrs.append(to_snake(label))
        return hdrs

    group_row = rows[0]
    header_row = rows[1]

    groups: List[str] = []
    for th in group_row.find_all("th"):
        label = th.get_text(" ", strip=True)
        colspan = int(th.get("colspan", 1))
        gname = "player" if label == "" else to_snake(label)
        groups.extend([gname] * colspan)

    keys: List[str] = []
    for idx, th in enumerate(header_row.find_all("th")):
        field = th.get("field")
        if field:
            keys.append(to_snake(field))
            continue
        sub = th.get_text(" ", strip=True)
        g = groups[idx] if idx < len(groups) else ""
        if g == "player":
            keys.append("player")
        else:
            keys.append(to_snake(f"{g} {sub}"))
    return keys


def parse_shooting_html(html: str, season_year: str) -> List[Dict[str, object]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    keys = parse_table_grouped_headers(table)
    tbody = table.find("tbody")
    rows: List[Dict[str, object]] = []
    if not tbody:
        return rows
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        record: Dict[str, object] = {}
        player_cell = tds[0]
        pos_tag = player_cell.find("span")
        pos = pos_tag.get_text(strip=True) if pos_tag else None
        if pos_tag:
            pos_tag.extract()
        raw_player = player_cell.get_text(" ", strip=True)
        name_parts = parse_name_full(raw_player, reversed_order=True)
        record.update(
            {
                "prefix": name_parts.prefix,
                "first_name": name_parts.first_name,
                "middle_name": name_parts.middle_name,
                "last_name": name_parts.last_name,
                "suffix": name_parts.suffix,
                "pos": pos,
                "season": season_year,
            }
        )
        for i, td in enumerate(tds):
            if i >= len(keys):
                continue
            key = keys[i]
            if key == "player":
                continue
            val = normalize_value(key, cell_text(td))
            record[key] = val
        rows.append(record)
    return rows


def parse_simple_table_html(html: str, season_year: str) -> List[Dict[str, object]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    thead = table.find("thead")
    header_row = thead.find("tr") if thead else None
    if not header_row:
        return []
    headers = []
    for th in header_row.find_all("th"):
        field = th.get("field")
        label = field if field else th.get_text(" ", strip=True)
        headers.append(to_snake(label))
    headers = ["pos" if h in {"pos", "position"} else h for h in headers]

    tbody = table.find("tbody")
    rows: List[Dict[str, object]] = []
    if not tbody:
        return rows
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        raw = {headers[i]: cell_text(td) if i < len(headers) else None for i, td in enumerate(tds)}
        name_key = next((h for h in headers if h in {"player", "player_name"}), None)
        raw_name = raw.get(name_key) if name_key else None
        name_parts = parse_name_full(raw_name or "", reversed_order=False)

        record: Dict[str, object] = {
            "prefix": name_parts.prefix,
            "first_name": name_parts.first_name,
            "middle_name": name_parts.middle_name,
            "last_name": name_parts.last_name,
            "suffix": name_parts.suffix,
            "season": season_year,
        }
        if "pos" in raw:
            record["pos"] = raw.get("pos")
        for k, v in raw.items():
            if k in {"player", "player_name", "pos"}:
                continue
            if any(x in k for x in ["height", "wingspan", "reach"]):
                inches = parse_height_like_to_inches(v or "")
                record[k] = inches
            else:
                record[k] = normalize_value(k, v)
        rows.append(record)
    return rows


# ------------------------------
# Fetching
# ------------------------------
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def fetch_html(url: str, timeout: float = 30.0) -> str:
    with httpx.Client(timeout=timeout, headers=DEFAULT_HEADERS) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def fetch_html_headless(url: str, timeout: float = 30.0, wait_selector: str = "table.Crom_table__p1iZz, table thead th[field]") -> str:
    """Render the page via headless Chromium and return the HTML once tables are present.

    Tries to accept cookie banners and waits for table + rows. Uses a generic
    Crom table selector by default.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=DEFAULT_HEADERS.get("User-Agent"),
            viewport={"width": 1366, "height": 900},
            device_scale_factor=1.25,
            is_mobile=False,
            locale="en-US",
            timezone_id="America/New_York",
        )
        # Make timeouts consistent with CLI
        try:
            context.set_default_timeout(timeout * 1000)
            context.set_default_navigation_timeout(timeout * 1000)
        except Exception:
            pass
        page = context.new_page()
        # Stealth JS to reduce bot signals
        page.add_init_script(
            """
            // webdriver flag
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            // chrome object
            window.chrome = { runtime: {} };
            // languages
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
            // plugins length
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4] });
            // permissions query shim
            const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
            if (originalQuery) {
              window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ? Promise.resolve({ state: Notification.permission }) : originalQuery(parameters)
              );
            }
            """
        )
        page.set_extra_http_headers({
            "Accept": DEFAULT_HEADERS.get("Accept", "text/html"),
            "Accept-Language": "en-US,en;q=0.9",
            # Client hints to look more like a real browser
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not=A?Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "Upgrade-Insecure-Requests": "1",
        })
        page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        # Accept cookie banner if present (OneTrust)
        try:
            page.locator("#onetrust-accept-btn-handler").click(timeout=3000)
        except Exception:
            pass
        # wait for network to settle a bit
        try:
            page.wait_for_load_state("networkidle", timeout=timeout * 1000)
        except Exception:
            pass
        # wait for table container becoming visible
        page.wait_for_selector(wait_selector, timeout=timeout * 1000, state="visible")
        # also ensure rows exist
        try:
            page.wait_for_selector("tbody tr", timeout=timeout * 1000)
        except Exception:
            pass
        # attempt to scroll to trigger any lazy loads
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(500)
        except Exception:
            pass
        html = page.content()
        context.close()
        browser.close()
        return html


# ------------------------------
# NBA Stats API fallback
# ------------------------------

NBA_API_ROOT = "https://stats.nba.com/stats"
ANTHRO_API = NBA_API_ROOT + "/draftcombineanthro?LeagueID=00&SeasonYear={season_year}"
AGILITY_API = NBA_API_ROOT + "/draftcombinedrillresults?LeagueID=00&SeasonYear={season_year}"
SHOOTING_API = NBA_API_ROOT + "/draftcombineshooting?LeagueID=00&SeasonYear={shooting_year}"
SHOOTING_SPOT_API = NBA_API_ROOT + "/draftcombinespotshooting?LeagueID=00&SeasonYear={season_year}"

NBA_API_HEADERS = {
    "User-Agent": DEFAULT_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/stats/",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not=A?Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}


def fetch_stats_json(url: str, timeout: float = 30.0) -> dict:
    # Some environments have trouble with HTTP/2; disable to be safe.
    with httpx.Client(timeout=timeout, headers=NBA_API_HEADERS, http2=False) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.json()


def _result_rows(payload: dict) -> Tuple[List[str], List[List[object]]]:
    if "resultSets" in payload and payload["resultSets"]:
        rs = payload["resultSets"][0]
        return rs["headers"], rs["rowSet"]
    rs = payload.get("resultSet")
    if rs:
        return rs["headers"], rs["rowSet"]
    return [], []


def _rows_to_dicts(headers: List[str], rows: List[List[object]]) -> List[Dict[str, object]]:
    keys = [to_snake(h) for h in headers]
    return [{keys[i]: row[i] if i < len(row) else None for i in range(len(keys))} for row in rows]


def _add_name_parts(d: Dict[str, object], season_year: str) -> Dict[str, object]:
    raw_last_first = d.get("player_last_first") or d.get("player_lastname_firstname")
    raw_name = d.get("player_name")
    name_str = str(raw_last_first or raw_name or "")
    parts = parse_name_full(name_str, reversed_order=bool(raw_last_first))
    out: Dict[str, object] = {
        "prefix": parts.prefix,
        "first_name": parts.first_name,
        "middle_name": parts.middle_name,
        "last_name": parts.last_name,
        "suffix": parts.suffix,
        "season": season_year,
    }
    pos = d.get("position") or d.get("pos")
    if pos is not None:
        out["pos"] = pos
    out.update(d)
    return out


def season_arg_to_values(season_arg: str) -> Tuple[str, str]:
    """Map CLI `YYYY-YY` into (season_year, season_year_shooting).

    - season_year: 'YYYY-YY' (used for anthro/agility)
    - season_year_shooting: 'YYYY' END year (e.g., 2023-24 -> 2024)
    If season_arg is already just 'YYYY', use it for both (fallback).
    """
    s = season_arg.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})", s)
    if m:
        start = int(m.group(1))
        shooting_end_year = str(start + 1)
        return (s, shooting_end_year)
    m2 = re.fullmatch(r"(\d{4})", s)
    if m2:
        y = m2.group(1)
        return (y, y)
    raise ValueError("Season must be 'YYYY-YY' or 'YYYY'. e.g., '2024-25'.")


def scrape_shooting(season_arg: str, html_override: Optional[str] = None, timeout: float = 30.0, headless: bool = True) -> List[Dict[str, object]]:
    season_year, shooting_year = season_arg_to_values(season_arg)
    if html_override is not None:
        html = html_override
        return parse_shooting_html(html, season_year=season_year)
    # Try HTML first
    try:
        url = SHOOTING_URL.format(season_year_shooting=shooting_year)
        html = fetch_html_headless(url, timeout=timeout) if headless else fetch_html(url, timeout=timeout)
        rows = parse_shooting_html(html, season_year=season_year)
        if rows:
            return rows
    except Exception:
        pass
    # Try alternate spot-shooting page (older seasons)
    try:
        url = SHOOTING_SPOT_URL.format(season_year=season_year)
        html = fetch_html_headless(url, timeout=timeout) if headless else fetch_html(url, timeout=timeout)
        rows = parse_shooting_html(html, season_year=season_year)
        if rows:
            return rows
    except Exception:
        pass
    # Fallback: Stats API
    try:
        data = fetch_stats_json(SHOOTING_API.format(shooting_year=shooting_year), timeout=timeout)
        headers, rset = _result_rows(data)
        dicts = _rows_to_dicts(headers, rset)
        out = [{k: normalize_value(k, v) for k, v in d.items()} for d in dicts]
        return [_add_name_parts(d, season_year) for d in out]
    except Exception:
        pass
    # Fallback 2: Spot Shooting API for older seasons (uses YYYY-YY)
    try:
        data = fetch_stats_json(SHOOTING_SPOT_API.format(season_year=season_year), timeout=timeout)
        headers, rset = _result_rows(data)
        dicts = _rows_to_dicts(headers, rset)
        out = [{k: normalize_value(k, v) for k, v in d.items()} for d in dicts]
        return [_add_name_parts(d, season_year) for d in out]
    except Exception:
        return []


def scrape_anthro(season_arg: str, html_override: Optional[str] = None, timeout: float = 30.0, headless: bool = True) -> List[Dict[str, object]]:
    season_year, _ = season_arg_to_values(season_arg)
    if html_override is not None:
        html = html_override
        return parse_simple_table_html(html, season_year=season_year)
    try:
        url = ANTHRO_URL.format(season_year=season_year)
        html = fetch_html_headless(url, timeout=timeout) if headless else fetch_html(url, timeout=timeout)
        rows = parse_simple_table_html(html, season_year=season_year)
        if rows:
            return rows
    except Exception:
        pass
    # Fallback: API
    try:
        data = fetch_stats_json(ANTHRO_API.format(season_year=season_year), timeout=timeout)
        headers, rset = _result_rows(data)
        dicts = _rows_to_dicts(headers, rset)
        # convert heights to inches
        out: List[Dict[str, object]] = []
        for d in dicts:
            dd = {k: normalize_value(k, v) for k, v in d.items()}
            for hk in list(dd.keys()):
                if any(x in hk for x in ["height", "wingspan", "reach"]):
                    dd[hk] = parse_height_like_to_inches(str(dd[hk])) if dd.get(hk) is not None else None
            out.append(_add_name_parts(dd, season_year))
        return out
    except Exception:
        return []


def scrape_agility(season_arg: str, html_override: Optional[str] = None, timeout: float = 30.0, headless: bool = True) -> List[Dict[str, object]]:
    season_year, _ = season_arg_to_values(season_arg)
    if html_override is not None:
        html = html_override
        return parse_simple_table_html(html, season_year=season_year)
    try:
        url = AGILITY_URL.format(season_year=season_year)
        html = fetch_html_headless(url, timeout=timeout) if headless else fetch_html(url, timeout=timeout)
        rows = parse_simple_table_html(html, season_year=season_year)
        if rows:
            return rows
    except Exception:
        pass
    # Fallback: API
    try:
        data = fetch_stats_json(AGILITY_API.format(season_year=season_year), timeout=timeout)
        headers, rset = _result_rows(data)
        dicts = _rows_to_dicts(headers, rset)
        out = [{k: normalize_value(k, v) for k, v in d.items()} for d in dicts]
        return [_add_name_parts(d, season_year) for d in out]
    except Exception:
        return []


# ------------------------------
# CLI
# ------------------------------
def _all_seasons() -> List[str]:
    seasons: List[str] = []
    for start in range(2000, 2026):  # 2000-01 through 2025-26
        end_two = (start + 1) % 100
        seasons.append(f"{start}-{end_two:02d}")
    return seasons


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape NBA Draft Combine data (shooting, anthro, agility)")
    parser.add_argument(
        "--year",
        dest="season",
        type=str,
        default=None,
        help="Optional season 'YYYY-YY' (e.g., 2025-26). Default scrapes all seasons.",
    )
    parser.add_argument(
        "--source",
        choices=["all", "shooting", "anthro", "agility"],
        default="all",
        help="Which data source to scrape",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Request timeout in seconds (default 30)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print progress messages while scraping",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Disable headless rendering (use raw HTML fetch)",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default="scraper/output",
        help="Directory to write CSV files",
    )
    # Optional local HTML overrides
    parser.add_argument(
        "--from-file-shooting",
        type=str,
        default=None,
        help="Path to local HTML for shooting (uses this instead of fetching)",
    )
    parser.add_argument(
        "--from-file-anthro",
        type=str,
        default=None,
        help="Path to local HTML for anthro (uses this instead of fetching)",
    )
    parser.add_argument(
        "--from-file-agility",
        type=str,
        default=None,
        help="Path to local HTML for agility (uses this instead of fetching)",
    )

    args = parser.parse_args()
    out_dir = Path(args.out_dir)

    def read_override(path_str: Optional[str]) -> Optional[str]:
        if not path_str:
            return None
        raw = Path(path_str).read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"<table[\s\S]*?</table>", raw, flags=re.IGNORECASE)
        return m.group(0) if m else raw

    # Determine seasons to scrape
    seasons = [args.season] if args.season else _all_seasons()

    for season in seasons:
        if args.verbose:
            print(f"[info] scraping season {season} source={args.source}")
        if args.source in {"all", "shooting"}:
            try:
                if args.verbose:
                    print("[info] shooting: fetching")
                rows = scrape_shooting(
                    season,
                    html_override=read_override(args.from_file_shooting),
                    timeout=args.timeout,
                    headless=(not args.no_headless),
                )
                save_csv(rows, out_dir / f"{season}_shooting.csv")
                if args.verbose or not rows:
                    print(f"[info] shooting: saved {len(rows)} rows -> {out_dir / f'{season}_shooting.csv'}")
            except Exception as e:
                print(f"[warn] shooting {season}: {e}")

        if args.source in {"all", "anthro"}:
            try:
                if args.verbose:
                    print("[info] anthro: fetching")
                rows = scrape_anthro(
                    season,
                    html_override=read_override(args.from_file_anthro),
                    timeout=args.timeout,
                    headless=(not args.no_headless),
                )
                save_csv(rows, out_dir / f"{season}_anthro.csv")
                if args.verbose or not rows:
                    print(f"[info] anthro: saved {len(rows)} rows -> {out_dir / f'{season}_anthro.csv'}")
            except Exception as e:
                print(f"[warn] anthro {season}: {e}")

        if args.source in {"all", "agility"}:
            try:
                if args.verbose:
                    print("[info] agility: fetching")
                rows = scrape_agility(
                    season,
                    html_override=read_override(args.from_file_agility),
                    timeout=args.timeout,
                    headless=(not args.no_headless),
                )
                save_csv(rows, out_dir / f"{season}_agility.csv")
                if args.verbose or not rows:
                    print(f"[info] agility: saved {len(rows)} rows -> {out_dir / f'{season}_agility.csv'}")
            except Exception as e:
                print(f"[warn] agility {season}: {e}")


if __name__ == "__main__":
    main()
