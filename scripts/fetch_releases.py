"""Fetch this week's metal/prog album releases from Metal Archives + Wikipedia.

No third-party APIs; just polite HTTP scraping. Outputs data/releases.json.
"""

import json
import math
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from curl_cffi import requests
from bs4 import BeautifulSoup

MAX_TOTAL = 60  # 12 in the digest, 48 in the lazy-loaded tail

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
)

MA_ADVANCED = "https://www.metal-archives.com/search/ajax-advanced/searching/albums"
WIKI_LIST = "https://en.wikipedia.org/wiki/List_of_{year}_albums"

MONTH_NUM = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}

OUT = Path(__file__).resolve().parent.parent / "data" / "releases.json"

# curl_cffi's Session impersonates a real browser's TLS+HTTP fingerprint so we
# get past Cloudflare's bot-mitigation challenge that blocks plain `requests`.
session = requests.Session(impersonate="chrome")
session.headers.update(HEADERS)


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

def fetch(url: str, *, params: dict | None = None, sleep: float = 0.5,
          extra_headers: dict | None = None):
    for attempt in range(4):
        try:
            r = session.get(url, params=params, headers=extra_headers, timeout=25)
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


def fetch_metal_archives(window_start: date, window_end: date) -> list[dict]:
    """Use MA advanced-search ajax: filter by year/month range, parse ISO date from HTML comment.

    All bands on MA are metal-related, so we don't filter by genre here — Wikipedia is where
    we apply genre keyword filtering for prog rock, etc.
    """
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
            try:
                data = r.json()
            except ValueError:
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
    """Fetch genres from band page + aggregate review stats from discography page."""
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

    out: list[dict] = []
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
                out.append(row)

    out = [r for r in out if matches_genre(r.get("genres", []))]
    print(f"[wiki] {len(out)} metal/prog releases in window", file=sys.stderr)
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
        "spotify": f"https://open.spotify.com/search/{q}",
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


def merge(ma: list[dict], wiki: list[dict]) -> list[dict]:
    by_key: dict[str, dict] = {}
    # MA first — it has reliable genre tags for metal
    for r in ma + wiki:
        key = f"{normalize_key(r['artist'])}::{normalize_key(r['album'])}"
        if key in by_key:
            existing = by_key[key]
            # Merge genre lists
            merged_genres = sorted(set(existing.get("genres", []) + r.get("genres", [])))
            existing["genres"] = merged_genres
            existing["sources"] = sorted(set(existing.get("sources", []) + [r["source"]]))
            # Prefer MA source_url for cover fetching since it's more direct
            continue
        by_key[key] = {
            **r,
            "sources": [r["source"]],
        }
    return list(by_key.values())


def main() -> None:
    today = date.today()
    week_friday = previous_friday(today)
    window_start = week_friday - timedelta(days=6)
    print(f"Window: {window_start} .. {week_friday}", file=sys.stderr)

    ma = fetch_metal_archives(window_start, week_friday)
    wiki = fetch_wikipedia(window_start, week_friday)
    merged = merge(ma, wiki)
    print(f"Merged: {len(merged)} unique releases", file=sys.stderr)

    # Per-band info from MA (genres, rating, review count) — used for scoring. Cached by URL.
    band_urls = sorted({r.get("band_url", "") for r in merged
                        if r.get("band_url", "").startswith("https://www.metal-archives.com/")})
    print(f"Fetching MA band stats for {len(band_urls)} bands...", file=sys.stderr)
    band_info: dict[str, dict] = {}
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

    print("Fetching covers for kept set...", file=sys.stderr)
    for i, r in enumerate(kept):
        cover = None
        if "metal-archives" in r["sources"] and r.get("source_url", "").startswith("https://www.metal-archives.com/"):
            cover = fetch_ma_cover(r["source_url"])
        if not cover and r.get("source_url", "").startswith("https://en.wikipedia.org/"):
            cover = fetch_wiki_cover(r["source_url"])
        r["cover"] = cover
        if (i + 1) % 10 == 0:
            print(f"  cover {i + 1}/{len(kept)}", file=sys.stderr)

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
            "score": r.get("score", 0),
            "score_components": r.get("score_components", {}),
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

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(out, indent=2, ensure_ascii=False)
    OUT.write_text(payload)
    # Also emit a JS-wrapped version so the static page works under file:// (no fetch).
    js_path = OUT.with_suffix(".js")
    js_path.write_text(f"window.RELEASES_DATA = {payload};\n")
    print(f"Wrote {OUT} and {js_path} with {len(final)} releases.", file=sys.stderr)


if __name__ == "__main__":
    main()
