"""
profiler.py — Author Profiler

For every unique citing paper discovered by the tracker, resolves full
per-author metadata: full name, institution, city, country.

Data-source priority
--------------------
1. **OpenAlex** — best institutional-affiliation coverage, free, no API key.
   Also yields an ``openalex_author_id`` used later by the ranker for
   unambiguous h-index look-ups.
2. **Semantic Scholar** — fallback for papers absent from OpenAlex.
3. **CrossRef** — last resort; provides reliable full names but rarely
   includes affiliations.

City look-up fix
----------------
OpenAlex institution records are referenced by *web* URLs such as
``https://openalex.org/I27837315``.  These must be converted to the
*API* endpoint ``https://api.openalex.org/institutions/I27837315``
before the JSON response can be fetched.  A module-level cache
``_inst_city_cache`` prevents duplicate requests.

Organisation filtering
----------------------
``_is_person()`` rejects entries whose display_name matches known
organisation keywords (university, consortium, etc.) or contains digits
(e.g. "AAAI 2026"), preventing false author records.
"""

import json
import time
import csv
import re
from dataclasses import dataclass, asdict
from typing import Optional
from collections import Counter
import requests

from .errors import RateLimitError


# ---------------------------------------------------------------------------
# ISO 3166-1 alpha-2 → country name
# ---------------------------------------------------------------------------

COUNTRY_NAMES: dict[str, str] = {
    "US": "United States",  "CN": "China",          "GB": "United Kingdom",
    "DE": "Germany",        "FR": "France",          "JP": "Japan",
    "KR": "South Korea",    "CA": "Canada",          "AU": "Australia",
    "IN": "India",          "IT": "Italy",           "NL": "Netherlands",
    "ES": "Spain",          "CH": "Switzerland",     "SE": "Sweden",
    "SG": "Singapore",      "BR": "Brazil",          "IL": "Israel",
    "AT": "Austria",        "BE": "Belgium",         "DK": "Denmark",
    "FI": "Finland",        "NO": "Norway",          "PL": "Poland",
    "PT": "Portugal",       "CZ": "Czech Republic",  "HK": "Hong Kong",
    "TW": "Taiwan",         "RU": "Russia",          "SA": "Saudi Arabia",
    "AE": "United Arab Emirates", "MX": "Mexico",    "AR": "Argentina",
    "ZA": "South Africa",   "EG": "Egypt",           "IR": "Iran",
    "TR": "Turkey",         "PK": "Pakistan",        "NG": "Nigeria",
    "NZ": "New Zealand",    "GR": "Greece",          "HU": "Hungary",
    "RO": "Romania",        "UA": "Ukraine",         "MY": "Malaysia",
    "TH": "Thailand",       "ID": "Indonesia",       "VN": "Vietnam",
    "PH": "Philippines",    "CL": "Chile",           "CO": "Colombia",
    "LT": "Lithuania",      "LV": "Latvia",          "EE": "Estonia",
    "SK": "Slovakia",       "SI": "Slovenia",        "HR": "Croatia",
    "RS": "Serbia",         "BG": "Bulgaria",        "IE": "Ireland",
    "LU": "Luxembourg",     "IS": "Iceland",         "CY": "Cyprus",
    "MT": "Malta",          "LK": "Sri Lanka",       "BD": "Bangladesh",
    "QA": "Qatar",          "KW": "Kuwait",          "BH": "Bahrain",
    "OM": "Oman",           "JO": "Jordan",          "LB": "Lebanon",
    "MA": "Morocco",        "TN": "Tunisia",         "DZ": "Algeria",
    "KZ": "Kazakhstan",     "UZ": "Uzbekistan",
}


def _country_name(code: str) -> str:
    return COUNTRY_NAMES.get(code.upper(), code) if code else ""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class AuthorRecord:
    full_name:         str
    first_name:        str
    last_name:         str
    institution:       str
    city:              str
    country_code:      str
    country:           str
    citing_paper_title: str
    citing_paper_year:  str
    cited_paper_title:  str   # which of YOUR papers they cited
    data_source:        str   # openalex | semantic_scholar | crossref
    openalex_author_id: str   # e.g. https://openalex.org/A12345


# ---------------------------------------------------------------------------
# Organisation filter
# ---------------------------------------------------------------------------

_ORG_KEYWORDS = {
    "association", "university", "institute", "institution", "laboratory",
    "conference", "proceedings", "workshop", "journal", "society", "committee",
    "department", "division", "center", "centre", "foundation", "corporation",
    "company", "inc", "ltd", "llc", "press", "publisher", "group", "team",
    "network", "consortium", "council", "academy", "school", "college",
    "ministry", "government", "agency", "bureau", "office", "board",
}


def _is_person(name: str) -> bool:
    """Return False if the name looks like an organisation rather than a person."""
    lower = name.lower()
    for kw in _ORG_KEYWORDS:
        if re.search(rf"\b{kw}\b", lower):
            return False
    return not bool(re.search(r"\d", name))


def _split_name(display_name: str) -> tuple[str, str]:
    """Split ``'First Last'`` or ``'Last, First'`` into ``(first, last)``."""
    name = display_name.strip()
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        return parts[1], parts[0]
    parts = name.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

DELAY = 1.0
OPENALEX_EMAIL = "citeradar@example.com"

_inst_city_cache: dict[str, str] = {}


def _api_get(session: requests.Session, url: str, params: dict = {}) -> Optional[dict]:
    try:
        resp = session.get(url, params=params, timeout=12)
        if resp.status_code == 429:
            print("      [rate-limit] waiting 20 s…")
            time.sleep(20)
            resp = session.get(url, params=params, timeout=12)
            if resp.status_code == 429:
                raise RateLimitError(f"API rate limit while fetching {url}")
        if resp.status_code == 200:
            return resp.json()
    except RateLimitError:
        raise
    except requests.RequestException as e:
        raise RateLimitError(f"API request failed while fetching {url}: {e}") from e
    except Exception as e:
        print(f"      [error] {e}")
    return None


def _get_institution_city(inst_id: str, session: requests.Session) -> str:
    """
    Fetch city for an OpenAlex institution ID (cached).

    ``inst_id`` arrives as a web URL, e.g. ``https://openalex.org/I27837315``.
    This is converted to the API endpoint before the JSON response is fetched.
    """
    if not inst_id:
        return ""
    if inst_id in _inst_city_cache:
        return _inst_city_cache[inst_id]

    api_url = inst_id.replace(
        "https://openalex.org/",
        "https://api.openalex.org/institutions/",
    )
    try:
        resp = session.get(api_url, params={"mailto": OPENALEX_EMAIL}, timeout=12)
        if resp.status_code == 429:
            print("      [rate-limit] waiting 20 s…")
            time.sleep(20)
            resp = session.get(api_url, params={"mailto": OPENALEX_EMAIL}, timeout=12)
            if resp.status_code == 429:
                raise RateLimitError(f"API rate limit while fetching {api_url}")
        city = resp.json().get("geo", {}).get("city", "") if resp.status_code == 200 else ""
    except RateLimitError:
        raise
    except requests.RequestException as e:
        raise RateLimitError(f"API request failed while fetching {api_url}: {e}") from e
    except Exception:
        city = ""

    _inst_city_cache[inst_id] = city
    time.sleep(0.3)
    return city


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

def _title_match(query: str, candidate: str) -> float:
    a = set(re.findall(r"\w+", query.lower()))
    b = set(re.findall(r"\w+", candidate.lower()))
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


def _openalex_lookup(title: str, session: requests.Session) -> list[AuthorRecord]:
    """Search OpenAlex for a paper by title; return one record per author."""
    data = _api_get(session, "https://api.openalex.org/works", params={
        "search": title, "per_page": 1, "mailto": OPENALEX_EMAIL,
    })
    if not data:
        return []
    results = data.get("results", [])
    if not results:
        return []
    work     = results[0]
    oa_title = work.get("display_name", "") or work.get("title", "")
    if _title_match(title, oa_title) < 0.5:
        return []

    records: list[AuthorRecord] = []
    for authorship in work.get("authorships", []):
        author       = authorship.get("author", {})
        display_name = author.get("display_name", "")
        if not display_name or not _is_person(display_name):
            continue
        first, last = _split_name(display_name)

        institutions = authorship.get("institutions", [])
        institution  = ""
        city         = ""
        country_code = ""
        if institutions:
            inst         = institutions[0]
            institution  = inst.get("display_name", "")
            country_code = inst.get("country_code", "")
            city         = _get_institution_city(inst.get("id", ""), session)
        if not country_code:
            raw = authorship.get("countries", [])
            if raw:
                country_code = raw[0]

        records.append(AuthorRecord(
            full_name=display_name, first_name=first, last_name=last,
            institution=institution, city=city,
            country_code=country_code, country=_country_name(country_code),
            citing_paper_title="", citing_paper_year="", cited_paper_title="",
            data_source="openalex",
            openalex_author_id=author.get("id", ""),
        ))
    return records


def _semantic_scholar_lookup(title: str, session: requests.Session) -> list[AuthorRecord]:
    """Search Semantic Scholar for a paper by title."""
    data = _api_get(session, "https://api.semanticscholar.org/graph/v1/paper/search", params={
        "query": title, "limit": 1,
        "fields": "title,authors,authors.affiliations,year",
    })
    if not data:
        return []
    papers = data.get("data", [])
    if not papers or _title_match(title, papers[0].get("title", "")) < 0.5:
        return []

    records: list[AuthorRecord] = []
    for author in papers[0].get("authors", []):
        display_name = author.get("name", "")
        if not display_name:
            continue
        first, last = _split_name(display_name)
        affils      = author.get("affiliations", [])
        records.append(AuthorRecord(
            full_name=display_name, first_name=first, last_name=last,
            institution=affils[0] if affils else "",
            city="", country_code="", country="",
            citing_paper_title="", citing_paper_year="", cited_paper_title="",
            data_source="semantic_scholar", openalex_author_id="",
        ))
    return records


def _crossref_lookup(title: str, session: requests.Session) -> list[AuthorRecord]:
    """Query CrossRef for author full names (rarely has affiliations)."""
    data = _api_get(session, "https://api.crossref.org/works", params={
        "query.title": title, "rows": 1, "select": "title,author",
    })
    if not data:
        return []
    items = data.get("message", {}).get("items", [])
    if not items:
        return []
    item     = items[0]
    cr_title = " ".join(item.get("title", [""]))
    if _title_match(title, cr_title) < 0.5:
        return []

    records: list[AuthorRecord] = []
    for a in item.get("author", []):
        first = a.get("given",  "").strip()
        last  = a.get("family", "").strip()
        affil_list  = a.get("affiliation", [])
        institution = affil_list[0].get("name", "") if affil_list else ""
        records.append(AuthorRecord(
            full_name=f"{first} {last}".strip(), first_name=first, last_name=last,
            institution=institution, city="", country_code="", country="",
            citing_paper_title="", citing_paper_year="", cited_paper_title="",
            data_source="crossref", openalex_author_id="",
        ))
    return records


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _author_from_dict(data: dict) -> AuthorRecord:
    fields = AuthorRecord.__dataclass_fields__
    return AuthorRecord(**{k: data.get(k, "") for k in fields})


def _citation_key(citation: dict) -> str:
    return f"{citation.get('title', '')}|{citation.get('cited_paper_title', '')}"


def _load_author_checkpoint(json_path: str) -> tuple[list[AuthorRecord], list[str], list[str]]:
    if not json_path:
        return [], [], []
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return [], [], []

    records = [
        _author_from_dict(a)
        for a in data.get("authors", [])
        if isinstance(a, dict)
    ]
    not_found = data.get("papers_not_found", [])
    processed = data.get("processed_citations", [])
    if not processed:
        processed = [
            f"{r.citing_paper_title}|{r.cited_paper_title}"
            for r in records
            if r.citing_paper_title
        ]
    return records, not_found, processed


def build_author_profiles(citations_data: dict,
                           session: requests.Session,
                           json_path: str = "",
                           csv_path: str = "") -> tuple[list[AuthorRecord], list[str]]:
    """
    Resolve full author metadata for every unique citing paper.

    Parameters
    ----------
    citations_data : dict
        JSON object from :func:`tracker.save_citations` (contains ``"citations"`` list).
    session : requests.Session

    Returns
    -------
    all_records : list[AuthorRecord]
    not_found   : list[str]   — titles that could not be resolved
    """
    citations = citations_data.get("citations", [])

    # De-duplicate by (citing_title, cited_title)
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for c in citations:
        key = (c["title"], c["cited_paper_title"])
        if key not in seen:
            seen.add(key)
            unique.append(c)

    print(f"Unique citing-paper entries : {len(unique)}")
    print("Resolving authors via OpenAlex → Semantic Scholar → CrossRef\n")

    all_records, not_found, processed_citations = _load_author_checkpoint(json_path)
    processed = set(processed_citations)

    if processed:
        print(f"Resuming authors: {len(processed)} citing-paper entries already processed.\n")

    for i, paper in enumerate(unique, 1):
        title       = paper["title"]
        year        = paper.get("year", "")
        cited_title = paper["cited_paper_title"]
        key         = _citation_key(paper)

        if key in processed:
            print(f"[{i:>3}/{len(unique)}] {title[:80]}")
            print("        already processed — skipping")
            continue

        print(f"[{i:>3}/{len(unique)}] {title[:80]}")

        records = _openalex_lookup(title, session)
        time.sleep(DELAY)
        if not records:
            records = _semantic_scholar_lookup(title, session)
            time.sleep(DELAY)
        if not records:
            records = _crossref_lookup(title, session)
            time.sleep(DELAY)

        if records:
            print(f"        → {len(records)} authors  [{records[0].data_source}]")
            for r in records:
                r.citing_paper_title = title
                r.citing_paper_year  = year
                r.cited_paper_title  = cited_title
            all_records.extend(records)
        else:
            print("        → not found in any source")
            not_found.append(title)

        processed.add(key)
        processed_citations.append(key)
        if json_path and csv_path:
            save_author_profiles(all_records, not_found, json_path, csv_path,
                                 processed_citations, complete=False,
                                 quiet=True)

    if json_path and csv_path:
        save_author_profiles(all_records, not_found, json_path, csv_path,
                             processed_citations, complete=True)

    return all_records, not_found


def save_author_profiles(all_records: list[AuthorRecord], not_found: list[str],
                          json_path: str, csv_path: str,
                          processed_citations: Optional[list[str]] = None,
                          complete: bool = True,
                          quiet: bool = False) -> None:
    """Persist author profile data to JSON and CSV."""
    processed_citations = list(dict.fromkeys(processed_citations or []))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "total_author_records": len(all_records),
                "complete":             complete,
                "processed_citations":  processed_citations,
                "papers_not_found":     not_found,
                "authors":              [asdict(r) for r in all_records],
            },
            f, ensure_ascii=False, indent=2,
        )
    fields = list(AuthorRecord.__dataclass_fields__.keys())
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(r) for r in all_records)

    if quiet:
        print(f"        checkpoint saved → {json_path}")
    else:
        print(f"\nAuthor profiles saved → {json_path}, {csv_path}")
        country_counts = Counter(r.country or "Unknown" for r in all_records if r.country)
        print("\nTop countries:")
        for country, count in country_counts.most_common(10):
            print(f"  {count:>4}  {country}")
