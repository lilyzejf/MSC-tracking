# MSC Vessel Watch

Tracks vessel lists for a set of MSC-certified tuna fisheries and flags monthly
additions/removals, published as a static dashboard via GitHub Pages.

## How it works

- `scraper/fisheries.json` — the list of fisheries to track (name + URL slug on
  fisheries.msc.org). Slugs you don't know can be left as `null`; the scraper
  will look them up via the site search on first run and save them back here.
- `scraper/scraper.py` — for each fishery: finds the newest "Vessel List" PDF,
  downloads it, extracts vessel names, diffs against the last saved snapshot,
  and writes results into `docs/data/`.
- `.github/workflows/monthly-update.yml` — runs the scraper on the 3rd of each
  month, commits the updated data, and redeploys the Pages site.
- `docs/index.html` — the dashboard. Pure static HTML/JS, reads the JSON files
  in `docs/data/` at page load. No build step.

## One-time setup

1. **Push this repo** to `lilyzejf/MSC-tracking` (or wherever you created it).
2. **Enable GitHub Pages**: repo Settings → Pages → Source: "GitHub Actions".
3. **Run it once manually** to populate data and confirm everything resolves:
   repo → Actions tab → "Monthly vessel list update" → Run workflow.
4. Check the Action's log output for any `[WARN] could not resolve slug for…`
   or `[WARN] no vessel list found for…` lines — fill in/correct those slugs
   by hand in `scraper/fisheries.json` (visit the fishery's page on
   fisheries.msc.org and copy the slug from the URL).

## Important caveat: PDF parsing is best-effort

Different certification bodies format vessel list PDFs differently (some are
clean tables, some are looser text). `extract_vessel_names()` in scraper.py
handles both cases generically, but you should sanity-check the first run's
output against a couple of source PDFs and tighten `EXCLUDE_PATTERNS` (or add
certifier-specific parsing) if you see junk rows or missed vessels.

## Running locally

```
cd scraper
pip install -r requirements.txt
python scraper.py
```
