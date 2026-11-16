# SaiPrice

A pricing-transparency pipeline for the Ho Chi Minh City residential property market. It scrapes public listings, stores them in Postgres, tracks price changes over time, estimates an expected asking price for each listing with a random forest model trained on other listings' asking prices, and serves the results through a Django REST API and a server-rendered dashboard.

It only ever sees what's publicly listed. This isn't a lead-generation tool and doesn't surface off-market inventory.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the pipeline, model and web app fit together.

## Status

In development. Build started July 2026.

| Stage | Status |
|---|---|
| Scraper (alonhadat.com.vn, primary) | Runs on a local schedule. Searches apartments, houses and villas for sale. Recent runs are partial, see Limits. |
| Scraper (homedy.com, secondary) | Runs on the same schedule, first 10 search pages of apartments for sale. Feeds the district medians on `/districts/`. Hidden from the listing pages, `/flagged/` and the API. Never delists. |
| Scraper (batdongsan.com.vn) | Manual fallback only, Cloudflare blocks automation |
| Scraper (maisonoffice.vn, offices) | `scrape_offices`, run by hand, not in the daily script. Feeds `/offices/`. |
| Database (Postgres) | Live: `Listing`, `Agent`, `PriceHistory`, `ScrapeRun`, `ScoringRun`, `DistrictSnapshot`, `OfficeBuilding`, `OfficeRentHistory` |
| Django backend + API | `GET /api/listings/` and `GET /api/listings/<id>/` live, alonhadat listings only |
| Frontend dashboard | Listing list with filters (district, property type, price, search), detail page, `/flagged/`, `/districts/`, `/offices/` and `/health/` live (Tailwind, paginated) |
| ML price model | Trained (random forest, log-price target). `predicted_price` set on active alonhadat sale listings by `score_listings` |
| Deployment (Render) | Not started |
| Research writeup | Not started |

## Limits

Figures as of 2026-11-16.

- Every price is an asking price from a public listing, not a sale price. The schema has no sale price.
- The delist sweep last fired on 2026-09-28. It skips any run that logs an `srp_` code, and every recent alonhadat run has. So `is_active` is stale: 172 of 1,950 active alonhadat listings were last seen more than 30 days ago.
- alonhadat crawls are partial. Since 2026-10-19, 6 of 26 runs saw 0 listings and the others saw between 60 and 819. The last three saw 377, 799 and 800. Each search root that stops early logs `srp_fetch_gave_up`: three failed tries at one search page, each a timeout, a connection or SSL error, a 5xx or a 429.
- The house search root failed on its first page in all 26 alonhadat runs since it was added on 2026-10-19: 18 times with a 429, 7 with an SSL error and once with a connection error. Each reason is from the last of three tries. No house search page has ever been parsed. The villa root got past page 1 in only 2 of the 26 runs, on 11-03 and 11-10, and all 75 villas were first scraped on those two days. Active alonhadat rows: 1,875 apartments, 75 villas, 0 houses.
- `/districts/` medians cover apartments for sale seen in the last 30 days. Apartments under 10 triệu per m² are left out as likely rents or mistyped prices. A day the crawl is blocked still gets a snapshot, built from rows seen in the previous 30 days, so it repeats earlier figures. The first snapshot is dated 2026-11-14.
- homedy posted dates are approximate. They come from text like "2 ngày trước" (2 days ago), so a "2 tuần trước" listing is dated 14 days back. homedy rows never delist and never appear on the listing pages or the API.
- The price model learned from 2,044 apartments and 1 house. It has no property type input, so it prices a house or villa as an apartment of the same size in the same district. Its held-out median error is 24.4% on 409 test rows.

## How it works

- `scrape_listings` crawls one source per run and upserts each listing on `(source_site, source_id)`. A price change writes a `PriceHistory` row.
- `score_listings` predicts an asking price for alonhadat sale listings and runs three anomaly rules. `snapshot_districts` stores the day's district medians.
- [ARCHITECTURE.md](ARCHITECTURE.md) covers the crawl, upsert, delisting, scoring and model in detail.

## API

`GET /api/listings/`: active alonhadat listings, paginated at 20 per page. homedy and batdongsan rows are not served.

Filters: `district`, `property_type`, `listing_intent`, `min_price`, `max_price`, `min_area`, `max_area`, `is_anomaly`, `agent`, `district_id`.

Orderable by any field, `?ordering=price` or `?ordering=-price`, plus `?sort_by=days_on_market` (or `-days_on_market`) for the one computed field the ordering filter can't reach directly.

`GET /api/listings/<id>/`: single listing detail, active alonhadat listings only.

Anomaly fields, populated by `score_listings`:

- `is_anomaly` (boolean): true when any anomaly rule flagged the listing. Filterable: `GET /api/listings/?is_anomaly=true`.
- `anomaly_reason` (dict or null): one key per rule that ran in the last scoring pass, each mapping to `{"triggered": bool, "value": ...}`. Three rules run: `price_gap` (the listing sits in the top 5% of `(predicted_price - price) / price` for that run, recomputed each run rather than a fixed cutoff), `low_photos` (fewer than 3 photos) and `stale_listing` (more than 90 days on market). A rule that couldn't run on a listing is absent from the dict, not written as `triggered: false`. `price_gap` needs both prices set and at least 20 comparable listings in the run. `low_photos` needs a parsed gallery. `stale_listing` needs a computable days on market. Null on listings not yet scored. Read-only, not filterable.

A flagged listing:

```json
{
  "id": 8,
  "is_anomaly": true,
  "anomaly_reason": {
    "price_gap": {"triggered": false, "value": -0.1375},
    "low_photos": {"triggered": true, "value": 0},
    "stale_listing": {"triggered": false, "value": 9}
  }
}
```

## Tech stack

- Python: `requests` + `beautifulsoup4` for scraping, `scikit-learn` (random forest, trained offline and committed as `model.pkl`) for the price model
- PostgreSQL
- Django 5.2 + Django REST Framework + `django-filter`
- Django templates + Tailwind CSS 4 via `django-tailwind-cli` (standalone binary, no Node.js/npm) for the dashboard. Chart.js only if trend charts ship (stretch)
- Render for deployment (planned)

## Setup

```bash
git clone https://github.com/Floner1/saiprice.git
cd saiprice
python -m venv venv
venv\Scripts\activate      # Windows
# source venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
```

Copy `.env.example` to `.env` (gitignored) in the project root and fill it in:

```
SECRET_KEY=...
DB_NAME=...
DB_USER=...
DB_PASSWORD=...
DB_HOST=...
DB_PORT=...
```

`SECRET_KEY` has no default, so Django won't start without it. `.env.example` has a one-liner to generate one, and the deploy-hardening variables listed there all default to off for local development.

Then:

```bash
python manage.py migrate
python manage.py tailwind build
python manage.py scrape_listings --source alonhadat
python manage.py score_listings
python manage.py runserver
```

`tailwind build` compiles `assets/css/tailwind.css`, which is gitignored and not shipped in the repo. Skip it and the dashboard loads unstyled. Rebuild after changing `tailwind_src/source.css`, or run `python manage.py tailwind watch` alongside `runserver` for live rebuilds during template work.

### Tests

```bash
python manage.py test listings
```

Django creates a throwaway test database, so the `DB_USER` in `.env` needs permission to create one.

### Commands

| Command | What it does |
|---|---|
| `scrape_listings --source alonhadat` or `--source homedy` | Crawl one source's search pages and upsert each listing. `--pages N` caps pages per search root and skips the delist sweep. `--max-ldp-visits N` (default 40) caps detail-page visits. `--no-ldp-enrich` turns them off. homedy never visits detail pages. |
| `score_listings` | Predict prices and run the anomaly rules on active alonhadat listings |
| `snapshot_districts` | Store today's median asking price per m² by district, one set of rows per source |
| `train_model` | Fit linear regression and random forest, print held-out metrics, pickle the winner to `listings/ml/model.pkl` |
| `build_dataset` | Print the cleaned training set's row counts per drop rule. Read-only. |
| `report_stats` | Write market statistics for the research writeup to a timestamped file |
| `scrape_offices` | Crawl maisonoffice.vn buildings into the office tables. `--districts`, `--per-district`, `--dry-run`. |
| `ingest_saved_listings <folder>` | Load hand-saved batdongsan pages. Never delists. |

### Daily script

`run_scraper.ps1` runs these in order and appends all output to `scraper_run_reports/<timestamp>_crawl.txt`:

1. `scrape_listings --source alonhadat`
2. `score_listings`
3. `scrape_listings --source homedy --pages 10`
4. `snapshot_districts`

Windows Task Scheduler runs it on a local schedule. The schedule lives in Task Scheduler, not in the repo. The script hardcodes the project path on line 1 and expects `venv\` and `scraper_run_reports\` to exist in the project root.
