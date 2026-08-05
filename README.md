# SaiPrice

A pricing-transparency pipeline for the Ho Chi Minh City residential property market. It scrapes public listings, stores them in Postgres, tracks price changes over time, predicts a fair price per listing with a random forest model, and serves the results through a Django REST API and a server-rendered dashboard.

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
| Frontend dashboard | Listing list, filters (district, property type, price, search), detail page, and flagged-listings summary live (Tailwind, paginated) |
| ML price model | Trained (random forest, log-price target); `predicted_price` populated on active listings by `score_listings` |
| Deployment (Render) | Not started |
| Research writeup | Not started |

## How it works

- `scrape_listings --source alonhadat` crawls alonhadat's search results and listing detail pages with plain `requests` (no browser needed), and upserts each listing on `(source_site, source_id)`. A price change writes a new `PriceHistory` row, and a listing that drops out of a full crawl gets flagged `is_active=False`.
- `ingest_saved_listings` is a manual fallback for batdongsan.com.vn. Save a listing page as HTML by hand, feed the folder in, and it gets the same upsert behavior. It's not on a schedule and isn't a primary data source, since batdongsan blocks automated requests behind Cloudflare.
- `score_listings` runs after each scrape. It predicts a price for each active listing with the trained model and checks two anomaly rules, too few photos and too long on market. A third rule that compares price against the prediction is designed but not implemented yet.
- Everything lands in one Postgres database, queried directly by both the API and the dashboard.

## API

`GET /api/listings/`: active listings, paginated at 20 per page.

Filters: `district`, `property_type`, `listing_intent`, `min_price`, `max_price`, `min_area`, `max_area`, `is_anomaly`, `agent`, `district_id`.

Orderable by any field, `?ordering=price` or `?ordering=-price`, plus `?sort_by=days_on_market` (or `-days_on_market`) for the one computed field the ordering filter can't reach directly.

`GET /api/listings/<id>/`: single listing detail (active listings only).

Anomaly fields, populated by `score_listings` (CLAUDE.md §12):

- `is_anomaly` (boolean): true when any anomaly rule flagged the listing. Filterable: `GET /api/listings/?is_anomaly=true`.
- `anomaly_reason` (dict or null): one key per rule that ran in the last scoring pass, each mapping to `{"triggered": bool, "value": ...}`. Two rules run today, `low_photos` and `stale_listing`. A third, `price_gap`, would compare price against the model's own prediction, but it isn't built yet, so it never appears in the dict. Null on listings not yet scored. Read-only, not filterable.

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

- Python: `requests` + `beautifulsoup4` for scraping; `scikit-learn` (random forest, trained offline and committed as `model.pkl`) for the price model
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
python manage.py score_listings
python manage.py runserver
```

## Design notes

batdongsan.com.vn blocked plain requests with a 403 behind Cloudflare, and even headed Playwright without stealth patches got past the first page before failing on every page after. Adding stealth patches to beat a check built to catch that wasn't worth it, so alonhadat became the working automated source, with homedy planned as a second one.

Raw price skew measured at 4.675, driven by the luxury tail, and log price brought that down to 0.836, so log price became the training target.

Parsing price back out of the listing title to catch bad stored prices produced 11 false positives out of 11 flags, since titles carry per-square-meter figures or discount percentages instead of the listing price, so it got dropped.

## Full spec

[CLAUDE.md](CLAUDE.md) is the full technical specification, covering the database schema, dedup rules, error handling, anomaly detection, and the deployment plan.
