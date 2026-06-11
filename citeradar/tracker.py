"""
tracker.py — Citation Tracker

For every paper in ``papers.json`` that has at least one citation, visits
the Google Scholar "Cited by" page and retrieves all citing papers.

Anti-ban strategy
-----------------
- Uses a realistic browser User-Agent header.
- Enforces a 2-second minimum delay between Scholar requests.
- On HTTP 429, backs off for 30 seconds and retries once.
- Optionally enriches truncated author lists via the CrossRef API (free,
  no key required) to minimise the number of Scholar page requests.

Non-breaking-space fix
----------------------
Scholar's ``<div class="gs_a">`` sometimes uses U+00A0 (non-breaking space)
around the " – " separators.  ``parse_meta()`` normalises all whitespace
variants before splitting, ensuring correct venue/year extraction.
"""

import json
import time
import csv
import re
from dataclasses import dataclass, asdict
from typing import Optional
import requests
from bs4 import BeautifulSoup

from .errors import RateLimitError


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CitingPaper:
    cited_paper_title: str    # which of YOUR papers was cited
    title: str                # title of the citing paper
    authors: str              # full author list (enriched via CrossRef if available)
    authors_complete: bool    # True if author list is confirmed complete
    venue: str                # journal or conference
    year: str
    citing_url: str           # link to the citing paper on Scholar


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

BASE_URL       = "https://scholar.google.com"
CROSSREF_URL   = "https://api.crossref.org/works"
DELAY_SECONDS  = 2


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _looks_blocked_by_scholar(soup: BeautifulSoup) -> bool:
    """Return True for Scholar CAPTCHA / unusual-traffic pages."""
    if soup.select_one(
        "form[action*='/sorry/'], input[name='captcha'], "
        "#captcha, #captcha-form, .g-recaptcha, #gs_captcha_ccl"
    ):
        return True

    text = soup.get_text(" ", strip=True).lower()
    return any(phrase in text for phrase in (
        "our systems have detected unusual traffic",
        "not a robot",
        "to continue, please type the characters",
    ))


def _get_soup(url: str, session: requests.Session) -> Optional[BeautifulSoup]:
    """GET a URL and return parsed HTML, with one 429-retry."""
    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        raise RateLimitError(f"Google Scholar request failed: {e}") from e
    if resp.status_code == 429:
        print("    [!] Rate-limited (HTTP 429) — waiting 30 s…")
        time.sleep(30)
        try:
            resp = session.get(url, headers=HEADERS, timeout=15)
        except requests.RequestException as e:
            raise RateLimitError(f"Google Scholar request failed: {e}") from e
        if resp.status_code == 429:
            raise RateLimitError("Google Scholar rate limit while tracking citations")
    if resp.status_code != 200:
        raise RateLimitError(f"Google Scholar returned HTTP {resp.status_code} for {url}")

    soup = BeautifulSoup(resp.text, "html.parser")
    if _looks_blocked_by_scholar(soup):
        print("    [!] Google Scholar returned a CAPTCHA/block page — waiting 30 s…")
        time.sleep(30)
        try:
            resp = session.get(url, headers=HEADERS, timeout=15)
        except requests.RequestException as e:
            raise RateLimitError(f"Google Scholar request failed: {e}") from e
        if resp.status_code == 429:
            raise RateLimitError("Google Scholar rate limit while tracking citations")
        if resp.status_code != 200:
            raise RateLimitError(
                f"Google Scholar returned HTTP {resp.status_code} for {url}"
            )
        soup = BeautifulSoup(resp.text, "html.parser")
        if _looks_blocked_by_scholar(soup):
            raise RateLimitError(
                "Google Scholar returned a CAPTCHA/block page while tracking citations"
            )

    return soup


# ---------------------------------------------------------------------------
# Meta-string parser
# ---------------------------------------------------------------------------

def parse_meta(raw: str) -> tuple[str, str, str]:
    """
    Split the Scholar ``gs_a`` meta string into ``(authors, venue, year)``.

    Scholar formats this field as::

        Authors - Venue, Year - Publisher

    but uses U+00A0 (non-breaking space) around the dash separators, so we
    normalise all whitespace before splitting.
    """
    raw = raw.replace("\xa0", " ").replace("\u2013", "-").replace("\u2014", "-")
    raw = re.sub(r" +", " ", raw).strip()

    parts   = re.split(r" - ", raw)
    authors = parts[0].strip() if parts else ""
    year    = ""
    venue   = ""

    if len(parts) >= 2:
        venue_year = parts[1].strip()
        all_years  = list(re.finditer(r"\b(19|20)\d{2}\b", venue_year))
        if all_years:
            m     = all_years[-1]            # use the LAST year found
            year  = m.group(0)
            venue = venue_year[: m.start()].strip().rstrip(",").strip()
            if not venue:
                venue = venue_year[m.end():].strip().lstrip(",").strip()
        else:
            venue = venue_year

    if not year:
        for part in parts[2:]:
            m = re.search(r"\b(19|20)\d{2}\b", part)
            if m:
                year = m.group(0)
                break

    venue = re.sub(r",?\s*\b(19|20)\d{2}\b\s*$", "", venue).strip()
    venue = re.sub(r"[,\s]+$", "", venue).strip()

    return authors, venue, year


# ---------------------------------------------------------------------------
# CrossRef author enrichment
# ---------------------------------------------------------------------------

def _title_similar(a: str, b: str) -> bool:
    """Return True if two title strings share ≥50 % of their words."""
    wa = set(re.findall(r"\w+", a.lower()))
    wb = set(re.findall(r"\w+", b.lower()))
    if not wa or not wb:
        return False
    return len(wa & wb) / max(len(wa), len(wb)) >= 0.5


def _crossref_full_authors(title: str, session: requests.Session) -> Optional[str]:
    """
    Query CrossRef to get the complete author list for a paper.
    Returns a formatted ``"First Last, First Last, …"`` string, or ``None``.
    CrossRef is free and does not require an API key.
    """
    try:
        resp = session.get(
            CROSSREF_URL,
            params={"query.title": title, "rows": 1},
            headers={"User-Agent": "CiteRadar/1.0 (mailto:citeradar@example.com)"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        items = resp.json().get("message", {}).get("items", [])
        if not items:
            return None
        item     = items[0]
        cr_title = " ".join(item.get("title", [""])).lower()
        if not _title_similar(title.lower(), cr_title):
            return None
        authors = item.get("author", [])
        if not authors:
            return None
        parts = []
        for a in authors:
            given  = a.get("given",  "").strip()
            family = a.get("family", "").strip()
            name   = f"{given} {family}".strip() if given else family
            if name:
                parts.append(name)
        return ", ".join(parts) if parts else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Page parsers
# ---------------------------------------------------------------------------

def _get_cited_by_url(paper_url: str, session: requests.Session) -> Optional[str]:
    """Return the 'Cited by' search URL for a paper's Scholar detail page."""
    soup = _get_soup(paper_url, session)
    if soup is None:
        return None
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True).lower().startswith("cited by"):
            href = a["href"]
            return href if href.startswith("http") else BASE_URL + href
    return None


def _parse_citing_papers(soup: BeautifulSoup, cited_title: str,
                          session: requests.Session, enrich: bool) -> list[CitingPaper]:
    """Parse all result cards on a Scholar search-results page."""
    results = []
    for div in soup.select("div.gs_r.gs_or"):
        title_tag = div.select_one("h3.gs_rt a")
        if title_tag:
            title      = title_tag.get_text(strip=True)
            href       = title_tag.get("href", "")
            citing_url = href if href.startswith("http") else BASE_URL + href
        else:
            title_no_link = div.select_one("h3.gs_rt")
            title         = title_no_link.get_text(strip=True) if title_no_link else ""
            citing_url    = ""

        title = re.sub(r"^\[.*?\]\s*", "", title)   # strip [PDF] [HTML] prefixes

        meta_tag = div.select_one("div.gs_a")
        raw_meta = meta_tag.get_text(separator=" ", strip=True) if meta_tag else ""
        authors, venue, year = parse_meta(raw_meta)

        authors_complete = "\u2026" not in authors and "..." not in authors
        if enrich and not authors_complete and title:
            full = _crossref_full_authors(title, session)
            if full:
                authors          = full
                authors_complete = True

        if title:
            results.append(CitingPaper(
                cited_paper_title=cited_title,
                title=title,
                authors=authors,
                authors_complete=authors_complete,
                venue=venue,
                year=year,
                citing_url=citing_url,
            ))
    return results


def _has_next_page(soup: BeautifulSoup) -> bool:
    """Return True if a 'Next' pagination link exists."""
    if soup.select_one("td#gs_n a[aria-label='Next']"):
        return True
    nav = soup.select_one("td#gs_n")
    if nav:
        for a in nav.find_all("a"):
            if "next" in a.get_text(strip=True).lower():
                return True
    if soup.select_one("button.gs_btnPR:not([disabled])"):
        return True
    return False


def _fetch_all_citing(cited_title: str, cited_by_url: str,
                      session: requests.Session, enrich: bool,
                      expected_citations: int = 0, start: int = 0,
                      on_page=None) -> list[CitingPaper]:
    """Paginate through all citing papers for a single paper."""
    all_citing: list[CitingPaper] = []
    while True:
        soup = _get_soup(f"{cited_by_url}&start={start}", session)
        if soup is None:
            break
        page = _parse_citing_papers(soup, cited_title, session, enrich)
        if not page and expected_citations > start:
            raise RateLimitError(
                "Google Scholar returned no citing results for "
                f"'{cited_title}' despite {expected_citations} claimed citations"
            )
        all_citing.extend(page)
        print(f"    [{start + 1}–{start + len(page)}] {len(page)} papers retrieved")
        has_next = _has_next_page(soup)
        next_start = start + 10 if has_next else None
        if has_next and on_page:
            on_page(page, start, next_start)
        if not has_next:
            break
        start = next_start
        time.sleep(DELAY_SECONDS)
    return all_citing


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _paper_key(paper: dict) -> str:
    """Stable key for resuming per-paper citation tracking."""
    return paper.get("paper_url") or f"{paper.get('title', '')}|{paper.get('year', '')}"


def _citation_dict(citation) -> dict:
    return asdict(citation) if isinstance(citation, CitingPaper) else citation


def _citation_from_dict(data: dict) -> CitingPaper:
    fields = CitingPaper.__dataclass_fields__
    return CitingPaper(**{k: data.get(k, "") for k in fields})


def _dedupe_citations(citations: list[CitingPaper]) -> list[CitingPaper]:
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[CitingPaper] = []
    for citation in citations:
        data = _citation_dict(citation)
        key = (
            data.get("cited_paper_title", ""),
            data.get("title", ""),
            data.get("year", ""),
            data.get("citing_url", ""),
        )
        if key not in seen:
            seen.add(key)
            deduped.append(_citation_from_dict(data))
    return deduped


def _load_citation_checkpoint(json_path: str, enrich: bool) -> tuple[
    list[CitingPaper], list[dict], list[str], dict
]:
    if not json_path:
        return [], [], [], {}
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return [], [], [], {}

    if not data.get("complete", True) and data.get("enrich") is not None:
        if data.get("enrich") != enrich:
            raise ValueError(
                "Cannot resume citations with a different --no-enrich setting. "
                "Rerun with the original option or remove citations.json."
            )

    citations = [
        _citation_from_dict(c)
        for c in data.get("citations", [])
        if isinstance(c, dict)
    ]
    return (
        _dedupe_citations(citations),
        data.get("summary", []),
        data.get("processed_papers", []),
        data.get("in_progress_paper", {}) if not data.get("complete", True) else {},
    )


def track_citations(papers_data: dict, enrich: bool = True,
                    author_name: str = "", json_path: str = "",
                    csv_path: str = "") -> tuple[list[CitingPaper], list[dict]]:
    """
    For each paper in ``papers_data["papers"]`` that has citations, fetch all
    citing papers from Google Scholar.

    Parameters
    ----------
    papers_data : dict
        The JSON object produced by :func:`scraper.scrape_profile`.
    enrich : bool
        If ``True``, resolve truncated Scholar author lists via CrossRef.

    Returns
    -------
    all_citing : list[CitingPaper]
    summary    : list[dict]
        Per-paper retrieval statistics.
    """
    papers       = papers_data["papers"]
    cited_papers = [p for p in papers if p["citations"] > 0]

    print(f"Papers with citations: {len(cited_papers)} / {len(papers)}")
    print(f"CrossRef author enrichment: {'enabled' if enrich else 'disabled'}\n")

    session = requests.Session()
    all_citing, summary, processed_papers, in_progress = _load_citation_checkpoint(
        json_path, enrich
    )
    processed = set(processed_papers)

    if processed:
        print(f"Resuming citations: {len(processed)} papers already processed.\n")
    if in_progress:
        print(
            "Resuming in-progress citation search: "
            f"{in_progress.get('title', '')} "
            f"at result offset {in_progress.get('next_start', 0)}.\n"
        )

    for i, paper in enumerate(cited_papers, 1):
        title       = paper["title"]
        n_citations = paper["citations"]
        key         = _paper_key(paper)

        if key in processed:
            print(f"[{i}/{len(cited_papers)}] {title}")
            print("  Already processed — skipping.\n")
            continue

        print(f"[{i}/{len(cited_papers)}] {title}")
        print(f"  Claimed citations: {n_citations}")

        resumed_paper = in_progress if in_progress.get("key") == key else {}
        cited_by_url = resumed_paper.get("cited_by_url", "")
        start = int(resumed_paper.get("next_start", 0) or 0)

        if start and not any(c.cited_paper_title == title for c in all_citing):
            start = 0

        if cited_by_url:
            print(f"  Resuming citing results at offset {start}.")
        else:
            time.sleep(DELAY_SECONDS)
            cited_by_url = _get_cited_by_url(paper["paper_url"], session)

        if not cited_by_url:
            raise RateLimitError(
                "Could not find 'Cited by' link for "
                f"'{title}'. Google Scholar may have returned a CAPTCHA/block page."
            )

        def checkpoint_page(page: list[CitingPaper], page_start: int,
                            next_start: int) -> None:
            nonlocal all_citing
            all_citing.extend(page)
            all_citing = _dedupe_citations(all_citing)
            if json_path and csv_path:
                save_citations(
                    author_name, all_citing, summary,
                    json_path, csv_path, processed_papers,
                    complete=False, enrich=enrich, quiet=True,
                    in_progress={
                        "key": key,
                        "title": title,
                        "cited_by_url": cited_by_url,
                        "next_start": next_start,
                    },
                )

        time.sleep(DELAY_SECONDS)
        citing = _fetch_all_citing(
            title, cited_by_url, session, enrich,
            expected_citations=n_citations, start=start,
            on_page=checkpoint_page,
        )
        all_citing.extend(citing)
        all_citing = _dedupe_citations(all_citing)
        retrieved_citations = sum(
            1 for c in all_citing if c.cited_paper_title == title
        )

        print(f"  → {retrieved_citations} citing papers retrieved.\n")
        summary.append({
            "your_paper":          title,
            "year":                paper["year"],
            "venue":               paper["venue"],
            "claimed_citations":   n_citations,
            "retrieved_citations": retrieved_citations,
        })
        processed.add(key)
        processed_papers.append(key)
        if json_path and csv_path:
            save_citations(author_name, all_citing, summary,
                           json_path, csv_path, processed_papers,
                           complete=False, enrich=enrich, quiet=True)
        time.sleep(DELAY_SECONDS)

    if json_path and csv_path:
        save_citations(author_name, all_citing, summary,
                       json_path, csv_path, processed_papers,
                       complete=True, enrich=enrich)

    return all_citing, summary


def save_citations(author_name: str, all_citing: list[CitingPaper],
                   summary: list[dict], json_path: str, csv_path: str,
                   processed_papers: Optional[list[str]] = None,
                   complete: bool = True, enrich: Optional[bool] = None,
                   quiet: bool = False,
                   in_progress: Optional[dict] = None) -> None:
    """Persist citation data to JSON and CSV."""
    data = [_citation_dict(c) for c in _dedupe_citations(all_citing)]
    processed_papers = list(dict.fromkeys(processed_papers or []))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "author":               author_name,
                "total_citing_papers":  len(data),
                "complete":             complete,
                "enrich":               enrich,
                "processed_papers":     processed_papers,
                "in_progress_paper":    in_progress or {},
                "summary":              summary,
                "citations":            data,
            },
            f, ensure_ascii=False, indent=2,
        )

    fields = list(CitingPaper.__dataclass_fields__.keys())
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(data)

    if quiet:
        print(f"  Checkpoint saved → {json_path}")
    else:
        print(f"\nCitations saved → {json_path}, {csv_path}")
        print(f"\nRetrieval summary:")
        for s in summary:
            print(f"  [{s['claimed_citations']:>3} cited / "
                  f"{s['retrieved_citations']:>3} retrieved]  {s['your_paper']}")
