# SaiPrice

A pricing-transparency pipeline for the Ho Chi Minh City residential property market. It scrapes public listings, stores them in Postgres, tracks price changes over time, and serves the results through a Django REST API and a server-rendered dashboard. A price-prediction model is planned but not built yet.

It only ever sees what's publicly listed. This isn't a lead-generation tool and doesn't surface off-market inventory.

## Status

In development. Build started July 2026.

| Stage | Status |
|---|---|
| Scraper (alonhadat.com.vn, primary) | Live, runs on a local schedule |
| Scraper (homedy.com, secondary) | Not built yet |
| Scraper (batdongsan.com.vn) | Manual fallback only, Cloudflare blocks automation (see CLAUDE.md §6) |
| Database (Postgres) | Live: `Listing`, `Agent`, `PriceHistory`, `ScrapeRun` |
| Django backend + API | `GET /api/listings/` and `GET /api/listings/<id>/` live |
| Frontend dashboard | Listing list, filters (district, property type, price, search), and detail page live (Tailwind, paginated); anomaly summary view not built yet |
| ML price model | Not started |
| Deployment (Render) | Not started |
| Research writeup | Not started |

## How it works

- `scrape_listings --source alonhadat` crawls alonhadat's search results and listing detail pages with plain `requests` (no browser needed), and upserts each listing on `(source_site, source_id)`. A price change writes a new `PriceHistory` row, and a listing that drops out of a full crawl gets flagged `is_active=False`.
- `ingest_saved_listings` is a manual fallback for batdongsan.com.vn. Save a listing page as HTML by hand, feed the folder in, and it gets the same upsert behavior. It's not on a schedule and isn't a primary data source, since batdongsan blocks automated requests behind Cloudflare.
- Everything lands in one Postgres database, queried directly by both the API and, eventually, the dashboard.

## API

`GET /api/listings/`: active listings, paginated at 20 per page.

Filters: `district`, `property_type`, `listing_intent`, `min_price`, `max_price`, `min_area`, `max_area`, `is_anomaly`, `agent`, `district_id`.

`GET /api/listings/<id>/`: single listing detail (active listings only).

Anomaly fields, populated by `score_listings` (CLAUDE.md §12):

- `is_anomaly` (boolean): true when any anomaly rule flagged the listing. Filterable: `GET /api/listings/?is_anomaly=true`.
- `anomaly_reason` (dict or null): one key per rule that ran in the last scoring pass (`price_gap`, `low_photos`, `stale_listing`), each mapping to `{"triggered": bool, "value": ...}`. `low_photos` and `stale_listing` run today, so stored dicts carry two keys; `price_gap` joins once the ML model ships. Null on listings not yet scored. Read-only, not filterable.

A flagged listing:

```json
{
  "id": 31,
  "is_anomaly": true,
  "anomaly_reason": {
    "low_photos": {"triggered": true, "value": 2},
    "stale_listing": {"triggered": false, "value": 14}
  }
}
```

## Tech stack

- Python: `requests` + `beautifulsoup4` for scraping; `scikit-learn` planned for the price model
- PostgreSQL
- Django 5.2 + Django REST Framework + `django-filter`
- Django templates + Tailwind CSS 4 via `django-tailwind-cli` (standalone binary, no Node.js/npm) for the dashboard; Chart.js only if trend charts ship (stretch)
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

Create a `.env` file (gitignored) in the project root:

```
DB_NAME=...
DB_USER=...
DB_PASSWORD=...
DB_HOST=...
DB_PORT=...
```

Then:

```bash
python manage.py migrate
python manage.py scrape_listings --source alonhadat
python manage.py runserver
```

## Full spec

[CLAUDE.md](CLAUDE.md) is the full technical specification, covering the database schema, dedup rules, error handling, anomaly detection, and the deployment plan.
