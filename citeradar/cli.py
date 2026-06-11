"""
cli.py — CiteRadar Pipeline Orchestrator

Entry point for the ``citeradar`` command.  Runs all five pipeline stages in
sequence and writes every output file into a folder named after the researcher.

Usage
-----
::

    citeradar <SCHOLAR_USER_ID> [options]

    Options:
      --no-enrich   Skip CrossRef author enrichment (faster, fewer details)
      --no-hindex   Skip OpenAlex h-index look-up  (fastest, no rankings)
      --outdir DIR  Parent directory for the output folder (default: current dir)

Output folder structure
-----------------------
::

    <ResearcherName>/
    ├── summary.txt               # plain-text statistics
    ├── papers.csv                # researcher's own papers + citation counts
    ├── citing_papers.csv         # every paper that cited them, with author info
    ├── ranked_by_citations.csv   # citing researchers sorted by times-cited-you
    ├── ranked_by_citations.json
    ├── ranked_by_hindex.csv      # citing researchers sorted by h-index
    ├── ranked_by_hindex.json
    └── citation_map.html         # interactive Folium world map
"""

import argparse
import json
import os
import re
import sys
import requests

from .scraper  import scrape_profile, save_papers
from .tracker  import track_citations
from .profiler import build_author_profiles
from .ranker   import run_rankings
from .reporter import generate_report
from .errors   import RateLimitError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitise_name(name: str) -> str:
    """Convert a display name to a safe folder name."""
    name = name.strip()
    name = re.sub(r'[\\/*?:"<>|]', "", name)   # strip Windows-forbidden chars
    name = re.sub(r"\s+", "_", name)
    return name or "CiteRadar_Output"


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _p(folder: str, filename: str) -> str:
    """Return an absolute path inside the output folder."""
    return os.path.join(folder, filename)


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _find_existing_run(outdir: str, scholar_id: str) -> tuple[str, dict]:
    """Find the newest completed papers.json for this Scholar ID."""
    candidates: list[tuple[float, str, dict]] = []
    paths = [os.path.join(outdir, "papers.json")]

    if os.path.isdir(outdir):
        for name in os.listdir(outdir):
            paths.append(os.path.join(outdir, name, "papers.json"))

    for papers_json in paths:
        data = _load_json(papers_json)
        if data.get("scholar_id") != scholar_id or not data.get("complete", True):
            continue
        try:
            mtime = os.path.getmtime(papers_json)
        except OSError:
            continue
        candidates.append((mtime, os.path.dirname(papers_json), data))

    if not candidates:
        return "", {}
    _, folder, papers_data = max(candidates, key=lambda item: item[0])
    return folder, papers_data


def _is_complete(data: dict) -> bool:
    """Older artifacts had no marker; treat them as complete."""
    return bool(data) and data.get("complete", True)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(scholar_id: str, outdir: str,
                 enrich: bool, compute_hindex: bool) -> None:
    """
    Execute the full CiteRadar pipeline for a single Scholar profile.

    Parameters
    ----------
    scholar_id     : Google Scholar user ID (the ``?user=…`` parameter)
    outdir         : parent directory; the researcher's sub-folder is created here
    enrich         : whether to call CrossRef to complete truncated author lists
    compute_hindex : whether to run the OpenAlex h-index look-up stage
    """

    # ── Stage 1: scrape researcher's own papers ────────────────────────────
    print("\n" + "=" * 60)
    print("STAGE 1 — Scraping Google Scholar profile")
    print("=" * 60)

    folder, papers_data = _find_existing_run(outdir, scholar_id)
    if folder:
        author_info = papers_data.get("author", {})
        author_name = author_info.get("name", scholar_id)
        print(f"  Reusing completed papers.json from: {folder}")
    else:
        author_info, papers = scrape_profile(scholar_id)
        if not papers:
            print("[ERROR] No papers found.  The profile may be private or "
                  "Scholar returned a CAPTCHA.  Aborting.")
            sys.exit(1)

        author_name = author_info.get("name", scholar_id)
        folder      = os.path.join(outdir, _sanitise_name(author_name))
        _ensure_dir(folder)
        print(f"\nOutput folder: {folder}")

        papers_json = _p(folder, "papers.json")
        papers_csv  = _p(folder, "papers.csv")
        save_papers(author_info, papers, papers_json, papers_csv, scholar_id)
        papers_data = _load_json(papers_json)

    print(f"\nOutput folder: {folder}")

    # ── Stage 2: track citations ──────────────────────────────────────────
    print("\n" + "=" * 60)
    print("STAGE 2 — Tracking citations on Google Scholar")
    print("=" * 60)

    citations_json = _p(folder, "citations.json")
    citations_csv  = _p(folder, "citing_papers.csv")
    citations_data = _load_json(citations_json)

    if _is_complete(citations_data):
        print(f"  Reusing completed citations.json from: {folder}")
    else:
        if citations_data and citations_data.get("enrich") is not None:
            if citations_data.get("enrich") != enrich:
                print("[ERROR] Cannot resume citations with a different "
                      "--no-enrich setting. Rerun with the original option "
                      "or remove citations.json.")
                sys.exit(1)

        all_citing, summary = track_citations(
            papers_data, enrich=enrich, author_name=author_name,
            json_path=citations_json, csv_path=citations_csv,
        )
        citations_data = _load_json(citations_json)
        if not all_citing:
            print("[WARN] No citing papers found.  Stopping after Stage 2.")
            return

    if not citations_data.get("complete", True):
        print("[ERROR] citations.json is incomplete; rerun to resume Stage 2.")
        sys.exit(1)
    if not citations_data.get("citations"):
        print("[WARN] No citing papers found.  Stopping after Stage 2.")
        return

    # ── Stage 3: build author profiles ───────────────────────────────────
    print("\n" + "=" * 60)
    print("STAGE 3 — Resolving author metadata (OpenAlex / S2 / CrossRef)")
    print("=" * 60)

    authors_json = _p(folder, "authors.json")
    authors_csv  = _p(folder, "authors.csv")
    authors_data = _load_json(authors_json)

    if _is_complete(authors_data):
        print(f"  Reusing completed authors.json from: {folder}")
    else:
        session = requests.Session()
        all_records, not_found = build_author_profiles(
            citations_data, session, authors_json, authors_csv,
        )
        authors_data = _load_json(authors_json)

    if not authors_data.get("complete", True):
        print("[ERROR] authors.json is incomplete; rerun to resume Stage 3.")
        sys.exit(1)

    # ── Stage 4: rankings ─────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("STAGE 4 — Building author rankings")
    print("=" * 60)

    cit_json  = _p(folder, "ranked_by_citations.json")
    cit_csv   = _p(folder, "ranked_by_citations.csv")
    hidx_json = _p(folder, "ranked_by_hindex.json")
    hidx_csv  = _p(folder, "ranked_by_hindex.csv")

    if not compute_hindex:
        # Build citation ranking only; write empty h-index files
        from .ranker import build_citation_ranking, _save
        cit_ranks = build_citation_ranking(
            authors_data.get("authors", authors_data)
        )
        _save(cit_ranks, cit_json, cit_csv)
        print("[INFO] h-index ranking skipped (--no-hindex flag set).")
        hidx_json = hidx_csv = None
    else:
        run_rankings(authors_data, cit_json, cit_csv, hidx_json, hidx_csv)

    # ── Stage 5: summary + map ───────────────────────────────────────────
    print("\n" + "=" * 60)
    print("STAGE 5 — Generating summary report and world map")
    print("=" * 60)

    summary_path = _p(folder, "summary.txt")
    map_path     = _p(folder, "citation_map.html")
    generate_report(authors_data, author_name, summary_path, map_path)

    # ── Done ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  CiteRadar complete!")
    print(f"  All files saved to: {folder}")
    print("=" * 60)
    print(f"\n  papers.csv               — your papers + citation counts")
    print(f"  citing_papers.csv        — all papers that cited you")
    print(f"  ranked_by_citations.csv  — citing researchers (most citations first)")
    if hidx_csv:
        print(f"  ranked_by_hindex.csv     — citing researchers (highest h-index first)")
    print(f"  summary.txt              — statistics overview")
    print(f"  citation_map.html        — open in any browser\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="citeradar",
        description=(
            "CiteRadar: automated citation intelligence for Google Scholar profiles.\n"
            "Provide a Scholar user ID and receive a complete citation analysis\n"
            "including author profiles, rankings, and an interactive world map."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "scholar_id",
        help="Google Scholar user ID, e.g. i1H5XQ8AAAAJ",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip CrossRef author-name enrichment (faster but fewer complete author lists)",
    )
    parser.add_argument(
        "--no-hindex",
        action="store_true",
        help="Skip OpenAlex h-index look-up (much faster; citation-count ranking still produced)",
    )
    parser.add_argument(
        "--outdir",
        default=".",
        help="Parent directory for the output folder (default: current directory)",
    )

    args = parser.parse_args()

    try:
        run_pipeline(
            scholar_id     = args.scholar_id,
            outdir         = args.outdir,
            enrich         = not args.no_enrich,
            compute_hindex = not args.no_hindex,
        )
    except RateLimitError as e:
        print("\n[PAUSED] " + str(e))
        print("Progress saved. Rerun the same command later to resume.")
        sys.exit(2)


if __name__ == "__main__":
    main()
