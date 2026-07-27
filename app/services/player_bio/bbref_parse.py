"""Parse Basketball-Reference index and player pages into bio dataclasses.

Pure parsing: every function here takes HTML that someone else fetched (see
:mod:`app.services.player_bio.bbref_scrape`) and returns dataclasses, so the
parsers are exercisable against checked-in fixture pages with no network.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup


@dataclass
class IndexRow:
    letter: str
    slug: str
    name: str
    pos: Optional[str]
    year_min: Optional[int]
    year_max: Optional[int]
    height_in: Optional[int]
    weight_lb: Optional[int]
    birth_date: Optional[str]
    colleges: Optional[str]
    active_flag: bool


@dataclass
class PlayerBio:
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


def parse_index_html(letter: str, html: str) -> List[IndexRow]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[IndexRow] = []
    # Rows generally use th[data-append-csv] for slug
    for tr in soup.select("tr"):
        th = tr.find("th", attrs={"data-append-csv": True})
        if not th:
            continue
        slug = th.get("data-append-csv") or ""
        if not slug:
            # Fallback to href
            a = th.find("a")
            if not a or not a.get("href"):
                continue
            href = a["href"]
            # /players/b/bassech01.html -> bassech01
            m = re.search(r"/players/[a-z]/([a-z0-9]+)\.html", href)
            if not m:
                continue
            slug = m.group(1)
        name_tag = th.find("a")
        name = (
            name_tag.get_text(strip=True) if name_tag else th.get_text(" ", strip=True)
        )
        active_flag = bool(th.find("strong"))

        def td_data(stat: str) -> Optional[str]:
            td = tr.find("td", attrs={"data-stat": stat})
            if not td:
                return None
            return td.get_text(" ", strip=True)

        def td_num(stat: str) -> Optional[int]:
            td = tr.find("td", attrs={"data-stat": stat})
            if not td:
                return None
            # sometimes height is in csk with inches
            csk = td.get("csk")
            if csk and re.fullmatch(r"\d+(?:\.\d+)?", csk):
                try:
                    return int(round(float(csk)))
                except Exception:
                    pass
            txt = td.get_text(" ", strip=True)
            if not txt:
                return None
            # heights might be like 6-10
            if stat == "height" and re.match(r"^\d+-\d+$", txt):
                ft, inc = [int(x) for x in txt.split("-")]
                return ft * 12 + inc
            if txt.isdigit():
                return int(txt)
            try:
                return int(round(float(txt)))
            except Exception:
                return None

        pos = td_data("pos")
        year_min = td_num("year_min")
        year_max = td_num("year_max")
        height_in = td_num("height")
        weight_lb = td_num("weight")

        birth_csk = tr.find("td", attrs={"data-stat": "birth_date"})
        birth_date: Optional[str] = None
        if birth_csk and birth_csk.get("csk"):
            csk = birth_csk.get("csk")
            if csk and re.fullmatch(r"\d{8}", csk):
                birth_date = f"{csk[0:4]}-{csk[4:6]}-{csk[6:8]}"
        colleges_td = tr.find("td", attrs={"data-stat": "colleges"})
        colleges = colleges_td.get_text(" ", strip=True) if colleges_td else None

        out.append(
            IndexRow(
                letter=letter,
                slug=slug,
                name=name,
                pos=pos,
                year_min=year_min,
                year_max=year_max,
                height_in=height_in,
                weight_lb=weight_lb,
                birth_date=birth_date,
                colleges=colleges,
                active_flag=active_flag,
            )
        )
    return out


def _text_after_strong(p, label: str) -> Optional[str]:
    if not p:
        return None
    target = label.lower().rstrip(":").strip()
    for strong in p.find_all("strong"):
        text = strong.get_text(" ", strip=True).lower().rstrip(":").strip()
        if text != target:
            continue
        chunks: List[str] = []
        for node in strong.next_siblings:
            node_name = getattr(node, "name", None)
            if node_name in {"strong", "br"}:
                break
            raw = (
                node.get_text(" ", strip=True)
                if hasattr(node, "get_text")
                else str(node)
            )
            raw = raw.replace("\xa0", " ")
            if not raw:
                continue
            if "▪" in raw:
                raw = raw.split("▪", 1)[0]
                if raw.strip():
                    chunks.append(raw.strip())
                break
            chunks.append(raw.strip())
        combined = " ".join(chunk for chunk in chunks if chunk)
        combined = re.sub(r"\s+", " ", combined).strip(" ,\n\t")
        if combined:
            return combined
    return None


def _parse_height_weight(meta_div) -> Tuple[Optional[int], Optional[int]]:
    # Look for a p that contains a pattern like "6-6, 190lb"
    for p in meta_div.find_all("p"):
        txt = p.get_text(" ", strip=True)
        m = re.search(r"(\d+)-(\d+).*?(\d+)\s*lb", txt, flags=re.IGNORECASE)
        if m:
            ft = int(m.group(1))
            inc = int(m.group(2))
            lbs = int(m.group(3))
            return ft * 12 + inc, lbs
    return None, None


def _parse_birth(
    meta_div,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    # Returns (birth_date, city, state_province, country)
    p = meta_div.find("p", string=re.compile(r"Born:\s*", flags=re.IGNORECASE))
    if not p:
        # Try by id
        necro = meta_div.find(id="necro-birth")
        if not necro:
            return None, None, None, None
        birth_date = necro.get("data-birth")
        # Look up the parent p
        p = necro.find_parent("p")
    else:
        necro = p.find(id="necro-birth")
        birth_date = necro.get("data-birth") if necro else None
    # parse location in same p: contains "in City, State" and a country flag span e.g. f-i f-us
    city = state = country = None
    if p:
        # Try to derive country from flag span classes
        flag = p.find("span", class_=re.compile(r"\bf-i\b"))
        if flag:
            classes = flag.get("class", [])
            for c in classes:
                if c.startswith("f-") and len(c) == 4:
                    country = c[2:].upper()
                    break
        txt = p.get_text(" ", strip=True)
        # after 'in ' capture up to next comma; state may include dangling country code words
        m = re.search(r"\bin\s+([^,]+)\s*,\s*([^,]+)", txt)
        if m:
            city = m.group(1).strip()
            state = (m.group(2) or "").strip()
            # clean a trailing country code token accidentally stuck to state
            state = re.sub(
                r"\s+(us|usa|canada|mexico)$", "", state, flags=re.IGNORECASE
            )
    return birth_date, city, state, country


def _parse_draft(
    meta_div,
) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[str]]:
    # Draft: Team, 1st round (2nd pick, 2nd overall), 2017 NBA Draft
    p = None
    for cand in meta_div.find_all("p"):
        s = cand.find("strong")
        if s and s.get_text(strip=True).startswith("Draft"):
            p = cand
            break
    if not p:
        return None, None, None, None
    txt = p.get_text(" ", strip=True)
    # team (before first comma)
    team = None
    m_team = re.match(r"Draft:\s*([^,]+)", txt)
    if m_team:
        team = m_team.group(1).strip()
    # year from draft link or trailing year
    year = None
    ylink = p.find("a", href=re.compile(r"/draft/NBA_\d+\.html"))
    if ylink:
        m = re.search(r"NBA_(\d{4})", ylink["href"])  # type: ignore[index]
        if m:
            year = int(m.group(1))
    else:
        m = re.search(r"(19|20)\d{2}", txt)
        if m:
            year = int(m.group(0))
    # round and pick
    rnd = None
    pk = None
    m_r = re.search(r"(\d+)(?:st|nd|rd|th)\s+round", txt)
    if m_r:
        rnd = int(m_r.group(1))
    m_p = re.search(r"\((\d+)(?:st|nd|rd|th)?\s+pick", txt)
    if m_p:
        pk = int(m_p.group(1))
    return year, rnd, pk, team


def parse_player_html(letter: str, slug: str, html: str, source_url: str) -> PlayerBio:
    soup = BeautifulSoup(html, "html.parser")
    meta_div = soup.find("div", id="meta")
    full_name = soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else slug

    shoots = None
    school = None
    high_school = None
    current_team = None
    position = None
    nba_debut_date = None
    social_x_handle = None
    social_x_url = None
    social_instagram_handle = None
    social_instagram_url = None

    if meta_div:
        # Shoots
        for p in meta_div.find_all("p"):
            s = _text_after_strong(p, "Shoots")
            if s:
                shoots = s.split()[0]
            s2 = _text_after_strong(p, "College")
            if s2:
                # collect anchors or plain text
                anchors = [a.get_text(strip=True) for a in p.find_all("a")]
                school = ", ".join(anchors) if anchors else s2
            s3 = _text_after_strong(p, "High School") or _text_after_strong(
                p, "High Schools"
            )
            if s3:
                high_school = s3
            s4 = _text_after_strong(p, "Team")
            if s4:
                current_team = s4
            s5 = _text_after_strong(p, "Position")
            if s5:
                position = s5
            s6 = _text_after_strong(p, "NBA Debut")
            if s6:
                # extract a date like October 19, 2017
                m = re.search(
                    r"([A-Za-z]+\s+\d{1,2},\s+\d{4})", p.get_text(" ", strip=True)
                )
                if m:
                    # Keep ISO format
                    try:
                        nba_debut_date = (
                            datetime.strptime(m.group(1), "%B %d, %Y")
                            .date()
                            .isoformat()
                        )
                    except Exception:
                        pass
            # Socials
            for a in p.find_all("a"):
                href = a.get("href") or ""
                if "instagram.com" in href:
                    social_instagram_url = href
                    handle = a.get_text(strip=True) or href.rstrip("/").split("/")[-1]
                    social_instagram_handle = handle.lstrip("@").lower()
                if "twitter.com" in href or "x.com" in href:
                    social_x_url = href
                    handle = a.get_text(strip=True) or href.rstrip("/").split("/")[-1]
                    social_x_handle = handle.lstrip("@").lower()

    height_in, weight_lb = (None, None)
    birth_date, birth_city, birth_state, birth_country = (None, None, None, None)
    dy: Optional[int] = None
    dr: Optional[int] = None
    dp: Optional[int] = None
    dt: Optional[str] = None
    if meta_div:
        height_in, weight_lb = _parse_height_weight(meta_div)
        birth_date, birth_city, birth_state, birth_country = _parse_birth(meta_div)
        # Draft fields
        dy, dr, dp, dt = _parse_draft(meta_div)

    # derive seasons
    nba_debut_season = None
    if nba_debut_date:
        try:
            d = datetime.strptime(nba_debut_date, "%Y-%m-%d").date()
            end_year = d.year if d.month >= 7 else d.year - 1
            nba_debut_season = f"{end_year}-{(end_year + 1) % 100:02d}"
        except Exception:
            nba_debut_season = None

    scraped_at = datetime.now(timezone.utc).isoformat()
    return PlayerBio(
        slug=slug,
        url=source_url,
        full_name=full_name,
        birth_date=birth_date,
        birth_city=birth_city,
        birth_state_province=birth_state,
        birth_country=birth_country,
        shoots=shoots,
        school=school,
        high_school=high_school,
        draft_year=dy,
        draft_round=dr,
        draft_pick=dp,
        draft_team=dt,
        nba_debut_date=nba_debut_date,
        nba_debut_season=nba_debut_season,
        is_active_nba=None,
        current_team=current_team,
        nba_last_season=None,
        position=position,
        height_in=height_in,
        weight_lb=weight_lb,
        social_x_handle=social_x_handle,
        social_x_url=social_x_url,
        social_instagram_handle=social_instagram_handle,
        social_instagram_url=social_instagram_url,
        source_url=source_url,
        scraped_at=scraped_at,
    )


def derive_season_from_index(row: IndexRow) -> Tuple[Optional[bool], Optional[str]]:
    # is_active_nba, nba_last_season
    is_active = True if row.active_flag else None
    last_season = None
    if row.year_max:
        end = row.year_max
        last_season = f"{end - 1}-{(end) % 100:02d}"
    return is_active, last_season


def parse_slug_from_player_html(html: str) -> Optional[Tuple[str, str]]:
    # Try canonical link
    m = re.search(
        r"href=\"https?://www\.basketball-reference\.com/players/([a-z])/([a-z0-9]+)\.html\"",
        html,
    )
    if m:
        return m.group(1), m.group(2)
    # Fallback: any player URL in page
    m = re.search(r"/players/([a-z])/([a-z0-9]+)\.html", html)
    if m:
        return m.group(1), m.group(2)
    return None
