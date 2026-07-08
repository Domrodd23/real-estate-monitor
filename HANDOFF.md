# real-estate-monitor — project handoff

A free, self-hosted Reventure/Zillow-style real estate dashboard built entirely
from public data. **Fully built, deployed, and auto-refreshing monthly.** This
doc is a condensed baton-pass so a new session can continue without relearning.

## Where everything lives
- **Local code:** `~/real-estate-monitor`
- **GitHub (public):** https://github.com/Domrodd23/real-estate-monitor  (user: `Domrodd23`)
- **Live site:** https://domrodd23.github.io/real-estate-monitor/
- **Live map:** https://domrodd23.github.io/real-estate-monitor/map.html
- **Auto-memory** (loads automatically for sessions in `/Users/khanhvu`):
  `~/.claude/projects/-Users-khanhvu/memory/project_real-estate-monitor.md` — read it too.

## What's built and working (all done)
- **Pipeline:** `fetch.py` → `compute.py` → `build.py` (orchestrated by `run.py`);
  shared code in `src/remon/`. Config-driven via **`config.yaml`** (edit ZIPs/markets there).
- **Data sources (free):** Zillow ZHVI/ZORI (ZIP + county), Redfin county tracker,
  FRED (mortgage + FHFA), Census ACS (income/pop/migration). Keys in `.env` (git-ignored)
  and GitHub Actions secrets. Both `FRED_API_KEY` + `CENSUS_API_KEY` are valid.
- **Dashboard (`docs/index.html`):** per-ZIP metrics + charts, sortable multi-market
  comparison table, cash-flow projector + reverse-offer calculator (client-side JS,
  math independently verified), PDF + Excel exports, transparent 2-method forecasts.
- **National map (`docs/map.html`):** MapLibre + PMTiles vector tiles — see "open problem".
- **Automation:** `.github/workflows/monthly.yml` runs on the 5th monthly + manual,
  refreshes data, commits `/docs`, Pages republishes. Verified green.
- **Markets:** Toledo (9 ZIPs) + San Francisco (23 ZIPs). Change in `config.yaml`.

## The map UX problem: RESOLVED 2026-07-01 (full redesign, locally verified, NOT yet deployed)
The map was rebuilt to Zillow/Reventure interaction standards (designed via a 3-lens
design panel + adversarial multi-agent review; spec archived in the session scratchpad):
- **App-shell layout** — full-bleed map, slim topbar, floating control/legend cards,
  the old footnote paragraph now lives in an ⓘ popover on the legend.
- **Search box + "my markets" chips** (Toledo, San Francisco) backed by a static local
  index `docs/geo_index.json` (~6k counties/ZIPs/aliases, built by `mapview.build_geo_index`
  — no geocoding API, works offline). Keyboard navigable.
- **Click = select + stats panel** (desktop card / mobile bottom sheet): all metrics,
  "higher than N% of US counties" percentile lines (vigintiles ride in `metrics_json`),
  TRACKED badge, "Zoom here" + "ZIP detail →" buttons. No camera movement on click.
- **Instant hover highlight** (feature-state via `promoteId:'cname'`), legend value tick,
  single tooltip; **county→ZIP cross-fade** at z5.6–6.6 instead of the hard z6 snap.
- **Camera pack** — fitBounds US framing on load, eased distance-scaled flights, maxBounds,
  calmer wheel zoom, grab/grabbing/pointer cursors.
- **Plain-English metric pills + one-line explainers + "No data" legend swatch**;
  clickable legend range filter (quartile zones, open-ended ends, auto-clears on switch).
- **Loading veil, first-run coach mark, mobile layout** (bottom sheet, horizontal pill
  scroll; the mobile media query also catches short landscape viewports ≤430px tall).

Verified end-to-end in a real browser (desktop 1280px + mobile 375px), all interactions.

## Reventure-parity upgrade 2026-07-03 (user asked to match reventure.app data depth)
Built on top of the redesign after the user shared Reventure screenshots:
- **12 metrics** (was 6), grouped Reventure-style in a collapsible accordion selector
  (built client-side from `metrics_json`; groups: Popular / Price trends / Rental &
  investor / Demographics). New: 5-yr & 1-mo price change, % below peak, 1-yr rent
  change, gross rent yield, **estimated cap rate** — computed in `mapview.py` with a
  transparent expense model (state property-tax table + 0.5% insurance + 20% of rent
  for vacancy/upkeep; constants at top of mapview.py). All new metrics exist at BOTH
  county and ZIP level; ZIP income/population come from a new **Census ACS ZCTA fetch**
  (`census._fetch_acs_zcta`, ~33k ZIPs, cached like the others).
- **On-map value labels** (the "43606 · 5.7%" side-by-side view): symbol layers fed by
  a client-side GeoJSON built from `geo_index.json` (which now carries centroids `c` and
  values `v` per entry). County labels z5+, ZIP labels take over at z6.6 — exactly where
  the fill cross-fade completes and the legend flips scale.
- **Per-level color scales**: ZIP layers get their own quantile ramp (`zexpr`/`zlegend`
  in metrics_json) so ZIPs don't saturate against the national county scale; the legend
  shows a "county scale"/"ZIP scale" tag and re-renders on zoom crossing 6.6.
- **Investor breakdown tooltip** (Reventure's black card): hovering on cap rate/yield/
  rent/price-to-rent shows Gross rent income / Expenses (est.) / Net rent income /
  Home value, from the `exp_est` tile property.
- Tiles re-baked with all 13 properties (counties.pmtiles ~7.3MB, zips.pmtiles ~8.2MB).

All of it multi-agent reviewed (2 rounds: 13 findings fixed in round 1, 4 in round 2 —
incl. a stale-zip-file resurrection path and a 10x label-rebuild slowdown), and verified
live in the browser.

## Migration + guru-metrics upgrade 2026-07-04..06 (research-driven, user asked for migration + "what the gurus use")
A 5-researcher web workflow verified free sources; 8 more metrics landed (20 total, 6 groups):
- **Migration & growth**: `inbound_movers_pct` (ACS B07001 mobility, county+ZIP, 5-yr avg,
  masked when denominator <500), `net_dom_mig_rate` (Census PEP county rate/1k —
  `census._fetch_pep`, public CSV, fetched OUTSIDE the API-key gate), `agi_net_percap`
  (IRS SOI county in/outflow AGI — new `sources/irs.py`; totals rows only (pseudo-FIPS 96),
  one-sided/suppressed counties stay NA, per-capita on native-vintage population).
- **Market heat**: `days_on_market`, `price_cut_share`, `inventory_yoy` + `listings`
  tooltip extra (new `sources/realtor.py` — Realtor.com core metrics; fetch the S3 URLs
  directly, NEVER the research page (403s bots); <10 active listings masked at BOTH levels).
- `home_value_fc_12m` (Zillow ZHVF ZIP-only forecast — no county file exists; `zip_only`
  render flag), `value_income_ratio` (computed; desc labels the mixed vintages).
- **CT/AK FIPS crosswalk** (`LEGACY_FIPS_XWALK`): post-2022 Census/IRS vintages key CT on
  planning regions; rate/share metrics fill legacy counties from their successor region.
- County-only metrics hide the ZIP layer (opacity 0) instead of blanketing tracked states
  gray; county labels then persist at all zooms and carry values (COUNTY_LABEL_KEYS).
- **Versioned data URLs** (`?v=<build stamp>` on pmtiles/geo_index): stale browser
  byte-range caches against re-baked tiles corrupt reads — do not remove.
- ACS bumped to 2024 (config.yaml adds B07001 mobility variables; both ACS fetchers pick
  them up automatically). Attribution line added to the ⓘ popover (Realtor.com® required).
- Review round 3: 12 confirmed findings fixed across the two phases of this upgrade.
  The js-ui review dimension never completed (3 session-limit interruptions) — its hot
  spots were covered by other reviewers + manual checks, but a fresh js-ui pass on
  map.html.j2 is a reasonable belt-and-braces follow-up.

## Colors + coverage + sale metrics (2026-07-06)
- Palettes → blue→cream→red SEQ / red→cream→green DIV (`PALETTE_*`), `NO_DATA_COLOR` #d4d4d4.
- ZIP drill-down now config-driven (`output.map_zip_states`) — OH/CA/IN/TX/NV, 5,857 ZIPs.
- Redfin sale-side county metrics (`redfin.load_national_sale_metrics`): actual sale price,
  sold-above-list %, months of supply; <10-sales masked; name-join with VA/MD-city
  normalization fallback (1,977/3,072 joined ≈ 98% of joinable).
- Legend syncs on zoomend/moveend/idle (not per-frame); tick positions on the quantile scale.

## Investor climate build (2026-07-07): taxes, labor, landlord law, Section 8
Research-verified (5-agent workflow) then built:
- **Property tax rate** (ACS B25103/B25077, county + ZCTA): `_tax_rate` masks top-codes
  (10001/199/2000001/9999) + <100 owners. FEEDS THE CAP RATE (`_investor_metrics(tax_rate=)`;
  fallback ZCTA/county ACS rate → STATE_TAX_RATES). ACS block is hoisted ABOVE the ZORI
  block in build_county_table — order matters.
- **Construction labor index** (`sources/bls.py`): QCEW NAICS-23 committed CSVs in
  `src/remon/data/` (2025+2024 vintages); county→prior-vintage→state fallback, each vintage
  indexed to its own US baseline; refresh via data.bls.gov ONLY (www.bls.gov 403s all bots).
- **Landlord friendliness** (`src/remon/data/landlord_scores.json`): editorial 0–10 state
  rubric (5 components × 0-2), 50 states+DC, adversarially fact-checked (WA 2025 rent cap,
  CA AB1482, OR SB608 etc.); county paint from state; ZIP panels look it up via the
  `landlord_json` template var (rerender_map.py passes it too). Re-verify annually.
- **Section 8** (`sources/hud.py` + `scripts/prepare_hud.py`): huduser.gov blocks bots
  (HTTP 202) so the SLIM CSVs ARE COMMITTED (`hud_safmr_2026.csv` 38.6k ZIPs,
  `hud_psh_county_2025.csv`); refresh annually by running prepare_hud.py on a residential
  IP (curl commands in its header). Metrics: sec8_premium (ZIP-only: SAFMR 2BR gross vs
  ZORI; tooltip shows 2BR/3BR ceilings + market rent), hud_units_per_1k + hcv_per_1k
  (county, ÷ ACS renter households ≥100).
- New selector group "Taxes & policy" (7 groups, 29 metrics total). 4 new fmt kinds
  (pct2u/idx/score/per1ku) mirrored in Python `_fmt` and the template JS.
- ACS caches must be re-fetched when config variables change (REMON_NO_CACHE=1) — the
  35-day cache otherwise serves the old columns.
- Review round 4 (this build): 3 distinct fixes applied — CT/AK wage index now crosswalks
  INSIDE bls.load_wage_index (xwalk param) before the state fallback (Hartford 1.142 not
  1.08); a junk cached QCEW download can no longer shadow the committed vintages
  (header validation + per-vintage fallback); HUD's "01XXX" pseudo-county rows filtered
  from the committed PSH file (3,126 real counties).

**Not yet deployed** — needs the usual PAT push (see Deploying below).

**Still relevant if revisited:** Mapbox GL JS with a free token was offered and declined
(free PMTiles path chosen). City-name search beyond the tracked-market aliases would need
the Census Gazetteer places file added to `geo_index.json` (clean v2 add). Reventure
metrics we deliberately did NOT clone (no free source): For Sale Inventory, Days on
Market, Home Sales, 1-yr forecast (ours covers tracked ZIPs only, on the dashboard).

## Key gotchas (so you don't rediscover them)
- **Python:** local is 3.9, CI is 3.11 — keep code 3.9-compatible.
- **Zillow 403s datacenter IPs:** `src/remon/http.py` sends a browser User-Agent, and
  `src/remon/sources/zillow.py` has `FALLBACK_URLS` to the files.zillowstatic.com CDN.
- **PMTiles needs HTTP byte-range serving.** GitHub Pages supports it; plain
  `python -m http.server` does NOT (returns full file → map fails). For local testing use
  the range server: `python3 ~/.recmon-tools/range_server.py` then open `localhost:8901/map.html`.
- **tippecanoe** (bakes the map vector tiles) was built from source at
  `~/.recmon-tools/tippecanoe/tippecanoe` (NOT on PATH, NOT in CI). `mapview._generate_tiles`
  runs it best-effort. **Consequence:** the monthly CI job has no tippecanoe, so the map's
  DATA won't auto-refresh (geometry never changes; committed `docs/*.pmtiles` are served).
  To enable monthly map refresh, add a tippecanoe build/install step to `monthly.yml`.
- **MapLibre `'load'` AND `isStyleLoaded()` are both unreliable** with the carto vector
  style → the template retries `addLayers()` under try/catch every 250 ms until the layer
  exists (addLayer throwing = not ready yet).
- **Backgrounded tabs never render**: Chrome suspends rAF when `document.hidden`, so the
  map shows a blank canvas until the tab is visible — this is why the template kicks
  `resize()+triggerRepaint()` every 400 ms until the first `'idle'` (bounded). If a
  headless/preview test sees a blank map + `isStyleLoaded()===false` forever, check
  `document.visibilityState` before suspecting the code.
- **`rerender_map.py` also rebuilds `docs/geo_index.json`** (cached inputs only) and derives
  ZIP states from config.yaml, not a hardcoded list. It does NOT rebuild `docs/zips_*.json`
  or the PMTiles — after changing metric definitions or ZIP values, run the full
  `render_map_page` (or `python run.py`) once so tiles/zip-jsons/geo_index all carry the
  new properties, then use rerender for template-only iteration.
- **Removing a market from config.yaml**: `render_map_page` now deletes the orphaned
  `docs/zips_<st>.json` and excludes it from geo_index — but the committed `zips.pmtiles`
  only refreshes where tippecanoe exists (this machine), see the tippecanoe gotcha.
- **Deploying:** needs a fresh GitHub PAT (repo + workflow scope) each time; before pushing,
  `git fetch origin && git rebase -X theirs origin/main` (remote has bot refresh commits ahead).

## How to run locally
```
cd ~/real-estate-monitor && source .venv/bin/activate
python run.py                 # fetch -> compute -> build; writes docs/
open docs/index.html          # dashboard
python3 ~/.recmon-tools/range_server.py &   # for the map (byte-range server)
open http://localhost:8901/map.html         # the map
# fast map-only re-render (skips slow tile/zip regen): python ~/.recmon-tools/rerender_map.py
```

## Suggested next steps
1. **Deploy the redesigned map** (commit + PAT push; docs/ is fully rebuilt locally).
2. Add tippecanoe to CI so the map's numbers refresh monthly too.
3. (v2) Census Gazetteer city names in the search index; 24-mo sparklines in the panel.
