"""
ranker.py — Author Rankings

Produces two ranked files from the author-profile data:

1. **Ranked by citation count** — which researchers have cited your work most
   frequently (across all your papers).

2. **Ranked by h-index** — which high-impact researchers have cited you,
   using h-index data fetched from OpenAlex.

Author-disambiguation strategy
-------------------------------
OpenAlex occasionally *merges* different researchers who share the same name
into a single entity, leading to inflated h-index values.  We apply a
two-stage verification before accepting any h-index:

Stage 1 — Direct ID lookup with affiliation cross-validation
    When an ``openalex_author_id`` is already known (carried from the
    profiler), we fetch that specific author entity and confirm that *at
    least one* affiliation in their OpenAlex history matches the institution
    we independently recorded, using stop-word-filtered word overlap
    (threshold ≥ 0.6).  Stop words (university, of, the, …) are stripped
    so that "Texas Tech University" and "University of Texas" are correctly
    identified as *different* institutions rather than scoring 0.5+ overlap.

Stage 2 — Name-search fallback with strict double-check
    If no ID is available, we search by name (top-5 candidates), require
    name similarity ≥ 0.7 AND institution similarity ≥ 0.4, then apply the
    same affiliation cross-validation from Stage 1.

Conservative fall-back
    If the institution field is unknown, we only accept h-index values ≤ 20
    to avoid falsely attributing a famous researcher's h-index to an
    unverifiable common name.
"""

import json
import time
import csv
import re
from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Optional
import requests

from .errors import RateLimitError
from .proxy import make_session


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class CitationRank:
    rank: int
    full_name: str
    first_name: str
    last_name: str
    institution: str
    city: str
    country: str
    times_cited_you: int
    your_papers_cited: str     # "|"-separated list of YOUR papers they cited
    their_citing_papers: str   # "|"-separated list of papers that cite you
    openalex_author_id: str


@dataclass
class HIndexRank:
    rank: int
    full_name: str
    first_name: str
    last_name: str
    institution: str
    city: str
    country: str
    h_index: int
    total_papers: int
    total_citations: int
    times_cited_you: int
    your_papers_cited: str
    openalex_id: str


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

OPENALEX_EMAIL = "citeradar@example.com"
DELAY = 1.0


def _api_get(session: requests.Session, url: str, params: dict = {}) -> Optional[dict]:
    try:
        resp = session.get(url, params=params, timeout=12)
        if resp.status_code == 429:
            print("    [rate-limit] waiting 20 s…")
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
        print(f"    [error] {e}")
    return None


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------

def _name_similarity(a: str, b: str) -> float:
    wa = set(re.findall(r"\w+", a.lower()))
    wb = set(re.findall(r"\w+", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


# Common words that carry no discriminating information in institution names.
_INST_STOP = {
    "university", "institute", "college", "school", "department",
    "of", "the", "and", "for", "in", "at", "national", "center",
    "centre", "lab", "laboratory", "research", "technology",
}


def _inst_similarity(a: str, b: str) -> float:
    """
    Word-overlap similarity after stripping institution stop-words.

    Example::
        "Texas Tech University" → {texas, tech}
        "University of Texas"   → {texas}
        overlap = 1/2 = 0.5   (correctly LOW — different institution)
    """
    wa = set(re.findall(r"\w+", a.lower())) - _INST_STOP
    wb = set(re.findall(r"\w+", b.lower())) - _INST_STOP
    if not wa or not wb:
        return _name_similarity(a, b)
    return len(wa & wb) / max(len(wa), len(wb))


# ---------------------------------------------------------------------------
# Institution cross-validation
# ---------------------------------------------------------------------------

def _institution_confirmed(known_institution: str, oa_record: dict) -> bool:
    """
    Verify that the OpenAlex author record's affiliation history includes
    the institution we independently recorded.

    Two failure modes:

    A) ``known_institution`` is empty → cannot verify identity.
       Only accept if h-index ≤ 20 (low mis-attribution risk).

    B) Institution known but does not appear in OpenAlex affiliation history
       (stop-word-filtered similarity < 0.6) → reject.
       This prevents "Texas Tech" from matching "University of Texas".
    """
    h = (oa_record.get("summary_stats") or {}).get("h_index", 0) or 0

    if not known_institution:
        return h <= 20

    primary_inst = known_institution.split(",")[0].strip()
    all_aff_names = [
        aff.get("institution", {}).get("display_name", "")
        for aff in oa_record.get("affiliations", [])
    ]
    all_aff_names = [n for n in all_aff_names if n]

    if not all_aff_names:
        return h <= 20

    for aff_name in all_aff_names:
        if _inst_similarity(primary_inst, aff_name) >= 0.6:
            return True
    return False


# ---------------------------------------------------------------------------
# h-index look-up
# ---------------------------------------------------------------------------

def _parse_oa_author(record: dict) -> tuple[int, int, int, str]:
    """Extract (h_index, works_count, cited_by_count, oa_id) from OpenAlex."""
    stats = record.get("summary_stats", {})
    return (
        stats.get("h_index", 0) or 0,
        record.get("works_count", 0) or 0,
        record.get("cited_by_count", 0) or 0,
        record.get("id", ""),
    )


def _lookup_hindex(full_name: str, institution: str, openalex_author_id: str,
                   session: requests.Session) -> tuple[int, int, int, str]:
    """
    Return ``(h_index, works_count, cited_by_count, openalex_id)`` for an author.

    Strategy:

    1. **Direct ID lookup** (when ``openalex_author_id`` is known) followed by
       affiliation cross-validation.
    2. **Name-search fallback** with strict name + institution requirements.

    Returns sentinel strings ``"id_mismatch"`` or ``"name_mismatch"`` in the
    ``openalex_id`` position when disambiguation fails.
    """
    # Stage 1: direct ID lookup + cross-validation
    if openalex_author_id:
        data = _api_get(session, openalex_author_id, params={"mailto": OPENALEX_EMAIL})
        if data and data.get("id"):
            if _institution_confirmed(institution, data):
                return _parse_oa_author(data)
            return 0, 0, 0, "id_mismatch"

    # Stage 2: name search fallback
    data = _api_get(session, "https://api.openalex.org/authors", params={
        "search": full_name, "per_page": 5, "mailto": OPENALEX_EMAIL,
    })
    if not data:
        return 0, 0, 0, ""

    best        = None
    best_score  = -1.0
    for c in data.get("results", []):
        name_score = _name_similarity(full_name, c.get("display_name", ""))
        if name_score < 0.7:
            continue
        inst_score = 0.0
        if institution:
            for aff in c.get("affiliations", []):
                aff_name = aff.get("institution", {}).get("display_name", "")
                s = _name_similarity(institution, aff_name)
                if s > inst_score:
                    inst_score = s
        if name_score < 1.0 and inst_score < 0.4 and institution:
            continue
        total = name_score + inst_score * 0.5
        if total > best_score:
            best_score = total
            best = c

    if best is None:
        return 0, 0, 0, ""
    if not _institution_confirmed(institution, best):
        return 0, 0, 0, "name_mismatch"
    return _parse_oa_author(best)


# ---------------------------------------------------------------------------
# Ranking builders
# ---------------------------------------------------------------------------

def build_citation_ranking(authors: list[dict]) -> list[CitationRank]:
    """
    Group author records by name and rank by number of unique citing papers.

    Each author may appear multiple times in ``authors`` if they co-authored
    several papers that each cite one of your works.  We count how many of
    your papers they cited (unique ``cited_paper_title``) and how many of
    their own papers that includes (unique ``citing_paper_title``).
    """
    agg: dict[str, dict] = defaultdict(lambda: {
        "first_name": "", "last_name": "", "institution": "",
        "city": "", "country": "", "openalex_author_id": "",
        "citing_papers": set(), "cited_your_papers": set(),
    })

    for r in authors:
        name = r["full_name"].strip()
        if not name:
            continue
        d = agg[name]
        if not d["institution"] and r.get("institution"):
            d.update(institution=r["institution"],
                     city=r.get("city", ""), country=r.get("country", ""))
        if not d["first_name"] and r.get("first_name"):
            d.update(first_name=r["first_name"], last_name=r.get("last_name", ""))
        if not d["openalex_author_id"] and r.get("openalex_author_id"):
            d["openalex_author_id"] = r["openalex_author_id"]
        d["citing_papers"].add(r["citing_paper_title"])
        d["cited_your_papers"].add(r["cited_paper_title"])

    sorted_names = sorted(agg.items(), key=lambda kv: (-len(kv[1]["citing_papers"]), kv[0]))

    records: list[CitationRank] = []
    for rank, (name, d) in enumerate(sorted_names, 1):
        records.append(CitationRank(
            rank=rank, full_name=name,
            first_name=d["first_name"], last_name=d["last_name"],
            institution=d["institution"], city=d["city"], country=d["country"],
            times_cited_you=len(d["citing_papers"]),
            your_papers_cited=" | ".join(sorted(d["cited_your_papers"])),
            their_citing_papers=" | ".join(sorted(d["citing_papers"])),
            openalex_author_id=d["openalex_author_id"],
        ))
    return records


def build_hindex_ranking(citation_ranks: list[CitationRank],
                          session: requests.Session) -> list[HIndexRank]:
    """
    For every unique author, look up their h-index via OpenAlex and sort
    descending.  Authors for whom h-index cannot be verified are retained
    with h_index = 0 and placed at the bottom of the list.
    """
    results: list[HIndexRank] = []

    for i, cr in enumerate(citation_ranks, 1):
        method = "ID" if cr.openalex_author_id else "name-search"
        print(f"  [{i:>3}/{len(citation_ranks)}] {cr.full_name[:50]}  [{method}]")

        h, papers, cites, oa_id = _lookup_hindex(
            cr.full_name, cr.institution, cr.openalex_author_id, session,
        )

        if h > 0:
            src = "✓ direct" if cr.openalex_author_id else "~ name-search"
            print(f"          h={h}  papers={papers}  cited={cites}  [{src}]")
        elif oa_id == "id_mismatch":
            print("          ✗ institution mismatch — ID rejected")
        elif oa_id == "name_mismatch":
            print("          ✗ institution mismatch — name-search rejected")
        else:
            print("          not found / unverifiable")

        results.append(HIndexRank(
            rank=0, full_name=cr.full_name,
            first_name=cr.first_name, last_name=cr.last_name,
            institution=cr.institution, city=cr.city, country=cr.country,
            h_index=h, total_papers=papers, total_citations=cites,
            times_cited_you=cr.times_cited_you,
            your_papers_cited=cr.your_papers_cited,
            openalex_id=oa_id,
        ))
        time.sleep(DELAY)

    results.sort(key=lambda r: (-r.h_index, r.full_name))
    for rank, r in enumerate(results, 1):
        r.rank = rank
    return results


# ---------------------------------------------------------------------------
# Save helpers
# ---------------------------------------------------------------------------

def _save(records: list, json_path: str, csv_path: str) -> None:
    data = [asdict(r) for r in records]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if data:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(data[0].keys()))
            writer.writeheader()
            writer.writerows(data)
    print(f"  Saved → {json_path}  +  {csv_path}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_rankings(authors_data: dict,
                 cit_json: str, cit_csv: str,
                 hidx_json: str, hidx_csv: str,
                 proxy_pool=None) -> tuple[list[CitationRank], list[HIndexRank]]:
    """
    Build both rankings from the author-profile JSON produced by the profiler.

    Parameters
    ----------
    authors_data : dict  — JSON object with ``"authors"`` list
    cit_json / cit_csv   — output paths for citation-count ranking
    hidx_json / hidx_csv — output paths for h-index ranking

    Returns
    -------
    cit_ranks, hindex_ranks
    """
    authors = authors_data.get("authors", authors_data) if isinstance(authors_data, dict) else authors_data

    print("=" * 60)
    print("Building citation-count ranking…")
    cit_ranks = build_citation_ranking(authors)
    print(f"\nTop 10 by times cited you:")
    for r in cit_ranks[:10]:
        print(f"  #{r.rank:<3} {r.full_name:<28} cited you {r.times_cited_you}x"
              f"  [{r.institution or '—'}  {r.city or ''}, {r.country or '?'}]")
    _save(cit_ranks, cit_json, cit_csv)

    print("\n" + "=" * 60)
    print("Looking up h-index via OpenAlex…\n")
    session      = make_session(proxy_pool)
    hindex_ranks = build_hindex_ranking(cit_ranks, session)
    print(f"\nTop 10 by h-index:")
    for r in hindex_ranks[:10]:
        print(f"  #{r.rank:<3} h={r.h_index:<4} {r.full_name:<28}"
              f"  [{r.institution or '—'}]  (cited you {r.times_cited_you}x)")
    _save(hindex_ranks, hidx_json, hidx_csv)

    return cit_ranks, hindex_ranks
