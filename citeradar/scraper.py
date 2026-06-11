"""
scraper.py — Google Scholar Profile Scraper

Fetches all papers listed on a researcher's Google Scholar profile page,
including title, authors, venue, year, and citation count.

Rate-limiting strategy
----------------------
- PAGE_SIZE = 100 (maximum allowed by Scholar) minimises the number of HTTP
  requests needed to retrieve a full publication list.
- DELAY_SECONDS = 2 between pages provides a respectful crawl rate.
- On HTTP 429 (Too Many Requests) the code waits 30 s and retries once.
"""

import json
import time
import csv
from dataclasses import dataclass, asdict
from typing import Optional
import requests
from bs4 import BeautifulSoup

from .errors import RateLimitError


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Paper:
    title: str
    authors: str
    venue: str       # journal or conference
    year: str
    citations: int
    paper_url: str   # link to the paper detail page on Scholar


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

BASE_URL = "https://scholar.google.com"
PAGE_SIZE = 100       # max allowed by Scholar
DELAY_SECONDS = 2     # polite delay between requests


# ---------------------------------------------------------------------------
# Core scraping helpers
# ---------------------------------------------------------------------------

def _fetch_page(user_id: str, start: int, session: requests.Session) -> Optional[BeautifulSoup]:
    """Fetch one page of a Scholar profile and return parsed HTML."""
    url = (
        f"{BASE_URL}/citations"
        f"?user={user_id}&sortby=pubdate&pagesize={PAGE_SIZE}&cstart={start}"
    )
    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        raise RateLimitError(f"Google Scholar request failed: {e}") from e

    if resp.status_code == 429:
        print("  [!] Rate-limited (HTTP 429). Waiting 30 s before retry…")
        time.sleep(30)
        try:
            resp = session.get(url, headers=HEADERS, timeout=15)
        except requests.RequestException as e:
            raise RateLimitError(f"Google Scholar request failed: {e}") from e
        if resp.status_code == 429:
            raise RateLimitError("Google Scholar rate limit while scraping profile")

    if resp.status_code != 200:
        print(f"  [!] HTTP {resp.status_code} for {url}")
        return None

    return BeautifulSoup(resp.text, "html.parser")


def _parse_author_info(soup: BeautifulSoup) -> dict:
    """Extract the author's name and affiliation from the profile page."""
    name_tag  = soup.find("div", id="gsc_prf_in")
    affil_tag = soup.find("div", class_="gsc_prf_il")
    return {
        "name":        name_tag.get_text(strip=True)  if name_tag  else "Unknown",
        "affiliation": affil_tag.get_text(strip=True) if affil_tag else "",
    }


def _parse_papers(soup: BeautifulSoup) -> list[Paper]:
    """Parse all paper rows visible on the current page."""
    papers = []
    for row in soup.select("tr.gsc_a_tr"):
        title_tag  = row.select_one("a.gsc_a_at")
        title      = title_tag.get_text(strip=True) if title_tag else ""
        paper_url  = (BASE_URL + title_tag["href"]) if (title_tag and title_tag.get("href")) else ""

        gray_divs = row.select("div.gs_gray")
        authors   = gray_divs[0].get_text(strip=True) if len(gray_divs) > 0 else ""
        venue     = gray_divs[1].get_text(strip=True) if len(gray_divs) > 1 else ""

        year_tag  = row.select_one("span.gsc_a_h")
        year      = year_tag.get_text(strip=True) if year_tag else ""

        cite_tag  = row.select_one("a.gsc_a_ac")
        cite_text = cite_tag.get_text(strip=True) if cite_tag else "0"
        try:
            citations = int(cite_text)
        except ValueError:
            citations = 0

        if title:
            papers.append(Paper(title=title, authors=authors, venue=venue,
                                year=year, citations=citations, paper_url=paper_url))
    return papers


def _has_more_pages(soup: BeautifulSoup, page_papers: int) -> bool:
    """Return True if there are more papers to load."""
    btn = soup.select_one("button#gsc_bpf_more")
    if btn and btn.get("disabled"):
        return False
    return page_papers == PAGE_SIZE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scrape_profile(user_id: str) -> tuple[dict, list[Paper]]:
    """
    Scrape all papers from a Google Scholar profile.

    Parameters
    ----------
    user_id : str
        The Scholar user ID found in the profile URL after ``?user=``.

    Returns
    -------
    author_info : dict
        ``{"name": str, "affiliation": str}``
    papers : list[Paper]
        All papers found on the profile, sorted by publication date.
    """
    session     = requests.Session()
    all_papers: list[Paper] = []
    author_info = {}
    start       = 0

    print(f"Scraping Scholar profile: {BASE_URL}/citations?user={user_id}\n")

    while True:
        print(f"  Fetching papers {start + 1}–{start + PAGE_SIZE}…")
        soup = _fetch_page(user_id, start, session)
        if soup is None:
            print("  Stopping — fetch error.")
            break

        if start == 0:
            author_info = _parse_author_info(soup)
            print(f"  Author : {author_info['name']}")
            print(f"  Affil  : {author_info['affiliation']}\n")

        page_papers = _parse_papers(soup)
        all_papers.extend(page_papers)
        print(f"  → {len(page_papers)} papers on this page (total: {len(all_papers)})")

        if not _has_more_pages(soup, len(page_papers)):
            print("  No more pages.")
            break

        start += PAGE_SIZE
        time.sleep(DELAY_SECONDS)

    return author_info, all_papers


def save_papers(author_info: dict, papers: list[Paper],
                json_path: str, csv_path: str, scholar_id: str = "") -> None:
    """Persist papers to JSON and CSV."""
    data = {
        "author":       author_info,
        "scholar_id":   scholar_id,
        "total_papers": len(papers),
        "complete":     True,
        "papers":       [asdict(p) for p in papers],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  JSON saved → {json_path}")

    if papers:
        fields = list(asdict(papers[0]).keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(asdict(p) for p in papers)
        print(f"  CSV  saved → {csv_path}")
