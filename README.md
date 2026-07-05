# hcmc-property-pipeline (SaiPrice)

A data pipeline for Ho Chi Minh City property listings, with a price prediction model served through a Django API and dashboard.

## What it does

SaiPrice pulls listing data from batdongsan.com.vn into PostgreSQL, serves it through a Django REST API, and estimates price for each listing using a model trained on location, size, and property type. A dashboard lets visitors browse and filter the results.

## Status

In development. Build started July 2026.

| Stage | Status |
|---|---|
| Scraper | Not started |
| SQL database | Not started |
| Django backend + API | In Progress |
| Frontend dashboard | Not started |
| ML price model | Not started |
| Deployment (Render) | Not started |
| Research writeup | Not started |

## Roadmap

Planned for a later phase:

- Office listings (second scraping target, same pattern as residential)
- Price trend charts over time
- Interactive map view of listings

## Tech Stack

- Python: scraping (BeautifulSoup/requests), machine learning (scikit-learn)
- PostgreSQL: database
- Django: backend and REST API
- Django templates + Tailwind CSS + Chart.js: dashboard
- Render: deployment

## Data

Listings are scraped from batdongsan.com.vn. If scraped coverage is thin, the project falls back to two public Kaggle datasets: Vietnam Housing Dataset 2024 and House Pricing HCMC.

## Setup

```bash
git clone https://github.com/Floner1/hcmc-property-pipeline-saiprice.git
cd hcmc-property-pipeline-saiprice
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Database setup and environment variable instructions will be added once the backend stage is built.

## License

MIT
