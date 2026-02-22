#!/usr/bin/env python3
"""
Generate data/explore.html — an interactive corpus explorer with three views:
  1. Timeline   — decade histogram; click bar → item list
  2. Map         — publication place dots (needs 'place' field in inventory)
  3. List        — filterable card grid

Reads: data/inventory.json
Writes: data/explore.html

Usage:
    python scripts/generate_explore.py
    python scripts/generate_explore.py --inventory data/inventory.json --output data/explore.html
"""
import sys, json, argparse, time
from pathlib import Path
from collections import Counter

_ROOT = Path(__file__).parent.parent

# ── City → [lat, lng] lookup for map dots ─────────────────────────────────────
# Covers the main academic publication centres for Islamic studies / history of
# science.  Keys are normalised lower-case with common variants.

CITY_COORDS = {
    # Middle East
    'cairo': [30.06, 31.25], 'al-qahira': [30.06, 31.25], 'egypt': [26.8, 30.8],
    'beirut': [33.89, 35.50], 'damascus': [33.51, 36.29], 'baghdad': [33.34, 44.40],
    'istanbul': [41.01, 28.98], 'ankara': [39.93, 32.86],
    'tehran': [35.69, 51.39], 'téhéran': [35.69, 51.39],
    'riyadh': [24.69, 46.72],
    'jerusalem': [31.78, 35.22], 'amman': [31.96, 35.95],
    'tunis': [36.82, 10.17], 'tūnis': [36.82, 10.17],
    'kirman': [30.28, 57.08], 'kerman': [30.28, 57.08],
    # Central / South Asia
    'tashkent': [41.30, 69.24],
    'calcutta': [22.57, 88.36], 'kolkata': [22.57, 88.36],
    'delhi': [28.63, 77.22], 'new delhi': [28.63, 77.22],
    'bombay': [19.08, 72.88], 'mumbai': [19.08, 72.88],
    'hyderabad': [17.38, 78.49], 'lahore': [31.56, 74.34],
    'aligarh': [27.88, 78.08],
    # Europe
    'london': [51.51, -0.13], 'oxford': [51.75, -1.26], 'cambridge': [52.20, 0.12],
    'edinburgh': [55.95, -3.19],
    'paris': [48.86, 2.35], 'strasbourg': [48.57, 7.75], 'nanterre': [48.90, 2.21],
    'leiden': [52.16, 4.49], 'amsterdam': [52.37, 4.90],
    'berlin': [52.52, 13.40], 'frankfurt': [50.11, 8.68],
    'munich': [48.14, 11.58], 'heidelberg': [49.40, 8.69],
    'hamburg': [53.55, 9.99], 'göttingen': [51.53, 9.93], 'gottingen': [51.53, 9.93],
    'leipzig': [51.34, 12.38], 'bonn': [50.73, 7.10],
    'stuttgart': [48.78, 9.18],
    'tübingen': [48.52, 9.06], 'tubingen': [48.52, 9.06],
    'vienna': [48.21, 16.37], 'graz': [47.07, 15.44],
    'rome': [41.90, 12.50], 'naples': [40.85, 14.27], 'napoli': [40.85, 14.27],
    'florence': [43.77, 11.26],
    'madrid': [40.42, -3.70], 'barcelona': [41.39, 2.15],
    'st. petersburg': [59.94, 30.32], 'moscow': [55.76, 37.62],
    'budapest': [47.50, 19.04], 'warsaw': [52.23, 21.01],
    'copenhagen': [55.68, 12.57], 'københavn': [55.68, 12.57], 'kobenhavn': [55.68, 12.57],
    'vienne': [45.52, 4.87],  # Vienne, France (distinct from Vienna)
    'turnhout': [51.32, 4.94],
    # North America
    'new york': [40.71, -74.01], 'new york city': [40.71, -74.01],
    'chicago': [41.88, -87.63], 'boston': [42.36, -71.06],
    'princeton': [40.36, -74.66], 'washington': [38.91, -77.04],
    'los angeles': [34.05, -118.24], 'berkeley': [37.87, -122.27],
    'ann arbor': [42.28, -83.74], 'philadelphia': [39.95, -75.17],
    'austin': [30.27, -97.74], 'durham': [35.99, -78.90],
    # Other
    'toronto': [43.65, -79.38], 'montreal': [45.51, -73.55],
}


def geocode(place: str) -> list | None:
    """Return [lat, lng] for a place string, or None if not found.

    Handles common Zotero place formats:
    - "London"               → direct match
    - "London ; New York"   → takes first city before semicolon
    - "Cambridge, MA"        → strips comma/suffix, tries "cambridge"
    - "Oxford, [England]"   → strips brackets, tries "oxford"
    - "Téhéran Louvain …"  → tries first token, then last, then first-two
    """
    if not place:
        return None
    # Take only the first city when semicolons separate multiple places
    raw = place.split(';')[0].strip()
    k = raw.lower()
    if k in CITY_COORDS:
        return CITY_COORDS[k]
    # Normalise: drop commas, brackets, parentheses then re-split
    k_clean = k.replace(',', ' ').replace('[', ' ').replace(']', ' ') \
               .replace('(', ' ').replace(')', ' ')
    parts = [p for p in k_clean.split() if p]
    if not parts:
        return None
    candidates = [
        parts[0],
        parts[-1],
        ' '.join(parts[:2]) if len(parts) >= 2 else '',
    ]
    for p in candidates:
        if p and p in CITY_COORDS:
            return CITY_COORDS[p]
    return None


def build_decade_data(inventory: list) -> list:
    """Return [{decade, count, keys:[…]}] for ALL decades from corpus min to max (step 10).

    Empty decades are included with count=0 so the timeline shows a true picture of
    temporal distribution without gaps.
    """
    buckets: dict[int, list] = {}
    for r in inventory:
        y = r.get('year', '')
        try:
            yr  = int(str(y)[:4])
            dec = (yr // 10) * 10
            buckets.setdefault(dec, []).append(r)
        except (ValueError, TypeError):
            pass
    if not buckets:
        return []
    min_dec = min(buckets)
    max_dec = max(buckets)
    result = []
    for dec in range(min_dec, max_dec + 10, 10):
        items = buckets.get(dec, [])
        result.append({'decade': dec, 'count': len(items),
                       'keys': [i['key'] for i in items]})
    return result


def build_html(inventory: list, data_dir: Path = None, corpus_name: str = 'My Research Library',
               collection_slug: str = '') -> str:
    # ── Pre-compute derived data for JS ───────────────────────────────────────
    # Tag items that already have a generated reader HTML
    if data_dir is None:
        data_dir = _ROOT / 'data'
    # Tag items whose pages/ directory exists (new SPA reader)
    texts_root  = data_dir / 'texts'
    reader_keys = {d.name for d in texts_root.iterdir()
                   if d.is_dir() and (d / 'pages').is_dir()
                   and any((d / 'pages').glob('*.jpg'))} \
                  if texts_root.is_dir() else set()
    for r in inventory:
        r['has_reader'] = r['key'] in reader_keys

    decades = build_decade_data(inventory)

    # Map data: items with known place
    map_items = []
    for r in inventory:
        coords = geocode(r.get('place', ''))
        if coords:
            map_items.append({
                'key':    r['key'],
                'title':  r['title'],
                'year':   r.get('year', ''),
                'place':  r.get('place', ''),
                'lat':    coords[0],
                'lng':    coords[1],
                'pdf':    r.get('pdf_status', ''),
                'type':   r.get('doc_type', ''),
                'lang':   r.get('language') or 'unknown',
            })

    # Stats
    total         = len(inventory)
    with_place    = sum(1 for r in inventory if r.get('place'))
    with_year     = sum(1 for r in inventory if r.get('year'))
    num_pdfs      = sum(1 for r in inventory
                        if r.get('pdf_status') in ('stored', 'downloaded', 'stored_embedded'))
    num_extracted = sum(1 for r in inventory if r.get('extracted'))
    num_readers   = sum(1 for r in inventory if r.get('has_reader'))

    data_js      = json.dumps(inventory,  ensure_ascii=False)
    decades_js   = json.dumps(decades,    ensure_ascii=False)
    map_items_js = json.dumps(map_items,  ensure_ascii=False)

    # Nav links differ when generating a standalone file inside collections/{slug}/
    # vs the root-level data/explore.html.
    nav_prefix  = '../../' if collection_slug else ''
    coll_qs     = f'?collection={collection_slug}' if collection_slug else ''
    reader_qs   = f'&collection={collection_slug}' if collection_slug else ''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{corpus_name} — Explorer</title>

<!-- Fonts -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@600&display=swap" rel="stylesheet">
<!-- Leaflet for the map view -->
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

:root {{
  --bg:      #111;
  --panel:   #1a1a1a;
  --border:  #2e2e2e;
  --text:    #d8d8d8;
  --muted:   #666;
  --accent:  #0071e3;
  --link:    #5ba3f5;
  --good:    #2d9f5e;
  --warn:    #c49e3b;
  --bad:     #b34040;
}}

/* Global link colour — readable on dark backgrounds */
a {{ color: var(--link); text-decoration: none; }}
a:hover {{ color: #7cc0ff; text-decoration: underline; }}

body {{
  font-family: system-ui, -apple-system, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}}

/* ── Header ── */
header {{
  background: #0a0a0a;
  border-bottom: 1px solid var(--border);
  padding: 14px 24px;
  display: flex;
  align-items: center;
  gap: 20px;
  flex-shrink: 0;
}}
header h1 {{ font-size: 16px; font-weight: 600; color: #fff; }}
header .sub {{ font-size: 12px; color: var(--muted); flex: 1; }}
.site-nav {{ display: flex; gap: 12px; margin-left: auto; }}
.site-nav a {{
  font-size: 12px; color: var(--muted); text-decoration: none;
  padding: 4px 10px; border-radius: 6px; border: 1px solid var(--border);
  transition: color .15s, border-color .15s;
}}
.site-nav a:hover {{ color: #fff; border-color: #555; }}
.site-nav a.active {{ color: #fff; border-color: var(--accent); }}

/* ── Tabs ── */
.tabs {{
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--border);
  background: #0d0d0d;
  padding: 0 24px;
  flex-shrink: 0;
}}
.tab {{
  padding: 10px 20px;
  font-size: 13px;
  cursor: pointer;
  color: var(--muted);
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  transition: all .15s;
  user-select: none;
}}
.tab.active {{ color: #fff; border-bottom-color: var(--accent); }}
.tab:hover:not(.active) {{ color: var(--text); }}

/* ── Filters bar ── */
.filters {{
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  padding: 10px 24px;
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
  flex-shrink: 0;
}}
.filters input[type=text] {{
  background: #222;
  border: 1px solid #3a3a3a;
  color: var(--text);
  padding: 5px 10px;
  border-radius: 5px;
  font-size: 12px;
  width: 200px;
}}
.filters input[type=text]:focus {{ outline: none; border-color: var(--accent); }}
.filters select {{
  background: #222;
  border: 1px solid #3a3a3a;
  color: var(--text);
  padding: 5px 8px;
  border-radius: 5px;
  font-size: 12px;
}}
.filters label {{ font-size: 12px; color: var(--muted); display: flex; gap: 6px; align-items: center; }}
.filter-count {{ font-size: 11px; color: var(--muted); margin-left: auto; }}

/* ── Panels ── */
.panel {{ flex: 1; overflow: hidden; display: none; }}
.panel.active {{ display: flex; flex-direction: column; }}

/* ── Timeline panel ── */
.timeline-wrap {{
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}}

.chart-area {{
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 20px 20px 12px;
}}

.chart-title {{
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 16px;
  text-transform: uppercase;
  letter-spacing: .05em;
}}

.histogram {{
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: 160px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 4px;
}}

.bar-wrap {{
  display: flex;
  flex-direction: column;
  align-items: center;
  cursor: pointer;
  flex: 1;
  min-width: 0;
}}
.bar {{
  background: var(--accent);
  border-radius: 2px 2px 0 0;
  width: 100%;
  transition: filter .15s;
}}
.bar-wrap:hover .bar {{ filter: brightness(1.3); }}
.bar-wrap.selected .bar {{ background: var(--warn); }}
.bar-label {{
  font-size: 9px;
  color: var(--muted);
  margin-top: 4px;
  white-space: nowrap;
  transform: rotate(-40deg);
  transform-origin: top left;
  margin-left: 6px;
}}

.chart-legend {{
  display: flex;
  gap: 16px;
  margin-top: 16px;
  font-size: 11px;
  color: var(--muted);
}}

/* ── Timeline item list (shown on bar click) ── */
.tl-results {{
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}}
.tl-results-header {{
  padding: 10px 16px;
  font-size: 12px;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
  background: #161616;
  display: flex;
  align-items: center;
  gap: 8px;
}}
.tl-item {{
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: 10px;
  align-items: baseline;
}}
.tl-item:last-child {{ border-bottom: none; }}
.tl-item:hover {{ background: #1f1f1f; }}
.tl-year {{ font-size: 11px; color: var(--muted); min-width: 40px; }}
.tl-title {{ font-size: 13px; color: var(--text); flex: 1; }}
.tl-authors {{ font-size: 11px; color: var(--muted); }}

/* ── Map panel ── */
#map-panel-wrap {{
  flex: 1;
  display: flex;
  min-height: 0;
}}
#map-panel {{
  flex: 1;
  position: relative;
  min-height: 0;
}}
#leaflet-map {{
  position: absolute;
  inset: 0;
  background: #1a2233;
}}
.map-notice {{
  position: absolute;
  bottom: 20px;
  left: 50%;
  transform: translateX(-50%);
  background: rgba(0,0,0,.75);
  color: var(--text);
  font-size: 12px;
  padding: 8px 16px;
  border-radius: 6px;
  z-index: 1000;
  pointer-events: none;
}}
/* Map results side panel */
#map-results {{
  width: 320px;
  background: var(--panel);
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}}
#map-results-header {{
  padding: 10px 16px;
  font-size: 12px;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
  background: #161616;
  flex-shrink: 0;
}}
#map-results-body {{
  flex: 1;
  overflow-y: auto;
}}

/* ── List panel ── */
.list-wrap {{
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
}}
.card-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
}}
.card {{
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  transition: border-color .15s;
}}
.card:hover {{ border-color: #444; }}
.card-title {{
  font-size: 13px;
  font-weight: 500;
  color: #fff;
  line-height: 1.4;
}}
.card-title a {{ color: inherit; text-decoration: none; }}
.card-title a:hover {{ color: var(--accent); }}
.card-pub {{
  font-size: 11px;
  font-style: italic;
  color: #888;
  margin-top: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.card-meta {{
  font-size: 11px;
  color: var(--muted);
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}}
.card-badges {{ display: flex; gap: 4px; flex-wrap: wrap; }}

/* ── List toolbar ── */
.list-toolbar {{
  display: flex; align-items: center; gap: 12px; flex-shrink: 0;
  padding: 8px 16px; background: #111; border-bottom: 1px solid var(--border);
}}
.list-sort-group {{ display: flex; align-items: center; gap: 4px; flex: 1; flex-wrap: wrap; }}
.list-sort-label {{ font-size: 11px; color: var(--muted); margin-right: 4px; }}
.sort-btn, .view-btn {{
  font-size: 11px; padding: 3px 9px; border-radius: 5px; cursor: pointer;
  background: #1e1e1e; border: 1px solid var(--border); color: var(--muted);
  transition: color .15s, border-color .15s, background .15s;
}}
.sort-btn:hover, .view-btn:hover {{ color: #fff; border-color: #555; }}
.sort-btn.active {{ color: #fff; border-color: var(--accent); background: #1a2a3a; }}
#sortDirBtn {{
  font-size: 13px; padding: 2px 7px; border-radius: 5px; cursor: pointer;
  background: #1e1e1e; border: 1px solid var(--border); color: var(--muted);
}}
#sortDirBtn:hover {{ color: #fff; }}
.view-toggle {{ display: flex; gap: 4px; }}
.view-btn.active {{ color: #fff; border-color: var(--accent); background: #1a2a3a; }}
.list-count {{ font-size: 11px; color: var(--muted); white-space: nowrap; }}

/* ── Row view ── */
.row-list {{ display: flex; flex-direction: column; gap: 0; }}
.row-item {{
  display: grid;
  grid-template-columns: 1fr 140px 80px 90px;
  gap: 0 12px; align-items: start;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border);
  transition: background .1s;
}}
.row-item:hover {{ background: #181818; }}
.row-title {{ font-size: 13px; font-weight: 500; color: #fff; line-height: 1.3; }}
.row-title a {{ color: inherit; text-decoration: none; }}
.row-title a:hover {{ color: var(--accent); }}
.row-pub {{ font-size: 11px; font-style: italic; color: #888; margin-top: 2px; }}
.row-author {{ font-size: 11px; color: var(--muted); }}
.row-year  {{ font-size: 11px; color: var(--muted); text-align: right; }}
.row-place {{ font-size: 11px; color: var(--muted); }}

/* ── Badges ── */
.badge {{
  font-size: 10px;
  padding: 2px 7px;
  border-radius: 10px;
  white-space: nowrap;
}}
.b-stored    {{ background:#1a3a1a; color:#5fba7d; border:1px solid #2a5a2a; }}
.b-url       {{ background:#1a2a3a; color:#5a9fd4; border:1px solid #1a4a6a; }}
.b-missing   {{ background:#2a1a1a; color:#d45a5a; border:1px solid #4a1a1a; }}
.b-embedded  {{ background:#1a1a3a; color:#8888dd; border:1px solid #2a2a5a; }}
.b-scanned   {{ background:#2a1a2a; color:#cc88cc; border:1px solid #4a1a4a; }}
.b-itype     {{ background:#222;    color:#999;    border:1px solid #333; }}
.b-lang      {{ background:#1a2a2a; color:#66aaaa; border:1px solid #1a4a4a; }}
.b-good      {{ background:#0d2a1a; color:#5fba7d; border:1px solid #1a4a2a; }}
.b-suspect   {{ background:#2a2000; color:#d4b05a; border:1px solid #4a3800; }}
.b-garbled   {{ background:#2a1a1a; color:#d47a5a; border:1px solid #4a2a1a; }}
.b-oa        {{ background:#0d200d; color:#5fba7d; border:1px solid #1a4a1a; }}
.b-paywalled {{ background:#2a1a00; color:#d4a05a; border:1px solid #4a3000; }}
.b-unavail   {{ background:#1e1e1e; color:#555;    border:1px solid #2e2e2e; }}

/* ── Scrollbars ── */
::-webkit-scrollbar {{ width: 5px; height: 5px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{ background: #333; border-radius: 3px; }}

/* ── Scholion brand ── */
.scholion-logo {{
  font-family: 'Cormorant Garamond', Georgia, serif;
  font-size: 20px; font-weight: 600; color: #fff;
  letter-spacing: .04em; text-decoration: none; line-height: 1; flex-shrink: 0;
}}
.scholion-logo:hover {{ color: #ccc; }}
.header-divider {{ color: #444; font-size: 14px; flex-shrink: 0; }}
</style>
</head>
<body>

<header>
  <a href="{nav_prefix}index.html" class="scholion-logo">Scholion</a>
  <span class="header-divider">·</span>
  <h1>{corpus_name} — Explorer</h1>
  <span class="sub">
    {total} items
    {f' · {num_pdfs} PDFs' if num_pdfs else ''}
    {f' · {num_extracted} extracted' if num_extracted else ''}
    {f' · {num_readers} readers' if num_readers else ''}
    · {with_year} dated · {with_place} placed
  </span>
  <nav class="site-nav">
    <a href="{nav_prefix}dashboard.html{coll_qs}">📋 Dashboard</a>
    <a href="{nav_prefix}explore.html{coll_qs}" class="active">🔭 Explorer</a>
  </nav>
</header>

<!-- ── Tabs ── -->
<div class="tabs">
  <div class="tab"        onclick="switchTab('timeline')">📅 Timeline</div>
  <div class="tab"        onclick="switchTab('map')">🗺 Map</div>
  <div class="tab active" onclick="switchTab('list')">📚 List</div>
</div>

<!-- ── Filters ── -->
<div class="filters">
  <input type="text" id="q" placeholder="Search title, author, key…" oninput="applyFilters()">
  <label>Type:
    <select id="fType" onchange="applyFilters()">
      <option value="">All types</option>
      <option value="book">Book</option>
      <option value="journalArticle">Journal article</option>
      <option value="bookSection">Book section</option>
      <option value="encyclopediaArticle">Encyclopedia</option>
      <option value="manuscript">Manuscript</option>
    </select>
  </label>
  <label>PDF:
    <select id="fPdf" onchange="applyFilters()">
      <option value="">All</option>
      <option value="stored">In Zotero</option>
      <option value="downloaded">Downloaded</option>
      <option value="url_only">URL only</option>
      <option value="no_attachment">None</option>
    </select>
  </label>
  <label>Doc:
    <select id="fDoc" onchange="applyFilters()">
      <option value="">All</option>
      <option value="embedded">Embedded</option>
      <option value="scanned">Scanned</option>
    </select>
  </label>
  <label>Lang:
    <select id="fLang" onchange="applyFilters()">
      <option value="">All languages</option>
      <option value="en">English</option>
      <option value="ar">Arabic</option>
      <option value="de">German</option>
      <option value="fr">French</option>
    </select>
  </label>
  <label>Period:
    <select id="fPeriod" onchange="applyFilters()">
      <option value="">All periods</option>
      <option value="ancient">Ancient / Medieval (–1500)</option>
      <option value="early_modern">Early Modern (1500–1800)</option>
      <option value="19c">19th Century (1800–1900)</option>
      <option value="20c">20th Century (1900–2000)</option>
      <option value="modern">21st Century (2000–)</option>
    </select>
  </label>
  <label>Avail:
    <select id="fAvail" onchange="applyFilters()">
      <option value="">All</option>
      <option value="stored">Stored</option>
      <option value="open_access">Open access</option>
      <option value="restricted">Paywalled</option>
      <option value="unavailable">Unavailable</option>
    </select>
  </label>
  <span class="filter-count" id="filterCount"></span>
</div>

<!-- ── TIMELINE PANEL ── -->
<div class="panel" id="panel-timeline">
  <div class="timeline-wrap">
    <div class="chart-area">
      <div class="chart-title">Publications by decade</div>
      <div class="histogram" id="histogram"></div>
      <div class="chart-legend">
        <span>Click a bar to see items from that decade</span>
        <span id="tlTotal"></span>
      </div>
    </div>
    <div class="tl-results" id="tl-results" style="display:none">
      <div class="tl-results-header" id="tl-results-header"></div>
      <div id="tl-results-body"></div>
    </div>
  </div>
</div>

<!-- ── MAP PANEL ── -->
<div class="panel" id="panel-map">
  <div id="map-panel-wrap">
    <div id="map-panel">
      <div id="leaflet-map"></div>
      <div class="map-notice" id="map-notice"></div>
    </div>
    <div id="map-results" style="display:none">
      <div id="map-results-header">Click a location to see publications</div>
      <div id="map-results-body"></div>
    </div>
  </div>
</div>

<!-- ── LIST PANEL ── -->
<div class="panel active" id="panel-list">
  <div class="list-toolbar">
    <div class="list-sort-group">
      <span class="list-sort-label">Sort:</span>
      <button class="sort-btn active" data-sort="title"    onclick="setListSort('title')">Title</button>
      <button class="sort-btn"        data-sort="authors"  onclick="setListSort('authors')">Author</button>
      <button class="sort-btn"        data-sort="year"     onclick="setListSort('year')">Year</button>
      <button class="sort-btn"        data-sort="pub_title" onclick="setListSort('pub_title')">Publication</button>
      <button class="sort-btn"        data-sort="place"    onclick="setListSort('place')">Place</button>
      <button id="sortDirBtn" onclick="toggleListSortDir()" title="Reverse sort">↑</button>
    </div>
    <div class="view-toggle">
      <button id="viewCardBtn" class="view-btn active" onclick="setListView('cards')" title="Card view">⊞</button>
      <button id="viewRowBtn"  class="view-btn"        onclick="setListView('rows')"  title="Row view">☰</button>
    </div>
    <span class="list-count" id="listCount"></span>
  </div>
  <div class="list-wrap">
    <div class="card-grid" id="card-grid"></div>
  </div>
</div>

<script>
// ─── Data ────────────────────────────────────────────────────────────────────
const DATA       = {data_js};
const DECADES    = {decades_js};
const MAP_ITEMS  = {map_items_js};

// ─── State ───────────────────────────────────────────────────────────────────
let filtered  = DATA.slice();   // currently filtered items
let activeTab = 'list';         // default to list view
let selectedDecade = null;
let leafletMap = null;
let tlSort = 'year';            // sort field for decade item list: 'year'|'title'|'authors'
let tlSortAsc = true;          // sort direction; false = descending
let listSort    = 'title';      // list-panel sort field
let listSortAsc = true;         // list-panel sort direction
let listView    = 'cards';      // 'cards' | 'rows'

// ─── Utilities ───────────────────────────────────────────────────────────────
function esc(s) {{
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

/** Return the best link for an item: reader > URL > null */
function itemHref(r) {{
  if (r.has_reader) return `{nav_prefix}reader.html?key=${{r.key}}{reader_qs}`;
  if (r.url)        return r.url;
  return null;
}}

function itemLink(r, text, extraAttrs) {{
  const href = itemHref(r);
  if (!href) return esc(text);
  const target = r.has_reader ? '' : ' target="_blank" rel="noopener"';
  const title  = r.has_reader ? ' title="Open reader"' : '';
  return `<a href="${{esc(href)}}"${{target}}${{title}}${{extraAttrs||''}}>${{esc(text)}}</a>`;
}}

function periodOf(year) {{
  const y = parseInt(year);
  if (isNaN(y)) return '';
  if (y < 1500) return 'ancient';
  if (y < 1800) return 'early_modern';
  if (y < 1900) return '19c';
  if (y < 2000) return '20c';
  return 'modern';
}}

function badge(cls, text) {{
  return `<span class="badge ${{cls}}">${{esc(text)}}</span>`;
}}

function pdfBadge(s) {{
  if (!s) return '';
  if (s === 'stored')       return badge('b-stored', '✓ Stored');
  if (s === 'downloaded')   return badge('b-stored', '⬇ Downloaded');
  if (s === 'url_only')     return badge('b-url',    'URL only');
  return badge('b-missing', s);
}}

function typeBadge(s) {{
  if (s === 'embedded') return badge('b-embedded', 'Embedded');
  if (s === 'scanned')  return badge('b-scanned',  'Scanned');
  return '';
}}

function qualityBadge(s) {{
  if (s === 'good')    return badge('b-good',    '✓ good');
  if (s === 'suspect') return badge('b-suspect', '⚠ suspect');
  if (s === 'garbled') return badge('b-garbled', '✗ garbled');
  return '';
}}

function availabilityBadge(s) {{
  // Skip badge if already covered by pdfBadge (stored) or not yet scanned
  if (!s || s === 'stored') return '';
  if (s === 'open_access')  return badge('b-oa',         'Open access');
  if (s === 'restricted' || s === 'unknown')  return badge('b-paywalled', 'Paywalled');
  if (s === 'unavailable')  return badge('b-unavail',    'Unavailable');
  return '';
}}

// ─── Filtering ───────────────────────────────────────────────────────────────
function applyFilters() {{
  const q       = document.getElementById('q').value.toLowerCase();
  const fType   = document.getElementById('fType').value;
  const fPdf    = document.getElementById('fPdf').value;
  const fDoc    = document.getElementById('fDoc').value;
  const fLang   = document.getElementById('fLang').value;
  const fPeriod = document.getElementById('fPeriod').value;
  const fAvail  = document.getElementById('fAvail').value;

  filtered = DATA.filter(r => {{
    if (q && !JSON.stringify(r).toLowerCase().includes(q)) return false;
    if (fType   && r.item_type  !== fType)   return false;
    if (fPdf    && r.pdf_status !== fPdf)    return false;
    if (fDoc    && r.doc_type   !== fDoc)    return false;
    if (fLang   && r.language   !== fLang)   return false;
    if (fPeriod && periodOf(r.year) !== fPeriod) return false;
    if (fAvail) {{
      const match = r.availability === fAvail ||
                    (fAvail === 'restricted' && r.availability === 'unknown');
      if (!match) return false;
    }}
    return true;
  }});

  document.getElementById('filterCount').textContent =
    `${{filtered.length}} / ${{DATA.length}} items`;

  refreshActiveView();
}}

function refreshActiveView() {{
  if (activeTab === 'timeline') renderTimeline();
  if (activeTab === 'map')      renderMap();
  if (activeTab === 'list')     renderList();
}}

// ─── Tab switching ────────────────────────────────────────────────────────────
function switchTab(name) {{
  activeTab = name;
  document.querySelectorAll('.tab').forEach(t => {{
    const fn = t.getAttribute('onclick') || '';
    t.classList.toggle('active', fn.includes("'" + name + "'"));
  }});
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  // Leaflet must be told to recalculate its size after the container becomes visible
  if (name === 'map' && leafletMap) {{
    setTimeout(() => leafletMap.invalidateSize(), 50);
  }}
  refreshActiveView();
}}

// ─── Timeline ─────────────────────────────────────────────────────────────────
function renderTimeline() {{
  const filteredKeys = new Set(filtered.map(r => r.key));

  if (!DECADES.length) {{
    document.getElementById('histogram').innerHTML =
      '<em style="color:#555;font-size:12px">No dated items in corpus.</em>';
    document.getElementById('tl-results').style.display = 'none';
    return;
  }}

  // All decades (full range incl. empty) with active-item counts for current filter
  const allDec = DECADES.map(d => ({{
    ...d,
    active: d.keys.filter(k => filteredKeys.has(k)),
  }}));

  // Scale bars against the tallest active bar (min 1 avoids divide-by-zero)
  const maxCount = Math.max(1, ...allDec.map(d => d.active.length));
  const hist = document.getElementById('histogram');
  const HEIGHT = 152;

  hist.innerHTML = allDec.map(d => {{
    const n    = d.active.length;
    const h    = n > 0 ? Math.max(4, Math.round(n / maxCount * HEIGHT)) : 1;
    const sel  = selectedDecade === d.decade ? ' selected' : '';
    // Only label century marks — every 10 decades is too cramped
    const showLbl = d.decade % 100 === 0;
    const lbl     = showLbl ? String(d.decade) : '\xa0';
    // Empty bars are rendered dimly so the gaps in the distribution are visible
    const style = n === 0
      ? `height:1px;background:#2a2a2a;`
      : `height:${{h}}px;`;
    return `<div class="bar-wrap${{sel}}" onclick="selectDecade(${{d.decade}})"
              title="${{n}} item${{n !== 1 ? 's' : ''}} · ${{d.decade}}s">
      <div class="bar" style="${{style}}"></div>
      <div class="bar-label" style="${{showLbl ? '' : 'opacity:0'}}">${{lbl}}</div>
    </div>`;
  }}).join('');

  const total    = allDec.reduce((s, d) => s + d.active.length, 0);
  const nonEmpty = allDec.filter(d => d.active.length > 0).length;
  document.getElementById('tlTotal').textContent =
    `${{total}} items · ${{nonEmpty}} of ${{allDec.length}} decades`;

  // Show/hide decade-item list depending on selection state
  if (selectedDecade !== null) {{
    showDecadeItems(selectedDecade, filteredKeys);
  }} else {{
    document.getElementById('tl-results').style.display = 'none';
  }}
}}

function selectDecade(dec) {{
  selectedDecade = (selectedDecade === dec) ? null : dec;
  renderTimeline();
}}

function sortItems(items) {{
  const dir = tlSortAsc ? 1 : -1;
  return items.slice().sort((a, b) => {{
    let cmp;
    if (tlSort === 'title')   cmp = (a.title   || '').localeCompare(b.title   || '');
    else if (tlSort === 'authors') cmp = (a.authors || '').localeCompare(b.authors || '');
    else cmp = (a.year || '').localeCompare(b.year || '');
    return cmp * dir;
  }});
}}

function setTlSort(s) {{
  if (tlSort === s) {{
    tlSortAsc = !tlSortAsc;  // same field → reverse direction
  }} else {{
    tlSort = s;
    tlSortAsc = true;          // new field → reset to ascending
  }}
  renderTimeline();
}}

function showDecadeItems(dec, filteredKeys) {{
  const decData = DECADES.find(d => d.decade === dec);

  // Gather items from this decade that pass the current filter
  const raw = decData
    ? decData.keys
        .filter(k => filteredKeys.has(k))
        .map(k => DATA.find(r => r.key === k))
        .filter(Boolean)
    : [];
  const itemsInDec = sortItems(raw);

  document.getElementById('tl-results').style.display = 'block';

  // Header with sort buttons (active one shows direction arrow)
  const sortBtn = (key, label) => {{
    const isActive = tlSort === key;
    const arrow    = isActive ? (tlSortAsc ? ' ↑' : ' ↓') : '';
    const activeStyle = isActive
      ? 'background:#0071e3;border-color:#0071e3;color:#fff;'
      : '';
    return `<button onclick="setTlSort('${{key}}')"
      style="${{activeStyle}}background-blend-mode:normal;border:1px solid #444;color:${{isActive?'#fff':'#aaa'}};
             padding:2px 10px;border-radius:4px;font-size:11px;cursor:pointer;
             transition:all .15s;background:${{isActive?'#0071e3':'#222'}}">${{label}}${{arrow}}</button>`;
  }};
  document.getElementById('tl-results-header').innerHTML =
    `<span>${{dec}}s — ${{itemsInDec.length}} item${{itemsInDec.length !== 1 ? 's' : ''}}</span>
     <span style="margin-left:auto;display:flex;gap:5px;align-items:center">
       <span style="font-size:10px;opacity:.5">Sort:</span>
       ${{sortBtn('year','Year')}} ${{sortBtn('title','Title')}} ${{sortBtn('authors','Author')}}
     </span>`;

  if (!itemsInDec.length) {{
    const msg = (!decData || !decData.keys.length)
      ? 'No items published in this decade.'
      : 'No items in this decade match the current filters.';
    document.getElementById('tl-results-body').innerHTML =
      `<div style="padding:16px;color:#555;font-size:13px">${{msg}}</div>`;
    return;
  }}

  document.getElementById('tl-results-body').innerHTML = itemsInDec.map(r => {{
    const readerIcon = r.has_reader ? ' 📖' : '';
    return `<div class="tl-item">
      <span class="tl-year">${{esc(r.year || '?')}}</span>
      <span class="tl-title">${{itemLink(r, r.title + readerIcon)}}</span>
      <span class="tl-authors">${{esc((r.authors||'').split(';')[0])}}</span>
    </div>`;
  }}).join('');
}}

// ─── Map ──────────────────────────────────────────────────────────────────────
function renderMap() {{
  if (!leafletMap) {{
    leafletMap = L.map('leaflet-map', {{
      center: [30, 20],
      zoom:   2,
      zoomControl: true,
    }});
    L.tileLayer(
      'https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',
      {{
        attribution: '&copy; <a href="https://carto.com/">CARTO</a>',
        subdomains: 'abcd',
        maxZoom: 19,
      }}
    ).addTo(leafletMap);
    // Force size recalc — the panel may have been invisible when the map was created
    setTimeout(() => leafletMap.invalidateSize(), 100);
  }}

  // Clear existing markers
  leafletMap.eachLayer(layer => {{
    if (layer instanceof L.CircleMarker) leafletMap.removeLayer(layer);
  }});

  const filteredKeys = new Set(filtered.map(r => r.key));
  const visible = MAP_ITEMS.filter(m => filteredKeys.has(m.key));

  const notice = document.getElementById('map-notice');

  if (MAP_ITEMS.length === 0) {{
    notice.textContent = 'Run scripts/03_inventory.py to populate place-of-publication data.';
    notice.style.display = 'block';
  }} else {{
    notice.textContent = `${{visible.length}} items with known place · ${{MAP_ITEMS.length}} total geocoded`;
    notice.style.display = 'block';
  }}

  // Group by location (lat/lng rounded to 1dp)
  const groups = {{}};
  visible.forEach(m => {{
    const gk = `${{m.lat.toFixed(1)}},${{m.lng.toFixed(1)}}`;
    (groups[gk] = groups[gk] || {{ lat: m.lat, lng: m.lng, items: [] }}).items.push(m);
  }});

  Object.values(groups).forEach(g => {{
    const r = Math.min(18, 5 + g.items.length * 1.5);
    const placeName = g.items[0].place;
    L.circleMarker([g.lat, g.lng], {{
      radius: r,
      color:        '#0071e3',
      fillColor:    '#0071e3',
      fillOpacity:  0.6,
      weight:       1,
    }}).on('click', () => showMapPlace(placeName, g.items))
      .addTo(leafletMap);
  }});
}}

/** Show publications for a clicked map location in the side panel. */
function showMapPlace(placeName, mapItems) {{
  // Resolve to full DATA records so we get has_reader, url, etc.
  const raw = mapItems
    .map(m => DATA.find(r => r.key === m.key))
    .filter(Boolean);
  const items = sortItems(raw);

  const panel  = document.getElementById('map-results');
  const header = document.getElementById('map-results-header');
  const body   = document.getElementById('map-results-body');

  panel.style.display = 'flex';

  header.innerHTML =
    `<b>${{esc(placeName)}}</b> — ${{items.length}} item${{items.length !== 1 ? 's' : ''}}
     <button onclick="document.getElementById('map-results').style.display='none';
                      if(leafletMap) setTimeout(()=>leafletMap.invalidateSize(),50);"
       style="margin-left:auto;background:none;border:none;color:#666;cursor:pointer;
              font-size:14px;line-height:1" title="Close">✕</button>`;

  if (!items.length) {{
    body.innerHTML = '<div style="padding:16px;color:#555;font-size:13px">No items match current filters.</div>';
  }} else {{
    body.innerHTML = items.map(r => {{
      const readerIcon = r.has_reader ? ' 📖' : '';
      return `<div class="tl-item">
        <span class="tl-year">${{esc(r.year || '?')}}</span>
        <span class="tl-title">${{itemLink(r, r.title + readerIcon)}}</span>
        <span class="tl-authors">${{esc((r.authors||'').split(';')[0])}}</span>
      </div>`;
    }}).join('');
  }}

  // The map panel shrank — tell Leaflet to recalculate
  if (leafletMap) setTimeout(() => leafletMap.invalidateSize(), 50);
}}

// ─── List sort / view controls ────────────────────────────────────────────────
function setListSort(field) {{
  if (listSort === field) {{ listSortAsc = !listSortAsc; }}
  else {{ listSort = field; listSortAsc = true; }}
  document.querySelectorAll('.sort-btn').forEach(b => {{
    b.classList.toggle('active', b.dataset.sort === field);
  }});
  document.getElementById('sortDirBtn').textContent = listSortAsc ? '↑' : '↓';
  renderList();
}}

function toggleListSortDir() {{
  listSortAsc = !listSortAsc;
  document.getElementById('sortDirBtn').textContent = listSortAsc ? '↑' : '↓';
  renderList();
}}

function setListView(view) {{
  listView = view;
  document.getElementById('viewCardBtn').classList.toggle('active', view === 'cards');
  document.getElementById('viewRowBtn' ).classList.toggle('active', view === 'rows');
  renderList();
}}

// ─── List ─────────────────────────────────────────────────────────────────────
function renderList() {{
  const grid = document.getElementById('card-grid');

  // Sort
  const sorted = filtered.slice().sort((a, b) => {{
    const av = (a[listSort] || '').toString().toLowerCase();
    const bv = (b[listSort] || '').toString().toLowerCase();
    return listSortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
  }});

  const countEl = document.getElementById('listCount');
  if (countEl) countEl.textContent = sorted.length + ' items';

  if (!sorted.length) {{
    grid.innerHTML = '<p style="color:var(--muted);font-size:13px">No items match current filters.</p>';
    return;
  }}

  if (listView === 'rows') {{
    grid.className = 'row-list';
    grid.innerHTML = sorted.map(r => {{
      const titleHtml = itemLink(r, r.title);
      const pubHtml   = r.pub_title ? `<div class="row-pub">${{esc(r.pub_title)}}</div>` : '';
      return `<div class="row-item">
        <div><div class="row-title">${{titleHtml}}${{r.has_reader ? ' 📖' : ''}}</div>${{pubHtml}}</div>
        <div class="row-author">${{esc((r.authors||'').split(';')[0])}}</div>
        <div class="row-year">${{esc(r.year||'—')}}</div>
        <div class="row-place">${{esc(r.place||'—')}}</div>
      </div>`;
    }}).join('');
  }} else {{
    grid.className = 'card-grid';
    grid.innerHTML = sorted.map(r => {{
      const readerBadge = r.has_reader
        ? `<span class="badge b-stored" title="Reader available">📖 Reader</span>`
        : '';
      const titleHtml = itemLink(r, r.title);
      const pubHtml  = r.pub_title ? `<div class="card-pub">${{esc(r.pub_title)}}</div>` : '';
      const meta = [(r.authors||'').split(';')[0], r.year, r.place].filter(Boolean).join(' · ');
      const tq   = r.text_quality;
      return `<div class="card">
        <div class="card-title">${{titleHtml}}</div>
        ${{pubHtml}}
        <div class="card-meta">${{esc(meta)}}</div>
        <div class="card-badges">
          ${{pdfBadge(r.pdf_status)}}
          ${{availabilityBadge(r.availability)}}
          ${{typeBadge(r.doc_type)}}
          ${{r.language ? badge('b-lang', r.language) : ''}}
          ${{badge('b-itype', r.item_type || '')}}
          ${{tq ? qualityBadge(tq) : ''}}
          ${{readerBadge}}
        </div>
      </div>`;
    }}).join('');
  }}
}}

// ─── Init ─────────────────────────────────────────────────────────────────────

// Empty-state: show setup prompt when there are no items yet
if (DATA.length === 0) {{
  document.querySelector('.tabs').style.display = 'none';
  document.querySelector('.toolbar').style.display = 'none';
  document.getElementById('content').innerHTML = `
    <div style="text-align:center; padding:80px 24px; color:#888;">
      <h2 style="font-size:22px; color:#fff; margin-bottom:16px; font-weight:600;">
        Your library is empty
      </h2>
      <p style="margin-bottom:8px; max-width:480px; margin-left:auto; margin-right:auto; line-height:1.6;">
        No items have been imported yet. Run the <strong>First-time setup</strong>
        workflow to connect your Zotero library and import your bibliography.
      </p>
      <p style="margin-bottom:32px; max-width:480px; margin-left:auto; margin-right:auto;
          font-size:13px; color:#555;">
        Go to the <strong>Actions</strong> tab in your GitHub repository →
        <strong>First-time setup</strong> → <strong>Run workflow</strong>
      </p>
      <a href="../../actions" target="_top"
         style="background:#0071e3; color:#fff; padding:12px 28px;
                border-radius:8px; text-decoration:none; font-size:14px; font-weight:600;
                display:inline-block;">
        Open Actions tab
      </a>
    </div>`;
}} else {{
  applyFilters();
}}
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inventory', default='data/inventory.json')
    parser.add_argument('--output',    default='data/explore.html')
    args = parser.parse_args()

    inv_path = _ROOT / args.inventory
    out_path = _ROOT / args.output

    print(f"Reading {inv_path}…")
    inventory = json.loads(inv_path.read_text(encoding='utf-8'))
    print(f"  {len(inventory)} items")

    # Load corpus name from config
    config_path = inv_path.parent / 'corpus_config.json'
    corpus_name = 'My Research Library'
    if config_path.exists():
        try:
            corpus_name = json.loads(config_path.read_text(encoding='utf-8')).get('name', corpus_name)
        except Exception:
            pass

    print("Building explore.html…")
    html = build_html(inventory, data_dir=inv_path.parent, corpus_name=corpus_name)
    out_path.write_text(html, encoding='utf-8')

    size_kb = out_path.stat().st_size // 1024
    print(f"✓ Saved → {out_path}  ({size_kb} KB)")

    import subprocess
    subprocess.run(['open', str(out_path)], check=False)


if __name__ == '__main__':
    main()
