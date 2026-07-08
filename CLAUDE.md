# real-estate-monitor

Free self-hosted Reventure/Zillow-alternative real estate dashboard from public data.
Built, deployed, and auto-refreshing. Live: https://domrodd23.github.io/real-estate-monitor/

**Read `HANDOFF.md` first** — it has the full project state, what's done, the one open
problem (making the national map as intuitive as Zillow/Reventure), and the technical
gotchas (Zillow 403 fallback, PMTiles byte-range serving, tippecanoe location, deploy steps).

Quick facts:
- Pipeline: `run.py` = `fetch.py` → `compute.py` → `build.py`; shared code in `src/remon/`.
- Edit markets/ZIPs in `config.yaml` (no code changes needed).
- Keys in `.env` (git-ignored) + GitHub secrets.
- Run: `source .venv/bin/activate && python run.py`, then open `docs/index.html`.
- Map needs a byte-range server locally: `python3 ~/.recmon-tools/range_server.py` → `localhost:8901/map.html`.
