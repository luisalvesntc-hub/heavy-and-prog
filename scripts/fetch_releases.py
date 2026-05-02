"""Fetch this week's metal/prog album releases from Metal Archives + Wikipedia.

No third-party APIs; just polite HTTP scraping. Outputs data/releases.json.
"""

import argparse
import json
import math
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlencode, urljoin

from curl_cffi import requests
from bs4 import BeautifulSoup

MAX_TOTAL = 30  # 12 in the digest, 18 in the lazy-loaded tail

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
MA_AJAX_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.metal-archives.com/release/upcoming",
}

GENRE_KEYWORDS = (
    "metal", "prog", "djent", "doom", "sludge", "stoner",
    "post-rock", "post-metal", "math rock", "mathcore",
    "krautrock", "zeuhl", "canterbury", "avant-garde",
    "hardcore", "post-hardcore",
    "fusion", "jazz rock", "jazz-rock", "instrumental rock",
    "instrumental metal", "shred", "neoclassical", "art rock",
    "space rock", "psychedelic rock", "symphonic rock",
)


MA_ADVANCED = "https://www.metal-archives.com/search/ajax-advanced/searching/albums"
WIKI_LIST = "https://en.wikipedia.org/wiki/List_of_{year}_albums"

MB_BASE = "https://musicbrainz.org/ws/2"
MB_UA = "MetalProgWeekly/1.0 (https://luisalvesntc-hub.github.io/heavy-and-prog/)"

DDG_HTML = "https://html.duckduckgo.com/html/"
PROGREPORT_FEED = "https://progreport.com/feed/"
PROGREPORT_REVIEWS = "https://progreport.com/category/progressive-rock-reviews/"
PROGREPORT_NEWS = "https://progreport.com/category/latest-progressive-rock-news/"

PROGSUBWAY_FEED = "https://theprogressivesubway.com/feed/"
PROGSUBWAY_HOME = "https://theprogressivesubway.com/"

# Domains we recognize as music journalism / coverage (boost relevance ranking).
JOURNALISM_DOMAINS = {
    "loudwire.com", "metalsucks.net", "metalinjection.net", "angrymetalguy.com",
    "blabbermouth.net", "kerrang.com", "metalhammer.com", "louder", "decibelmagazine.com",
    "noisey.vice.com", "pitchfork.com", "stereogum.com", "consequence.net",
    "sputnikmusic.com", "metalstorm.net", "invisibleoranges.com", "noecho.net",
    "ghostcultmag.com", "progmagazine.com", "progreport.com", "theprogressivesubway.com",
    "seaoftranquility.org",
    "metalcoffeezine.com", "themetalwanderlust.com", "progressivemusicplanet.com",
    "metalblade.com", "nuclearblast.com", "newnoisemagazine.com", "exclaim.ca",
    "thequietus.com", "spin.com", "rollingstone.com", "treblezine.com",
    "albumoftheyear.org", "rateyourmusic.com",
}
MB_TAGS = [
    "progressive rock", "progressive metal", "prog rock", "prog metal",
    "art rock", "symphonic rock", "neo-prog", "krautrock", "canterbury scene",
    "jazz fusion", "jazz rock", "math rock", "instrumental rock",
    "instrumental metal", "post-rock", "post-metal", "psychedelic rock",
    "djent", "shred", "neoclassical metal", "space rock", "zeuhl",
]

MONTH_NUM = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# curl_cffi's Session impersonates a real browser's TLS+HTTP fingerprint so we
# get past Cloudflare's bot-mitigation challenge that blocks plain `requests`.
session = requests.Session(impersonate="chrome")
session.headers.update(HEADERS)

# Optional: route Metal Archives through ScrapingAnt so MA's Cloudflare sees a
# residential IP instead of GitHub Actions' (which Cloudflare blocks with 403).
# When SCRAPINGANT_KEY is unset (e.g. local dev) we egress directly.
SCRAPINGANT_KEY = os.environ.get("SCRAPINGANT_KEY", "").strip()
SCRAPINGANT_ENDPOINT = "https://api.scrapingant.com/v2/general"
PROXIED_HOSTS = ("metal-archives.com",)


# ---------- date helpers ----------

def previous_friday(today: date) -> date:
    delta = (today.weekday() - 4) % 7
    return today - timedelta(days=delta)


def parse_ma_date(s: str) -> date | None:
    # Metal Archives uses "April 17th, 2026" / "May 1st, 2026" etc.
    s = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", s).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_wiki_date(month_year: str, day_str: str) -> date | None:
    s = f"{month_year} {day_str}".strip()
    for fmt in ("%B %Y %d", "%B %d %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


# ---------- HTTP with retries ----------

def _should_proxy(url: str) -> bool:
    return bool(SCRAPINGANT_KEY) and any(h in url for h in PROXIED_HOSTS)


def _build_target_url(url: str, params) -> str:
    if not params:
        return url
    items = list(params.items()) if hasattr(params, "items") else list(params)
    qs = urlencode(items)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{qs}"


def fetch(url: str, *, params=None, sleep: float = 0.5,
          extra_headers: dict | None = None):
    if _should_proxy(url):
        # ScrapingAnt's general endpoint takes a single ?url= param. We pre-encode
        # the target URL with all its params, then forward MA-specific request
        # headers via the Ant-* prefix so MA still sees the AJAX shape.
        target = _build_target_url(url, params)
        # browser=true is required so ScrapingAnt's headless Chromium solves
        # MA's Cloudflare JS challenge. proxy_type=residential is also required
        # — datacenter IPs (even ScrapingAnt's) are blanket-blocked by MA's
        # Cloudflare regardless of browser. ~25 credits/request; we cap volume
        # via MAX_TOTAL + skipping the discography/tracklist enrichment to
        # stay inside the 10k/month free tier.
        ant_params = [
            ("url", target),
            ("x-api-key", SCRAPINGANT_KEY),
            ("proxy_type", "residential"),
            ("browser", "true"),
        ]
        ant_headers = {f"Ant-{k}": v for k, v in (extra_headers or {}).items()}
        request_url = SCRAPINGANT_ENDPOINT
        request_params = ant_params
        request_headers = ant_headers
        request_timeout = 90
    else:
        request_url = url
        request_params = params
        request_headers = extra_headers
        request_timeout = 25

    for attempt in range(4):
        try:
            r = session.get(request_url, params=request_params,
                            headers=request_headers, timeout=request_timeout)
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception:
            if attempt == 3:
                raise
            time.sleep(1.5 ** attempt)
    raise RuntimeError("unreachable")


# ---------- Metal Archives ----------

ISO_DATE_RE = re.compile(r"<!--\s*(\d{4})-(\d{2})-(\d{2})\s*-->")
WANTED_TYPES = {"full-length", "ep", "split", "live album", "compilation"}


def parse_ma_ajax_response(r) -> dict | None:
    """Parse the MA advanced-search AJAX response.

    When proxied through ScrapingAnt with browser=true, Chromium renders the
    JSON response inside `<html><body><pre>{...}</pre></body></html>`. Strip
    that wrapper if present, then parse as JSON.
    """
    text = r.text or ""
    try:
        return r.json()
    except ValueError:
        pass
    # Try to find a <pre> with JSON inside (browser-rendered case).
    if "<pre" in text.lower():
        try:
            soup = BeautifulSoup(text, "lxml")
            pre = soup.find("pre")
            if pre:
                inner = pre.get_text()
                return json.loads(inner)
        except Exception:
            pass
    # Last resort: find first { ... } slice.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None


def ma_warmup() -> None:
    """Visit the MA upcoming page so Cloudflare hands us a clearance cookie before
    we hit the AJAX endpoint. Without this, runs from CI hosts (GitHub Actions IPs)
    routinely 403 on the AJAX endpoint even though curl_cffi impersonates Chrome.
    """
    try:
        fetch("https://www.metal-archives.com/release/upcoming", sleep=0.3)
    except Exception as e:
        print(f"[ma] warm-up failed (continuing anyway): {e}", file=sys.stderr)


def fetch_metal_archives(window_start: date, window_end: date) -> list[dict]:
    """Use MA advanced-search ajax: filter by year/month range, parse ISO date from HTML comment.

    All bands on MA are metal-related, so we don't filter by genre here — Wikipedia is where
    we apply genre keyword filtering for prog rock, etc.
    """
    ma_warmup()

    out: list[dict] = []
    seen: set[str] = set()

    months = months_spanned(window_start, window_end)
    for year, month in months:
        offset = 0
        while offset < 5000:
            params = [
                ("bandName", ""), ("releaseTitle", ""),
                ("releaseYearFrom", str(year)), ("releaseMonthFrom", str(month)),
                ("releaseYearTo", str(year)), ("releaseMonthTo", str(month)),
                ("releaseType[]", "1"), ("releaseType[]", "2"),
                ("releaseType[]", "3"), ("releaseType[]", "5"), ("releaseType[]", "7"),
                ("sEcho", "1"), ("iColumns", "4"),
                ("iDisplayStart", str(offset)), ("iDisplayLength", "200"),
                ("iSortCol_0", "3"), ("sSortDir_0", "asc"), ("iSortingCols", "1"),
            ]
            r = fetch(MA_ADVANCED, params=params, sleep=0.7, extra_headers=MA_AJAX_HEADERS)
            data = parse_ma_ajax_response(r)
            if data is None:
                print(f"[MA] non-JSON at offset {offset}", file=sys.stderr)
                break

            rows = data.get("aaData", [])
            if not rows:
                break

            for row in rows:
                # Cells: 0=band <a>, 1=album <a>, 2=type, 3=date HTML with ISO comment
                if len(row) < 4:
                    continue
                band_html, album_html, rtype, date_html = row[0], row[1], row[2], row[3]
                m = ISO_DATE_RE.search(date_html)
                if not m:
                    continue
                y, mo, d = m.group(1), m.group(2), m.group(3)
                if d == "00":
                    continue  # month-only precision — skip, we want a real day
                try:
                    rdate = date(int(y), int(mo), int(d))
                except ValueError:
                    continue
                if rdate < window_start or rdate > window_end:
                    continue
                if rtype.strip().lower() not in WANTED_TYPES:
                    continue
                band_a = BeautifulSoup(band_html, "lxml").find("a")
                album_a = BeautifulSoup(album_html, "lxml").find("a")
                if not band_a or not album_a:
                    continue
                album_url = album_a.get("href", "")
                if album_url in seen:
                    continue
                seen.add(album_url)
                out.append({
                    "source": "metal-archives",
                    "artist": band_a.get_text(strip=True),
                    "album": album_a.get_text(strip=True),
                    "release_date": rdate.isoformat(),
                    "album_type": rtype.strip(),
                    "genres": ["Metal"],
                    "source_url": album_url,
                    "band_url": band_a.get("href", ""),
                })

            total = data.get("iTotalRecords", 0)
            offset += 200
            if offset >= total:
                break

    print(f"[MA] {len(out)} releases in window", file=sys.stderr)
    return out


def months_spanned(start: date, end: date) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


REVIEW_CELL_RE = re.compile(r">\s*(\d+)\s*\((\d+)%\)\s*<")


def fetch_ma_band_info(band_url: str) -> dict:
    """Fetch genres from band page. (Discography review-stats fetch skipped
    when running through ScrapingAnt to keep credit usage down — review_count
    + avg_rating remain 0 and the score function falls back to Wikipedia +
    band-age components.)"""
    out = {"genres": [], "avg_rating": 0.0, "review_count": 0}
    if not band_url:
        return out

    try:
        r = fetch(band_url, sleep=0.3)
    except Exception:
        return out
    soup = BeautifulSoup(r.text, "lxml")
    for dt in soup.find_all("dt"):
        if dt.get_text(strip=True).lower().startswith("genre"):
            dd = dt.find_next_sibling("dd")
            if dd:
                out["genres"] = [g.strip() for g in re.split(r"[;,/]", dd.get_text(" ", strip=True)) if g.strip()]
            break

    if SCRAPINGANT_KEY:
        return out  # skip discography page through the proxy

    bid = parse_ma_band_id(band_url)
    if bid is None:
        return out
    disco_url = f"https://www.metal-archives.com/band/discography/id/{bid}/tab/all"
    try:
        r = fetch(disco_url, sleep=0.3)
    except Exception:
        return out

    # Each reviewed album row contributes "N (XX%)". Aggregate: total reviews,
    # weighted average pct.
    total_reviews = 0
    weighted_pct = 0.0
    for n_str, pct_str in REVIEW_CELL_RE.findall(r.text):
        n = int(n_str)
        pct = int(pct_str)
        total_reviews += n
        weighted_pct += n * pct
    if total_reviews > 0:
        out["review_count"] = total_reviews
        out["avg_rating"] = weighted_pct / total_reviews
    return out


def parse_ma_band_id(band_url: str) -> int | None:
    m = re.search(r"/(\d+)/?$", band_url or "")
    return int(m.group(1)) if m else None


def score_release(release: dict, ma_info: dict | None) -> tuple[float, dict]:
    """Higher = more notable / acclaimed.

    Three components:
    - MA crowd score: avg_rating × log10(review_count+1). Captures both quality and reach.
    - Wikipedia bonus: a band/album with its own Wikipedia article cleared a notability bar.
    - MA band age: lower band IDs are older established bands; small tiebreaker.
    """
    components = {"ma_score": 0.0, "wiki": 0.0, "age": 0.0}

    if ma_info:
        avg = ma_info.get("avg_rating", 0.0)
        n = ma_info.get("review_count", 0)
        components["ma_score"] = round(avg * math.log10(n + 1), 2)

    if "wikipedia" in release.get("sources", []):
        components["wiki"] = 60.0

    bid = parse_ma_band_id(release.get("band_url", ""))
    if bid is not None:
        if bid < 50000:
            components["age"] = 25.0
        elif bid < 500000:
            components["age"] = 12.0
        elif bid < 3000000:
            components["age"] = 4.0

    total = sum(components.values())
    return total, components


def fetch_ma_tracklist(album_url: str) -> list[dict]:
    """Parse the tracklist table from a Metal Archives album page.

    MA renders tracks in a <table class="table_lyrics"> with three+ columns:
    track number, title, duration, and lyrics buttons.
    """
    if not album_url or not album_url.startswith("https://www.metal-archives.com/"):
        return []
    try:
        r = fetch(album_url, sleep=0.4)
    except Exception:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("table", class_="table_lyrics") or soup.find("table", class_=re.compile(r"display"))
    if not table:
        return []
    out: list[dict] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        first = cells[0].get_text(" ", strip=True)
        # Skip rows that are headers, side dividers, or "Total time" footers.
        if not re.match(r"^\d", first or "") and not first.endswith("."):
            continue
        title = cells[1].get_text(" ", strip=True)
        duration = cells[2].get_text(" ", strip=True)
        if not title:
            continue
        out.append({
            "title": title,
            "duration": duration if re.match(r"^\d+:\d{2}$", duration or "") else None,
        })
    return out


def fetch_ma_cover(album_url: str) -> str | None:
    try:
        r = fetch(album_url, sleep=0.5)
    except Exception as e:
        print(f"[MA] cover fetch failed: {e}", file=sys.stderr)
        return None
    soup = BeautifulSoup(r.text, "lxml")
    # The cover on MA album pages is typically inside <a id="cover"> wrapping an <img>.
    a = soup.find("a", id="cover")
    if a and a.find("img"):
        return a.find("img").get("src")
    img = soup.find("img", id="cover")
    if img:
        return img.get("src")
    return None


# ---------- Wikipedia ----------

MONTH_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)\b",
    re.I,
)
DAY_RE = re.compile(r"\b(\d{1,2})\b")


def fetch_wikipedia(window_start: date, window_end: date) -> list[dict]:
    year = window_end.year
    url = WIKI_LIST.format(year=year)
    try:
        r = fetch(url, sleep=0.3)
    except Exception as e:
        print(f"[wiki] fetch failed: {e}", file=sys.stderr)
        return []
    soup = BeautifulSoup(r.text, "lxml")

    raw: list[dict] = []  # all rows in window, before genre filter
    seen: set[tuple[str, str]] = set()
    content = soup.find("div", class_="mw-parser-output") or soup
    current_month: str | None = None

    for el in content.find_all(["h2", "h3", "h4", "table"]):
        if el.name in ("h2", "h3", "h4"):
            heading = el.get_text(" ", strip=True).replace("[edit]", "").strip()
            m = MONTH_RE.match(heading)
            if m:
                current_month = m.group(1).capitalize()
            continue
        if el.name == "table" and "wikitable" in (el.get("class") or []):
            if not current_month:
                continue
            for row in parse_wiki_table(el, current_month, year, window_start, window_end):
                key = (row["artist"].lower(), row["album"].lower())
                if key in seen:
                    continue
                seen.add(key)
                raw.append(row)

    # Inline genres on the year-list page are sparse/missing. Enrich each row by
    # fetching the album's own Wiki page for real infobox genres + cover, then
    # filter against our keyword set (covers prog, metal, fusion, instrumental, etc.)
    out: list[dict] = []
    enriched_count = 0
    for row in raw:
        if matches_genre(row.get("genres", [])):
            out.append(row)
            continue
        url_a = row.get("source_url", "")
        if not url_a.startswith("https://en.wikipedia.org/wiki/"):
            continue
        album = parse_wiki_album_page(url_a, row["album"])
        enriched_count += 1
        if not album:
            continue
        if not matches_genre(album.get("genres", [])):
            continue
        row["genres"] = sorted(set((row.get("genres") or []) + album.get("genres", [])))
        if album.get("cover"):
            row["cover"] = album["cover"]
        if album.get("artist") and not row.get("artist"):
            row["artist"] = album["artist"]
        out.append(row)

    print(f"[wiki] {len(out)} releases match keywords ({enriched_count} album pages fetched for enrichment)", file=sys.stderr)
    return out


def parse_wiki_table(table, month_name: str, year: int, window_start: date, window_end: date) -> list[dict]:
    out: list[dict] = []
    current_day: int | None = None
    month_num = MONTH_NUM.get(month_name)
    if not month_num:
        return out
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells or all(c.name == "th" for c in cells):
            continue

        # Wikipedia uses rowspan on the date column; short rows inherit the previous date.
        if len(cells) >= 6:
            day_cell, artist_cell, album_cell, genre_cell = cells[0], cells[1], cells[2], cells[3]
            day_match = DAY_RE.search(day_cell.get_text(" ", strip=True))
            if day_match:
                current_day = int(day_match.group(1))
        elif len(cells) >= 5:
            artist_cell, album_cell, genre_cell = cells[0], cells[1], cells[2]
        else:
            continue

        if current_day is None:
            continue

        try:
            rdate = date(year, month_num, current_day)
        except ValueError:
            continue
        if rdate < window_start or rdate > window_end:
            continue

        artist_a = artist_cell.find("a")
        album_a = album_cell.find("a")
        artist = artist_a.get_text(strip=True) if artist_a else artist_cell.get_text(" ", strip=True)
        album = album_a.get_text(strip=True) if album_a else album_cell.get_text(" ", strip=True)
        if not artist or not album:
            continue

        genres_text = genre_cell.get_text(" ", strip=True)
        genres = [g.strip() for g in re.split(r"[,/]", genres_text) if g.strip()]

        album_href = album_a.get("href", "") if album_a else ""
        album_url = urljoin("https://en.wikipedia.org", album_href) if album_href else ""

        out.append({
            "source": "wikipedia",
            "artist": artist,
            "album": album,
            "release_date": rdate.isoformat(),
            "album_type": "album",
            "genres": genres,
            "source_url": album_url,
            "band_url": urljoin("https://en.wikipedia.org", artist_a.get("href", "")) if artist_a else "",
        })
    return out


WIKI_DATE_PATTERNS = [
    "%B %d, %Y",   # April 17, 2026
    "%d %B %Y",    # 17 April 2026
    "%Y-%m-%d",
]


def parse_wiki_release_date(text: str) -> date | None:
    if not text:
        return None
    s = re.sub(r"<[^>]+>", " ", text)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    # Try first ~30 chars stripped phrase against several formats.
    for fmt in WIKI_DATE_PATTERNS:
        try:
            return datetime.strptime(s.split(";")[0].strip()[:30].strip(), fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"\b([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})\b", s)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}, {m.group(3)}", "%B %d, %Y").date()
        except ValueError:
            pass
    return None


def parse_wiki_album_page(url: str, page_label: str) -> dict | None:
    try:
        r = fetch(url, sleep=0.25)
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "lxml")
    infobox = soup.find("table", class_=re.compile(r"\binfobox\b"))
    if not infobox:
        return None

    fields: dict[str, str] = {}
    artist = None
    artist_url = ""
    for tr in infobox.find_all("tr"):
        th = tr.find("th")
        td = tr.find("td")
        if th and not td:
            text = th.get_text(" ", strip=True).lower()
            if "album by" in text or "ep by" in text or "mixtape by" in text:
                a = th.find("a")
                if a:
                    artist = a.get_text(strip=True)
                    href = a.get("href", "")
                    if href.startswith("/wiki/"):
                        artist_url = "https://en.wikipedia.org" + href
            continue
        if not th or not td:
            continue
        key = th.get_text(" ", strip=True).lower()
        val = td.get_text(" ", strip=True)
        if "released" in key:
            fields["released"] = val
        elif "genre" in key:
            fields["genres"] = val
        elif "artist" in key and not artist:
            a = td.find("a")
            if a:
                artist = a.get_text(strip=True)
                href = a.get("href", "")
                if href.startswith("/wiki/"):
                    artist_url = "https://en.wikipedia.org" + href

    rdate = parse_wiki_release_date(fields.get("released", ""))
    if not rdate:
        return None

    # Album title: page label minus "(album)" disambiguation, or H1 text.
    album_title = re.sub(r"\s*\([^)]*\)\s*$", "", page_label).strip()
    if not album_title:
        h1 = soup.find("h1")
        if h1:
            album_title = re.sub(r"\s*\([^)]*\)\s*$", "", h1.get_text(strip=True)).strip()
    if not album_title:
        return None

    cover = None
    cover_box = infobox.find(class_=re.compile(r"infobox-image"))
    img = (cover_box or infobox).find("img") if (cover_box or infobox) else None
    if img:
        src = img.get("src", "")
        if src.startswith("//"):
            src = "https:" + src
        if src:
            cover = src

    genres = [g.strip() for g in re.split(r"[,;/]", fields.get("genres", "")) if g.strip()]

    return {
        "source": "wikipedia",
        "artist": artist or "",
        "album": album_title,
        "release_date": rdate.isoformat(),
        "album_type": "album",
        "genres": genres,
        "source_url": url,
        "band_url": artist_url,
        "cover": cover,
    }


JOURNALISM_INDEXES = [
    ("Loudwire",          "https://loudwire.com/category/news/"),
    ("Metal Injection",   "https://metalinjection.net/news"),
    ("MetalSucks",        "https://www.metalsucks.net/category/metal-news/"),
    ("Angry Metal Guy",   "https://www.angrymetalguy.com/category/reviews/"),
    ("Blabbermouth",      "https://blabbermouth.net/news"),
    ("The Prog Report",   "https://progreport.com/category/latest-progressive-rock-news/"),
    ("The Prog Report",   "https://progreport.com/category/progressive-rock-reviews/"),
    ("The Progressive Subway", "https://theprogressivesubway.com/"),
    ("Decibel",           "https://www.decibelmagazine.com/category/news/"),
    ("Invisible Oranges", "https://www.invisibleoranges.com/news/"),
    ("New Noise",         "https://newnoisemagazine.com/news/"),
    ("Sputnikmusic",      "https://www.sputnikmusic.com/list_recent.php"),
    ("Stereogum",         "https://www.stereogum.com/category/news/"),
    ("Consequence",       "https://consequence.net/category/music/news/"),
    ("Pitchfork",         "https://pitchfork.com/news/"),
    # Louder Sound network: Metal Hammer, Classic Rock, Prog Magazine all live here.
    ("Louder",            "https://www.loudersound.com/news"),
    ("Metal Hammer",      "https://www.loudersound.com/metal-hammer"),
    ("Classic Rock",      "https://www.loudersound.com/classic-rock"),
    ("Prog Magazine",     "https://www.loudersound.com/prog"),
]


def fetch_journalism_pool() -> list[dict]:
    """Pull recent-articles index from each journalism site once. Returns a flat
    pool of {title, url, source, source_domain} we can match against.

    Most of these sites use WordPress; article cards live in <article>...<h2><a>.
    """
    pool: list[dict] = []
    seen_urls: set[str] = set()
    for source, url in JOURNALISM_INDEXES:
        try:
            r = fetch(url, sleep=0.5)
        except Exception as e:
            print(f"[pool] {source} ({url}): {e}", file=sys.stderr)
            continue
        soup = BeautifulSoup(r.text, "lxml")
        host = re.sub(r"^https?://(?:www\.)?", "", url).split("/")[0]
        anchors: list[tuple[str, str]] = []

        # Common WP patterns: article-card titles in h2/h3 with anchor.
        for sel in ("article h2 a", "article h3 a", "h2.entry-title a", "h3.entry-title a",
                    "h2.title a", ".post-title a", ".article-title a"):
            for a in soup.select(sel):
                href = a.get("href", "")
                title = a.get_text(" ", strip=True)
                if not href or not title:
                    continue
                if not href.startswith("http"):
                    continue
                anchors.append((title, href))
            if anchors:
                break

        # If no h2/h3 patterns matched, fall back to <a> with strong title text on the page.
        if not anchors:
            for a in soup.find_all("a", href=True):
                title = a.get_text(" ", strip=True)
                href = a["href"]
                if not href.startswith("http") or len(title) < 25 or len(title) > 200:
                    continue
                anchors.append((title, href))

        added = 0
        for title, href in anchors[:60]:
            if href in seen_urls:
                continue
            seen_urls.add(href)
            pool.append({"title": title, "url": href, "source": source, "source_domain": host})
            added += 1
        print(f"[pool] {source}: {added} articles", file=sys.stderr)
    print(f"[pool] total: {len(pool)} articles", file=sys.stderr)
    return pool


def normalize_token(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower())


def match_articles(release: dict, pool: list[dict], max_results: int = 8) -> list[dict]:
    """Return articles whose title matches the artist AND (album or release-related word).

    We look for the artist as a whole word, plus either a chunk of the album title
    or a release-keyword like "album", "review", "ep", "single".
    """
    artist_n = normalize_token(release.get("artist", ""))
    album_n = normalize_token(release.get("album", ""))
    if len(artist_n) < 2:
        return []

    # Build a few "album fingerprints" — quoted-strict, partial-strict, and word-set.
    album_strict = album_n
    album_words = [w for w in album_n.split() if len(w) > 3]

    def score_match(title: str) -> int:
        t = normalize_token(title)
        if artist_n not in t:
            # Artist not present → not a match.
            return 0
        if album_strict and album_strict in t:
            return 100
        # All long album words present?
        if album_words and all(w in t for w in album_words):
            return 80
        # Half of album words present + a release keyword?
        present_words = sum(1 for w in album_words if w in t)
        if album_words and present_words >= max(1, len(album_words) // 2) and \
           any(kw in t for kw in (" album ", " review ", " new ", " ep ", " single ")):
            return 50
        # Just artist + a strong release keyword (covers news mentions like "BAND announce new album")
        if any(kw in t for kw in (" announces ", " announce ", " unveils ", " reveals ", " releases ")):
            return 25
        return 0

    scored = []
    for art in pool:
        s = score_match(art["title"])
        if s > 0:
            scored.append((s, art))
    scored.sort(key=lambda x: -x[0])
    return [a for _, a in scored[:max_results]]


def fetch_progreport(window_start: date, window_end: date) -> list[dict]:
    """Pull recent reviews + news from The Prog Report. Each article becomes either a
    candidate release (when we can extract band+album from the title) or stays as
    a journalism article keyed to a release we already have.

    Returns a list with two shapes:
      - {source: "progreport-album", artist, album, release_date, source_url, ...}
        for entries we believe describe a specific album release.
      - {source: "progreport-article", artist, album, url, title}
        is _not_ returned here; articles are merged into releases via search_articles.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for url in (PROGREPORT_REVIEWS, PROGREPORT_NEWS, PROGREPORT_FEED):
        try:
            r = fetch(url, sleep=0.4)
        except Exception as e:
            print(f"[prog-report] {url}: {e}", file=sys.stderr)
            continue
        if url.endswith("/feed/"):
            soup = BeautifulSoup(r.text, "lxml-xml")
            items = soup.find_all("item")
            for it in items:
                title = (it.find("title").get_text(strip=True) if it.find("title") else "").strip()
                link = (it.find("link").get_text(strip=True) if it.find("link") else "").strip()
                pub = (it.find("pubDate").get_text(strip=True) if it.find("pubDate") else "").strip()
                if not title or not link or link in seen:
                    continue
                seen.add(link)
                rel = parse_progreport_release(title, link, pub, window_start, window_end)
                if rel:
                    out.append(rel)
        else:
            soup = BeautifulSoup(r.text, "lxml")
            for art in soup.select("article, h2.entry-title, h3.entry-title"):
                a = art.find("a", href=True) if art.name in ("article",) else art.find("a", href=True)
                if not a:
                    continue
                title = a.get_text(" ", strip=True)
                link = a["href"]
                if not title or not link or link in seen:
                    continue
                seen.add(link)
                # No reliable date on category pages; rely on RSS for date + supplement here.
                rel = parse_progreport_release(title, link, "", window_start, window_end, allow_no_date=True)
                if rel:
                    out.append(rel)
    print(f"[prog-report] {len(out)} prog releases extracted", file=sys.stderr)
    return out


PR_TITLE_PATTERNS = [
    re.compile(r"^(?P<artist>[^—–\-:\(\[]+?)\s+(?:—|–|-)\s+[^:]*?[“\"](?P<album>[^“”\"]+)[”\"]", re.I),
    re.compile(r"^(?P<artist>[^—–\-:\(\[]+?)\s+(?:—|–|-)\s+(?P<album>[^|—–\-:\(\[]+?)\s*(?:Album\s+Review|Review|EP\s+Review|—\s+Review|–\s+Review)", re.I),
    re.compile(r"^Album\s+Review:\s+(?P<artist>[^—–\-:\(\[]+?)\s+(?:—|–|-)\s+(?P<album>.+?)$", re.I),
    re.compile(r"^Review:\s+(?P<artist>[^—–\-:\(\[]+?)\s+(?:—|–|-)\s+(?P<album>.+?)$", re.I),
]


def parse_progreport_release(title: str, link: str, pub: str, window_start: date, window_end: date,
                              allow_no_date: bool = False,
                              source: str = "prog-report",
                              genres: list[str] | None = None) -> dict | None:
    title = title.strip()
    artist = album = None
    for pat in PR_TITLE_PATTERNS:
        m = pat.search(title)
        if m:
            artist = m.group("artist").strip(" -—–:")
            album = m.group("album").strip(" -—–:\"“”")
            break
    if not artist or not album:
        return None

    rdate: date | None = None
    if pub:
        try:
            rdate = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").date()
        except ValueError:
            try:
                rdate = datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S").date()
            except ValueError:
                pass
    if rdate and (rdate < window_start or rdate > window_end):
        return None
    if not rdate and not allow_no_date:
        return None

    return {
        "source": source,
        "artist": artist,
        "album": album,
        "release_date": (rdate or window_end).isoformat(),
        "album_type": "album",
        "genres": list(genres) if genres else ["Progressive Rock"],
        "source_url": link,
        "band_url": "",
        "cover": None,
    }


def fetch_progressive_subway(window_start: date, window_end: date) -> list[dict]:
    """Pull recent reviews from The Progressive Subway.

    Same shape as fetch_progreport: each post is a 'Review: Artist – Album' entry,
    so the existing PR_TITLE_PATTERNS and parse_progreport_release apply directly.
    The site is a WordPress blog with an RSS feed at /feed/.
    """
    out: list[dict] = []
    seen: set[str] = set()
    try:
        r = fetch(PROGSUBWAY_FEED, sleep=0.4)
    except Exception as e:
        print(f"[prog-subway] {PROGSUBWAY_FEED}: {e}", file=sys.stderr)
        return out
    soup = BeautifulSoup(r.text, "lxml-xml")
    for it in soup.find_all("item"):
        title = (it.find("title").get_text(strip=True) if it.find("title") else "").strip()
        link = (it.find("link").get_text(strip=True) if it.find("link") else "").strip()
        pub = (it.find("pubDate").get_text(strip=True) if it.find("pubDate") else "").strip()
        if not title or not link or link in seen:
            continue
        seen.add(link)
        rel = parse_progreport_release(
            title, link, pub, window_start, window_end,
            source="progressive-subway",
            genres=["Progressive Metal"],
        )
        if rel:
            out.append(rel)
    print(f"[prog-subway] {len(out)} prog releases extracted", file=sys.stderr)
    return out


def fetch_musicbrainz(window_start: date, window_end: date) -> list[dict]:
    """Tag-filtered release-group search across prog/fusion/instrumental tags.

    Public API, no auth, but rate-limited to 1 req/sec.
    """
    out: list[dict] = []
    seen: set[str] = set()
    tag_clause = " OR ".join(f'tag:"{t}"' for t in MB_TAGS)
    query = (
        f'primarytype:Album AND ({tag_clause}) '
        f'AND firstreleasedate:[{window_start.isoformat()} TO {window_end.isoformat()}]'
    )
    offset = 0
    page_size = 100
    while offset < 500:
        time.sleep(1.1)  # MB enforces 1 req/sec for anonymous access
        try:
            r = session.get(
                f"{MB_BASE}/release-group",
                params={"query": query, "fmt": "json", "limit": page_size, "offset": offset},
                headers={"User-Agent": MB_UA, "Accept": "application/json"},
                timeout=30,
            )
            if r.status_code == 503:
                time.sleep(3)
                continue
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[mb] offset={offset}: {e}", file=sys.stderr)
            break
        groups = data.get("release-groups", [])
        if not groups:
            break
        for rg in groups:
            mbid = rg.get("id")
            if not mbid or mbid in seen:
                continue
            seen.add(mbid)
            ac = rg.get("artist-credit") or []
            artist = "".join(
                (c.get("name") or (c.get("artist") or {}).get("name") or "") + (c.get("joinphrase") or "")
                for c in ac
            ).strip() or "Unknown"
            artist_mbid = ""
            if ac and isinstance(ac, list) and ac[0].get("artist"):
                artist_mbid = ac[0]["artist"].get("id", "")
            tags = sorted({t.get("name", "") for t in (rg.get("tags") or []) if t.get("name")})
            out.append({
                "source": "musicbrainz",
                "artist": artist,
                "album": rg.get("title", ""),
                "release_date": rg.get("first-release-date", ""),
                "album_type": rg.get("primary-type", "Album"),
                "genres": tags or [],
                "source_url": f"https://musicbrainz.org/release-group/{mbid}",
                "band_url": f"https://musicbrainz.org/artist/{artist_mbid}" if artist_mbid else "",
                "cover": f"https://coverartarchive.org/release-group/{mbid}/front-500",
            })
        offset += page_size
        if len(groups) < page_size:
            break
        total = data.get("count", 0)
        if offset >= total:
            break
    print(f"[mb] {len(out)} releases in window via tag-search", file=sys.stderr)
    return out


def fetch_wiki_cover(album_url: str) -> str | None:
    if not album_url:
        return None
    try:
        r = fetch(album_url, sleep=0.3)
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "lxml")
    # The infobox cover is usually inside .infobox-image, with an <img> whose src is upload.wikimedia.org.
    box = soup.find(class_="infobox-image") or soup.find("table", class_=re.compile("infobox"))
    if not box:
        return None
    img = box.find("img")
    if not img:
        return None
    src = img.get("src", "")
    if src.startswith("//"):
        src = "https:" + src
    return src or None


# ---------- merging / output ----------

def matches_genre(genres) -> bool:
    blob = " ".join(genres).lower() if isinstance(genres, list) else str(genres).lower()
    return any(k in blob for k in GENRE_KEYWORDS)


def normalize_key(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def search_links(artist: str, album: str) -> dict[str, str]:
    q = quote_plus(f"{artist} {album}")
    qa = quote_plus(artist)
    return {
        # spotify: URI opens the desktop/mobile app directly instead of the web player.
        "spotify": f"spotify:search:{q}",
        "youtube_music": f"https://music.youtube.com/search?q={q}",
        "bandcamp": f"https://bandcamp.com/search?q={q}",
        "discogs": f"https://www.discogs.com/search/?q={q}&type=release",
        "reviews": {
            "aoty": f"https://www.albumoftheyear.org/search/?q={q}",
            "metacritic": f"https://www.metacritic.com/search/{q}/?category=2",
            "sputnik": f"https://www.sputnikmusic.com/search_results.php?search_in=Albums&search_text={q}",
            "rym": f"https://rateyourmusic.com/search?searchterm={q}&searchtype=l",
        },
        "_artist_query": qa,
    }


SOURCE_BAND_FIELD = {
    "metal-archives": "ma_url",
    "wikipedia": "wikipedia_url",
    "musicbrainz": "mb_url",
    "prog-archives": "pa_url",
}


def merge_all(groups: list[list[dict]]) -> list[dict]:
    by_key: dict[str, dict] = {}
    for group in groups:
        for r in group:
            if not r.get("artist") or not r.get("album"):
                continue
            key = f"{normalize_key(r['artist'])}::{normalize_key(r['album'])}"
            band_field = SOURCE_BAND_FIELD.get(r["source"])
            if key in by_key:
                existing = by_key[key]
                existing["genres"] = sorted(set(existing.get("genres", []) + r.get("genres", [])))
                existing["sources"] = sorted(set(existing.get("sources", []) + [r["source"]]))
                if not existing.get("cover") and r.get("cover"):
                    existing["cover"] = r["cover"]
                if not existing.get("band_url") and r.get("band_url"):
                    existing["band_url"] = r["band_url"]
                if band_field and r.get("band_url") and not existing.get(band_field):
                    existing[band_field] = r["band_url"]
                continue
            entry = {**r, "sources": [r["source"]]}
            if band_field and r.get("band_url"):
                entry[band_field] = r["band_url"]
            by_key[key] = entry
    return list(by_key.values())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--week-of",
        help="Friday date YYYY-MM-DD to fetch; defaults to this calendar week's Friday.",
    )
    parser.add_argument(
        "--no-ma-enrich",
        action="store_true",
        help="Skip MA band-info / cover / tracklist enrichment. Used for the "
             "next-week preview run to keep ScrapingAnt credit usage low: we "
             "still get the release list, just without per-album rich data.",
    )
    args = parser.parse_args()

    if args.week_of:
        week_friday = datetime.strptime(args.week_of, "%Y-%m-%d").date()
        if week_friday.weekday() != 4:
            print(f"Warning: {week_friday} is not a Friday", file=sys.stderr)
    else:
        week_friday = previous_friday(date.today())
    window_start = week_friday - timedelta(days=6)
    print(f"Window: {window_start} .. {week_friday}", file=sys.stderr)

    def safe(label: str, fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            print(f"[{label}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            return []

    ma = safe("metal-archives", fetch_metal_archives, window_start, week_friday)
    wiki = safe("wikipedia", fetch_wikipedia, window_start, week_friday)
    mb = safe("musicbrainz", fetch_musicbrainz, window_start, week_friday)
    pr = safe("prog-report", fetch_progreport, window_start, week_friday)
    ps = safe("prog-subway", fetch_progressive_subway, window_start, week_friday)
    if not (ma or wiki or mb or pr or ps):
        print("All sources failed — refusing to overwrite data with an empty set.", file=sys.stderr)
        sys.exit(1)
    merged = merge_all([ma, wiki, mb, pr, ps])
    print(f"Merged: {len(merged)} unique releases", file=sys.stderr)

    # Pre-rank without MA band info so we only pay ScrapingAnt credits for the
    # bands of plausibly-kept releases. The pre-score uses signals we already
    # have: Wikipedia presence, source count, MA band age (via band ID).
    def _pre_score(r: dict) -> float:
        s = 0.0
        if "wikipedia" in r.get("sources", []):
            s += 60.0
        s += 5.0 * len(r.get("sources", []))
        bid = parse_ma_band_id(r.get("band_url", ""))
        if bid is not None:
            if bid < 50000: s += 25.0
            elif bid < 500000: s += 12.0
            elif bid < 3000000: s += 4.0
        return s

    # Keep ~2× MAX_TOTAL candidates so the band-info-driven re-rank can still
    # reshuffle meaningfully. Limits MA band-info calls hard.
    pre_ranked = sorted(merged, key=lambda r: -_pre_score(r))
    candidate_cap = max(MAX_TOTAL * 2, 80)
    candidates = pre_ranked[:candidate_cap]
    print(f"Pre-ranked: {len(candidates)} candidates kept for enrichment", file=sys.stderr)

    # Per-band info from MA (genres) — only for candidate releases.
    band_info: dict[str, dict] = {}
    if args.no_ma_enrich:
        print("Skipping MA band-info enrichment (--no-ma-enrich).", file=sys.stderr)
    else:
        band_urls = sorted({r.get("band_url", "") for r in candidates
                            if r.get("band_url", "").startswith("https://www.metal-archives.com/")})
        print(f"Fetching MA band stats for {len(band_urls)} bands...", file=sys.stderr)
        for i, bu in enumerate(band_urls):
            band_info[bu] = fetch_ma_band_info(bu)
            if (i + 1) % 25 == 0:
                print(f"  band-stats {i + 1}/{len(band_urls)}", file=sys.stderr)

    # Score, attach genres from band page if MA tagged "Metal" generically
    for r in merged:
        info = band_info.get(r.get("band_url", ""))
        if info and info["genres"] and r.get("genres") == ["Metal"]:
            r["genres"] = info["genres"]
        score, components = score_release(r, info)
        r["score"] = round(score, 2)
        r["score_components"] = components

    merged.sort(key=lambda r: (-r["score"], r["release_date"], r["artist"].lower()))
    kept = merged[:MAX_TOTAL]
    print(f"Keeping top {len(kept)} of {len(merged)} by score", file=sys.stderr)

    # When proxied through ScrapingAnt each MA HTML page costs ~25 credits, so
    # we skip tracklists entirely (cosmetic per-track YouTube only) and let
    # --no-ma-enrich also disable cover fetches when we want the cheapest run.
    skip_ma_html = args.no_ma_enrich or bool(SCRAPINGANT_KEY)
    tracklist_budget = 0 if skip_ma_html else len(kept)
    fetch_ma_covers = not args.no_ma_enrich

    print("Fetching covers + tracklists for kept set...", file=sys.stderr)
    for i, r in enumerate(kept):
        if not r.get("cover"):
            cover = None
            if (fetch_ma_covers and "metal-archives" in r["sources"]
                    and r.get("source_url", "").startswith("https://www.metal-archives.com/")):
                cover = fetch_ma_cover(r["source_url"])
            if not cover and r.get("source_url", "").startswith("https://en.wikipedia.org/"):
                cover = fetch_wiki_cover(r["source_url"])
            r["cover"] = cover
        # Tracklists: only Metal Archives gives us a clean structured list.
        if i < tracklist_budget and r.get("source_url", "").startswith("https://www.metal-archives.com/"):
            r["tracklist"] = fetch_ma_tracklist(r["source_url"])
        else:
            r["tracklist"] = []
        if (i + 1) % 10 == 0:
            print(f"  enrich {i + 1}/{len(kept)}", file=sys.stderr)

    print("Building journalism article pool (one fetch per site)...", file=sys.stderr)
    pool = fetch_journalism_pool()
    for r in kept:
        r["articles"] = match_articles(r, pool)
    matched = sum(1 for r in kept if r["articles"])
    print(f"Matched articles to {matched}/{len(kept)} kept releases.", file=sys.stderr)

    final: list[dict] = []
    for r in kept:
        links = search_links(r["artist"], r["album"])
        final.append({
            "artist": r["artist"],
            "album": r["album"],
            "cover": r.get("cover"),
            "release_date": r["release_date"],
            "album_type": r.get("album_type", "album"),
            "genres": r.get("genres", []),
            "sources": r["sources"],
            "source_url": r.get("source_url", ""),
            "band_url": r.get("band_url", ""),
            "ma_url": r.get("ma_url", ""),
            "wikipedia_url": r.get("wikipedia_url", ""),
            "mb_url": r.get("mb_url", ""),
            "pa_url": r.get("pa_url", ""),
            "bio_url": r.get("wikipedia_url") or r.get("ma_url") or r.get("mb_url") or r.get("band_url", ""),
            "score": r.get("score", 0),
            "score_components": r.get("score_components", {}),
            "articles": r.get("articles", []),
            "tracklist": r.get("tracklist", []),
            "spotify": links["spotify"],
            "youtube_music": links["youtube_music"],
            "bandcamp": links["bandcamp"],
            "discogs": links["discogs"],
            "reviews": links["reviews"],
        })

    # already sorted by score; keep that order for the page

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "week_of": week_friday.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": week_friday.isoformat(),
        "count": len(final),
        "releases": final,
    }

    weeks_dir = DATA_DIR / "weeks"
    weeks_dir.mkdir(parents=True, exist_ok=True)
    week_file = weeks_dir / f"{week_friday.isoformat()}.json"
    week_file.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"Wrote {week_file} with {len(final)} releases.", file=sys.stderr)

    # Maintain data/index.json: latest pointer + list of all available weeks (newest first).
    index_file = DATA_DIR / "index.json"
    if index_file.exists():
        try:
            index = json.loads(index_file.read_text())
        except ValueError:
            index = {"weeks": []}
    else:
        index = {"weeks": []}

    today = date.today()
    is_future = week_friday > today
    by_week = {w["week_of"]: w for w in index.get("weeks", [])}
    by_week[week_friday.isoformat()] = {
        "week_of": week_friday.isoformat(),
        "count": len(final),
        "generated_at": out["generated_at"],
        "is_future": is_future,
    }
    index["weeks"] = sorted(by_week.values(), key=lambda w: w["week_of"], reverse=True)
    # latest = most recent week_of that is <= today
    past = [w for w in index["weeks"] if w["week_of"] <= today.isoformat()]
    index["latest"] = past[0]["week_of"] if past else (index["weeks"][0]["week_of"] if index["weeks"] else None)
    # next = the soonest future week_of, if any
    future = sorted([w for w in index["weeks"] if w["week_of"] > today.isoformat()],
                    key=lambda w: w["week_of"])
    index["next"] = future[0]["week_of"] if future else None
    index_file.write_text(json.dumps(index, indent=2))
    print(f"Updated {index_file}: {len(index['weeks'])} weeks tracked. latest={index['latest']} next={index['next']}", file=sys.stderr)


if __name__ == "__main__":
    main()
