# Architecture

How SaiPrice is put together and why. For setup and the API reference, see the [README](README.md).

## The shape of it

Two programs share one Postgres database.

- A **pipeline** runs on a local machine on a schedule. It scrapes listings, writes them to Postgres, then scores them.
- A **web app** reads the same database. It serves a REST API and a server-rendered dashboard.

```
alonhadat.com.vn
      |  plain HTTP, no browser
      v
scrape_listings --> upsert --> Postgres <-- score_listings (model.pkl)
                                  ^
                                  |  ORM reads
                       dashboard + REST API (Django)
```

The scraper never runs on the web host. Both sides only meet in the database.

There is one Django app, `listings`. A solo project has no reason to split it.

## Repository map

```
listings/
  models.py            Listing, Agent, PriceHistory, ScrapeRun, ScoringRun,
                       OfficeBuilding, OfficeRentHistory
  upsert.py            the one write path for listings
  scraping/
    client.py          HTTP session, retry, backoff, rate limit, bot-wall detection
    currency.py        Vietnamese price text -> whole VND
    parsers.py         shared parsed-listing type and helpers
    sites/             one module per source (alonhadat, maisonoffice)
  management/commands/ scrape_listings, score_listings, train_model, and others
  ml/                  dataset, features, train, predict, committed model.pkl
  analytics.py         read-side numbers for /health/
  office_analytics.py  read-side numbers for /offices/
  views.py             dashboard views (ORM direct, no self-HTTP)
  api/                 DRF serializers, filters, views
  templates/           Django templates styled with Tailwind
saiprice/              settings and root URLs
tailwind_src/          the only hand-written CSS
run_scraper.ps1        what the scheduled task runs
```

Dashboard views query the ORM directly. They do not call the app's own API. That would be a pointless HTTP round trip inside one process.

## Data model

| Model | Role |
|---|---|
| `Listing` | One row per source listing. Identity is `(source_site, source_id)`. |
| `Agent` | One row per distinct agent per source. Deduplicated on the same key. |
| `PriceHistory` | One row on first sight and one on every observed price change. |
| `ScrapeRun` | One row per scraper invocation, scoped to one source. |
| `ScoringRun` | One row per `score_listings` invocation. |
| `OfficeBuilding`, `OfficeRentHistory` | Office rents from maisonoffice.vn. Kept apart from `Listing` on purpose. |

Decisions that shape the rest:

- **Prices are whole VND** in `DecimalField(decimal_places=0)`. Text like `8 tỷ` or `129,03 triệu` is parsed once, in `currency.py`, for every source.
- **Null price means negotiable.** "Thỏa thuận" listings keep a null `price`. `PriceHistory.price` is nullable too, so a new listing always gets exactly one history row.
- **Days on market is computed, not stored.** It is `(delisted_at or now) - posted_date`, with a fallback to the earliest `PriceHistory` row when `posted_date` is missing.
- **Offices are separate tables.** Their rents are USD per m² per month as a range. Residential code (API, model, scoring, delisting) must never see them.
- **Dedup is exact only.** The same flat reposted under a new `source_id` becomes a second row. Fuzzy matching was ruled out, so that is a known limit.

## Scraping

### Fetching

`client.fetch` returns `(response, error_code)`. Failures are named, not collapsed into `None`, so a bot wall, a timeout and a dead URL show up as different things.

- Transient errors (timeout, connection, 5xx) retry three times with backoff of 2, 4 and 8 seconds.
- A 429 honors `Retry-After` when present.
- Every request is followed by a random 1 to 3 second sleep. Random spacing avoids an even-interval fingerprint.
- alonhadat's challenge arrives as a 200 redirect to `/xac-thuc-nguoi-dung.html`. The client detects it and gives up with no retry.

The client sends a normal desktop user agent and nothing else. It never tries to solve a challenge. If a site blocks automated traffic, that source is dropped, not fought. That is why batdongsan.com.vn is not an automated source. Cloudflare blocks it on every navigation.

### Crawl loop

`scrape_listings --source alonhadat` walks search result pages (SRPs) for each category root, then optionally visits the listing detail page (LDP) for each new or unenriched row.

- Pagination stops when a page contains no unseen listing IDs. Out-of-range pages re-serve earlier content, so an empty diff means the category is exhausted.
- The SRP category is provisional because VIP cards can be cross-category. `property_type` and `listing_intent` are confirmed from the LDP breadcrumb and written only then.
- LDP visits are capped per run (`--max-ldp-visits`, default 40) and can be turned off (`--no-ldp-enrich`). A bulk LDP crawl is what triggered alonhadat's wall once.
- `images IS NULL` means "LDP never successfully visited". Those rows stay eligible and the backlog is served oldest first.
- The crawl scope is apartments, houses and villas for sale in Ho Chi Minh City, one category root each. Land and rent categories are mapped for LDP confirmation but are not crawl roots, so none of those listings are scraped. Each root is added only after a run on the previous scope proves stable.

### Upsert

`upsert.py` is the only place a `Listing` is written. Per parsed listing:

1. Resolve the `Agent` if the source gave a stable agent ID.
2. Look up the existing row by `(source_site, source_id)`.
3. If the parsed price differs from the stored price, insert a `PriceHistory` row. This compares before the overwrite.
4. Overwrite every field, set `last_seen_at`, set `is_active=True`, clear `delisted_at`.
5. If the row is new, insert its first `PriceHistory` row.

The overwrite is total. There is no per-field diffing. A field that fails to parse becomes null and heals on the next pass if the source parses again.

A listing missing a required field (`source_site`, `source_id`, `url`, `title`, `property_type`, `listing_intent`) is skipped and counted, never half-saved.

### Delisting

A listing is delisted when a full crawl does not touch it. `sweep_delistings` runs at the end of a complete crawl and flips `is_active=False` on rows whose `last_seen_at` is older than the run start.

A sweep is the most destructive thing the scraper does, so it refuses to run when the crawl looks wrong:

- The run was capped with `--pages`, so it was partial by design.
- Any SRP failure code was recorded. A failed page ends pagination the same way exhaustion does, so a truncated crawl looks complete.
- `listings_seen` is zero.
- `listings_seen` is under half of the previous finished run for the same source.

An LDP 404 does **not** delist. Every LDP URL comes from an SRP card in the same run, so the SRP just asserted the listing is live. It is counted as `ldp_404` and the row retries next run.

### Run tracking

Each run writes a `ScrapeRun`. `status_counts` is a dict of failure code to count, and each failure bumps exactly one key. Codes include `srp_bot_challenge`, `srp_fetch_gave_up`, `ldp_404`, `ldp_soft_gone`, `required_field_missing`, `upsert_exception` and `run_aborted`.

`error_count` excludes four codes that describe a page, not a malfunction: `ldp_404`, `ldp_soft_gone`, `ldp_no_anchor` and `unmapped_breadcrumb`.

`finished_at` means completed. An aborted run leaves it null so it never becomes the baseline for the next sweep. `posted_date_nulls` catches a silent markup change. A renamed date label nulls the field across the whole source without raising an error, so the run warns when the null rate passes 80%.

## Scoring and anomaly rules

`score_listings` runs after each scrape. It does two jobs over active listings.

**Prediction.** Sale listings with a positive area, a `posted_date`, an area under 500 m² and at most 10 bedrooms get a `predicted_price`. The caps are shared with the training set. A random forest does not extrapolate. It returns its top leaf, so a 19,884 m² building scored as an ordinary flat. Rows outside the caps get null, not a wrong number. Rent listings are skipped because rent is a monthly figure and sale price is a total.

**Anomaly rules.** Each rule picks its own population and writes its own key in `anomaly_reason`.

| Rule | Triggers when | Runs only on |
|---|---|---|
| `price_gap` | `(predicted - price) / price` is in the top 5% of this run | rows with both prices, and only if at least 20 exist |
| `low_photos` | fewer than 3 images | rows with `images` not null |
| `stale_listing` | more than 90 days on market | rows where days on market can be computed |

The price-gap cutoff is a percentile recomputed each run, not a fixed percentage. A fixed value would hard-code an accuracy figure nobody has measured.

`anomaly_reason` holds one key per rule that actually ran. A rule that could not run is absent, not written as `triggered: false`. For `low_photos`, null `images` means "never parsed" and `[]` means "parsed, zero photos". Only `[]` counts as a photo count.

A model failure must not wipe stored predictions. An empty prediction set means either "nothing qualified" or "the model failed". Only the first clears old values.

Each run writes a `ScoringRun` with the in-sample median absolute percentage error and a fingerprint of `model.pkl`. The error is in-sample, so it reads better than held-out accuracy and must not be quoted as such.

## The price model

- **Features:** `area_sqm`, `listing_age` (days since posting), `district_median_price`, and one-hot `property_type`. The current fit has no `property_type` column, because its training split holds only apartments.
- **Target:** `log(price)`. Raw VND is heavily right-skewed (4.675 measured) and log brings it to 0.836. Predictions are back-transformed with `exp`, so each value is a conditional median.
- **Candidates:** linear regression (scaled) and a random forest on one shared split. The lower held-out RMSE ships.
- **No leakage:** the split is grouped on `(price, area, district)`, so rows sharing all three always land on the same side. District medians are fitted on the training split only, then applied to both splits. A district with fewer than 10 training rows falls back to the training-wide median.
- **Cleaning** (`ml/dataset.py`, read-only): sale listings only, drop missing or non-positive price and area, drop whole buildings by the area and bedroom caps, drop exact reposts by `(title, price, area)`, then twins by `(price, area, district)`, keeping the lowest id, and drop rows priced above 3.5 times their district's 90th-percentile price per m². The percentile is the anchor, not the median, because Quận 1 has a real luxury tier that a median rule would mistake for a typo.
- **Delivery:** `train_model` is manual. The winning model is pickled to `listings/ml/model.pkl` and committed. Deploys do not retrain.
- **Limits:** the model has been retrained since houses and villas were added to the crawl, but the cleaned set still holds 2,044 apartments and 1 house, and the fitted model has no `property_type` column. Until more houses are crawled, it prices a house or villa as if it were an apartment of the same size in the same district. `listing_age` saturates past the oldest training row. There is no accuracy floor. Whatever the better model reaches is what ships.

## Web layer

Routes: `/` (list with filters and search), `/listing/<id>/`, `/flagged/`, `/health/`, `/offices/`, `/admin/`, `/api/`.

- `/health/` charts scrape and scoring runs from `analytics.py`, so a broken parser shows up as a drop in listings seen or a spike in errors.
- `/offices/` is the office rent view, backed by `office_analytics.py`.
- The API is Django REST Framework with `django-filter`, 20 per page.
- Styling is Tailwind CSS 4 through `django-tailwind-cli`, a standalone binary with no Node.js. The compiled stylesheet is gitignored and built on deploy. Content scanning is an allowlist (`@source`), because automatic detection compiled selectors out of unrelated files.
- Static files are served by WhiteNoise.

## Deployment and scheduling

The web app deploys to Render and reads a remote Postgres. The pipeline stays on a local Windows machine.

`run_scraper.ps1` runs `scrape_listings` and then `score_listings`, with all output streams redirected into a timestamped report. Windows Task Scheduler fires it daily. The scraper stays local because it already works that way and moving it adds schedule risk for no gain. Run monitoring through `ScrapeRun` is the same either way.

The cost is gaps in `PriceHistory` and delisting detection when the machine is off or asleep. That is accepted until it causes a real problem.

Secrets and host settings come from environment variables through `python-decouple`. `DEBUG` defaults to off and `ALLOWED_HOSTS` defaults to empty, so a misconfigured deploy fails closed.

## Sources and scope

| Source | Status |
|---|---|
| alonhadat.com.vn | Automated, plain `requests`. Primary. |
| batdongsan.com.vn | No automation. Cloudflare-gated. `ingest_saved_listings` loads hand-saved pages as a rare fallback and never delists. |
| maisonoffice.vn | Automated through its sitemap into the office tables. |

Out of scope on purpose:

- Cross-listing fuzzy duplicate detection.
- An "individual seller" anomaly signal. The sources expose no reliable field to separate agencies from individuals.
- Phone numbers gated behind a source's own reveal flow.
- A minimum model accuracy.

## Known limits

- **Dead pages.** alonhadat can serve a dead LDP as a 200 with a placeholder shell. The parser detects it by title (`ldp_soft_gone`). A permanently dead page stays retry-eligible and spends LDP budget every run.
- **Posted dates.** alonhadat's `posted_date` appears to be a refresh date, so `days_on_market` can undercount.
- **Bot wall.** Extra manual crawl starts can re-trigger alonhadat's wall. Let the schedule run on its own.
- **Local gaps.** A sleeping or offline machine leaves holes in history.
- **Mixed fonts.** The site font lacks many Vietnamese diacritics, so the browser draws those from a fallback face.
