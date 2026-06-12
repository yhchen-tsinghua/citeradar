# CiteRadar 🌍

**Automated Citation Intelligence for Google Scholar Profiles**

*CiteRadar: A Citation Intelligence Platform for Researcher
Profiling and Geographic Visualization.* [[Paper]](https://openreview.net/pdf?id=15TusV5Bkh)

[![PyPI version](https://badge.fury.io/py/citeradar.svg)](https://pypi.org/project/citeradar/)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

---

## What is CiteRadar?

Most citation tools only tell you **how many** people cited your work.
CiteRadar tells you **who** they are, **where** they are, and **how influential** they are.

Give CiteRadar your Google Scholar ID. It will automatically:

1. 📄 Scrape your complete publication list from Google Scholar
2. 🔍 Track every paper that has cited your work
3. 👤 Resolve full author profiles — name, institution, city, country — via OpenAlex, Semantic Scholar, and CrossRef
4. 📊 Rank citing researchers by **citation frequency** and **h-index**
5. 🗺️ Generate an **interactive HTML world map** showing where your readers are

All results are saved in a clean folder named after the researcher. **1 command, everything done.**

---

## Quick Start

```bash
pip install citeradar
citeradar YOUR_SCHOLAR_ID
```

Example:
```bash
citeradar i1H5XQ8AAAAJ
```

This creates a folder `Chenxu_Niu/` in your current directory with all outputs inside.

---

## Output

```
Chenxu_Niu/
├── summary.txt               # statistics: researchers, countries, institutions
├── papers.csv                # your own papers with citation counts
├── citing_papers.csv         # every paper that cited you + author info
├── ranked_by_citations.csv   # who cited you most (citation frequency ranking)
├── ranked_by_citations.json
├── ranked_by_hindex.csv      # most influential researchers who cited you (h-index ranking)
├── ranked_by_hindex.json
└── citation_map.html         # interactive world map — open in any browser
```

### Summary Report (`summary.txt`)
```
══════════════════════════════════════════════════════════
  CiteRadar — Citation Summary for Chenxu Niu
══════════════════════════════════════════════════════════
  Unique researchers who cited you : 134
  Countries                        : 11
  Institutions / affiliations      : 31
  Cities (with location data)      : 28

  Top Countries
  ────────────────────────────────────────
  United States                  86  ████████████████████
  South Korea                    12  ██
  China                           7  █
  ...
```

### Interactive World Map (`citation_map.html`)

![CiteRadar World Map](assets/citation_map_demo.png)

- **Heat-map layer** — shows global citation density at a glance
- **Circle markers** — one per city, sized and colored by researcher count
- **Click any circle** — popup shows researcher names and institutions
- **Fully self-contained** — one HTML file, no server needed, works offline

---

## Installation

```bash
pip install citeradar
```

**Requirements:** Python 3.9+

Dependencies are installed automatically: `requests[socks]`, `beautifulsoup4`, `folium`, `geopy`, `lxml`, `PyYAML`

---

## Usage

```bash
citeradar <SCHOLAR_ID> [options]
```

### Options

| Flag | Description |
|------|-------------|
| `--outdir DIR` | Save output folder to a specific directory (default: current directory) |
| `--no-enrich` | Skip CrossRef author enrichment (faster, slightly fewer complete author lists) |
| `--no-hindex` | Skip h-index lookup (much faster; citation-count ranking still produced) |
| `--proxy-config PATH` | YAML file defining rotating proxies for Requests-based API calls |

### Examples

```bash
# Full pipeline, save to Desktop
citeradar i1H5XQ8AAAAJ --outdir ~/Desktop

# Fast mode — no h-index lookup
citeradar i1H5XQ8AAAAJ --no-hindex

# Save to a specific folder
citeradar i1H5XQ8AAAAJ --outdir /path/to/my/research

# Rotate requests through proxies
citeradar i1H5XQ8AAAAJ --proxy-config proxies.yaml
```

Example proxy configuration:

```yaml
PROXY_ID_1:
  - http: "socks5h:127.0.0.1:1080"
    https: "socks5h:127.0.0.1:1080"
  - http: "socks5h:127.0.0.1:1081"
PROXY_ID_2:
  - https: "socks5h:127.0.0.1:2080"
```

CiteRadar rotates across top-level proxy IDs for Requests-based calls
(Google Scholar, CrossRef, OpenAlex, Semantic Scholar), and randomly chooses
one local endpoint within each proxy ID. Nominatim geocoding is not proxied.

You can also run it as a module:
```bash
python -m citeradar i1H5XQ8AAAAJ
```

---

## How It Works

```
Stage 1 — Scholar Scraper
  Fetches your full publication list from Google Scholar
  → papers.csv

Stage 2 — Citation Tracker
  Follows every "Cited by" link, paginates through all results
  Enriches truncated author lists via CrossRef API
  → citing_papers.csv

Stage 3 — Author Profiler
  For each citing paper, resolves full author metadata:
  OpenAlex (primary) → Semantic Scholar → CrossRef (fallback)
  Captures: full name, institution, city, country, OpenAlex author ID
  → authors.json

Stage 4 — Author Rankings
  Citation-count ranking  — groups by author, counts unique citing papers
  h-index ranking         — OpenAlex lookup with disambiguation guard
  → ranked_by_citations.*, ranked_by_hindex.*

Stage 5 — Reporter
  Computes aggregate statistics
  Geocodes cities via Nominatim (OpenStreetMap), cached per session
  Builds interactive Folium map with heat layer + circle markers
  → summary.txt, citation_map.html
```

---

## Author Disambiguation

Bibliometric databases sometimes merge different researchers who share the same name, leading to **wildly inflated h-index values**. We discovered this firsthand: "Wei Zhang" was initially assigned h=91 when the actual citing author has h≈10.

CiteRadar applies a **two-stage verification** before accepting any h-index:

**Stage 1 — Direct ID + affiliation cross-validation**
Uses the OpenAlex author ID captured during profiling, then confirms the author's affiliation history matches the institution we independently recorded. Uses stop-word-filtered word overlap (threshold ≥ 0.6) — so "Texas Tech University" and "University of Texas" are correctly identified as *different* institutions (common words like "university" and "of" are removed before comparison).

**Stage 2 — Name search + double-check**
When no ID is available, evaluates the top-5 name-search candidates. Requires name similarity ≥ 0.7 AND institution match, then applies the same affiliation cross-validation.

If an author's institution is unknown, only h-index ≤ 20 is accepted to prevent common-name misattribution.

---

## Rate Limits & Anti-Ban

| Source | Strategy |
|--------|----------|
| Google Scholar | Realistic Chrome User-Agent; 2s delay between requests; 30s back-off on HTTP 429 |
| OpenAlex | `mailto=` polite-pool; 1s delay; 20s back-off on 429 |
| CrossRef | Proper User-Agent with contact email; used only when needed |
| Optional proxies | Rotate Requests-based calls across configured proxy IDs |
| Nominatim | 1.1s between requests (OSM policy); module-level cache |

---

## Limitations

1. **Google Scholar CAPTCHA** — Scholar may block requests if too many are made in a short time. If this happens, wait a few hours or switch to a different network, then rerun.
2. **OpenAlex coverage** — Not all papers are indexed. ~15–25% of citing papers may fall back to Semantic Scholar or CrossRef depending on your research domain.
3. **Author disambiguation** — The two-stage verification greatly reduces false positives but cannot guarantee perfect results for very common names.
4. **Geocoding** — City coordinates come from OpenAlex institution records via Nominatim. ~60% of authors have city-level data; the rest are shown in the rankings but not on the map.

---


## Citation

If you use CiteRadar in your research, please cite:

```bibtex
@software{citeradar2025,
  title   = {CiteRadar: Automated Citation Intelligence for Google Scholar Profiles},
  author  = {Niu, Chenxu},
  year    = {2025},
  url     = {https://github.com/chenxuniu/citeradar},
  note    = {PyPI: https://pypi.org/project/citeradar/}
}
```

---

## Changelog

| Version | Date | Notes |
|---------|------|-------|
| **v1.0.0** | Apr 2025 | Initial release — full 5-stage pipeline, PyPI publish |

---

## License

MIT License — free to use, modify, and distribute.

---

*Built with assistance from Claude (Anthropic).*
