# Architecture

How SaiPrice is put together and why. For setup, commands and the API reference, see the [README](README.md).

## The shape of it

Two programs share one Postgres database.

- A **pipeline** runs on a local machine on a schedule. It scrapes listings, writes them to Postgres, scores them, then stores the day's district medians.
- A **web app** reads the same database. It serves a REST API and a server-rendered dashboard.

```
alonhadat.com.vn     homedy.com
        |                |      plain HTTP, no browser
        v                v
      scrape_listings (one source per run)
               |
               v
            upsert --> Postgres <-- score_listings (model.pkl)
                          ^   ^
                          |   +---- snapshot_districts (DistrictSnapshot)
                          |  ORM reads
               dashboard + REST API (Django)
```

The scraper never runs on the web host. Both sides only meet in the database.

There is one Django app, `listings`. A solo project has no reason to split it.

## Repository map

```
listings/
  models.py              Listing, Agent, PriceHistory, ScrapeRun, ScoringRun,
                         DistrictSnapshot, OfficeBuilding, OfficeRentHistory,
                         and PUBLIC_SOURCES
  upsert.py              the one write path for listings
  scraping/
    client.py            HTTP session, per-host settings, retry, backoff,
                         rate limit, bot-wall detection
    currency.py          Vietnamese price text -> whole VND
    parsers.py           shared parsed-listing type and helpers, mask_phones
    sites/               one module per source (alonhadat, homedy, maisonoffice)
  management/commands/   scrape_listings, score_listings, snapshot_districts,
                         train_model, build_dataset, report_stats,
                         scrape_offices, ingest_saved_listings
  ml/                    dataset, features, train, predict, committed model.pkl
  analytics.py           read-side numbers for /health/
  district_analytics.py  read-side numbers for /districts/ and snapshot_districts
  office_analytics.py    read-side numbers for /offices/
  views.py               dashboard views (ORM direct, no self-HTTP)
  api/                   DRF serializers, filters, views
  templates/             Django templates styled with Tailwind
saiprice/                settings and root URLs
tailwind_src/            the only hand-written CSS
run_scraper.ps1          what the scheduled task runs
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
| `DistrictSnapshot` | One row per day, source, district and property type: median asking price per m², quartiles and counts. History only. No page reads it yet. |
| `OfficeBuilding`, `OfficeRentHistory` | Office rents from maisonoffice.vn. Kept apart from `Listing` on purpose. |

Decisions that shape the rest:

- **Prices are whole VND** in `DecimalField(decimal_places=0)`. Text like `8 tỷ` or `129,03 triệu` is parsed once, in `currency.py`, for every source.
- **Every price is an asking price.** The sources publish what sellers ask. The schema has no sale price.
- **Null price means no parseable price.** "Thỏa thuận" (negotiable) listings and price-range cards keep a null `price`. `PriceHistory.price` is nullable too, so a new listing always gets exactly one history row.
- **Days on market is computed, not stored.** It is `(delisted_at or now) - posted_date`, with a fallback to the earliest `PriceHistory` row when `posted_date` is missing.
- **Offices are separate tables.** Their rents are USD per m² per month as a range. Residential code (API, model, scoring, delisting) must never see them.
- **Dedup is exact only.** The same flat reposted under a new `source_id` becomes a second row. Fuzzy matching was ruled out, so that is a known limit.

## Sources and scope

| Source | Status |
|---|---|
| alonhadat.com.vn | Automated, plain `requests`. Primary. Search pages for apartments, houses and villas for sale, plus capped detail-page visits. The only source a visitor sees listing by listing. |
| homedy.com | Automated, plain `requests`. Search pages for apartments for sale only. No detail pages, so no agent and no images. The daily script runs it with `--pages 10`, which skips the delist sweep, so its rows never delist. Feeds the district medians and nothing else a visitor sees. |
| batdongsan.com.vn | No automation. Cloudflare-gated. `ingest_saved_listings` loads hand-saved pages as a rare fallback and never delists. |
| maisonoffice.vn | Automated through its sitemap into the office tables. |

Out of scope on purpose:

- Cross-listing fuzzy duplicate detection.
- An "individual seller" anomaly signal. The sources expose no reliable field to separate agencies from individuals.
- Phone numbers. A number gated behind a source's own reveal flow is never fetched. homedy prints the agent's number in plain HTML on its listing pages, so the homedy parser never fetches a listing page. Sellers also type numbers into titles. `mask_phones` in `parsers.py` cuts anything `PHONE_RE` matches (a leading `0` or `+84`, then 9 or 10 digits with optional spaces, dots or dashes) out of homedy titles, URL slugs, addresses and skip-log lines.
- A minimum model accuracy.

## Scraping

### Fetching

`client.fetch` returns `(response, error_code)`. Failures are named, not collapsed into `None`, so a bot wall, a timeout and a dead URL show up as different things.

- One shared session sends a desktop browser User-Agent, a `vi-VN` Accept-Language and a standard Accept header.
- `SITES` holds per-host overrides. A host not listed gets the session defaults. homedy.com is the only entry. It gets its own User-Agent, `SaiPriceBot/1.0` with the repo URL, so the site can see who is asking.
- Transient errors (timeout, connection, 5xx) retry three times with backoff of 2, 4 and 8 seconds.
- A 429 on alonhadat is retried the same way, honoring `Retry-After` when present. After the third failed try it returns `fetch_gave_up`, like any other transient error. That ends one root, not the run, so the crawl moves on to the next root. A 429 on homedy is never retried. The first one returns `rate_limited` and logs the `Retry-After` value, and `rate_limited` ends the whole run.
- A request that is answered and not retried is followed by a random 1 to 3 second sleep. Random spacing avoids an even-interval fingerprint.
- Two responses count as a bot wall and return `bot_challenge` with no retry. One is alonhadat's 200 redirect to `/xac-thuc-nguoi-dung.html`. The other is any response with Cloudflare's `cf-mitigated: challenge` header, which arrives as a 403 or 503 and would otherwise read as an HTTP error or a retryable 5xx.

The client never tries to solve a challenge. If a site blocks automated traffic, that source is dropped, not fought. That is why batdongsan.com.vn is not an automated source. Cloudflare blocks it on every navigation.

### Crawl loop

`scrape_listings --source alonhadat` or `--source homedy` walks the search result pages (SRPs) of each category root for that one source. alonhadat then optionally visits the listing detail page (LDP) for each new or unenriched row. homedy has no LDP parser, so enrichment is off for it.

- Pagination stops when a page contains no unseen listing IDs. Out-of-range pages re-serve earlier content, so an empty diff means the category is exhausted.
- An SRP fetch error ends that root and records `srp_` plus the error code. The crawl moves on to the next root.
- A bot wall or a homedy rate limit (`STOP_CRAWL`) ends the whole run. No later root is tried, because a wall answers every page the same way.
- A first page that yields no cards and no skips records `srp_no_cards`. That is a changed page or a block page served as a 200, not an empty category.
- The alonhadat SRP category is provisional because VIP cards can be cross-category. `property_type` and `listing_intent` are confirmed from the LDP breadcrumb and written only then.
- A homedy card outside the apartment root is skipped as a missing `property_type`, since there is no LDP to confirm it. An area range ("55 - 120 m2") is a project's spread, so it stores a null area. District text is mapped to alonhadat's labels so both sources group together. A label outside that list stores a null district and records `unmapped_district`.
- homedy shows a relative age ("2 ngày trước", "2 tuần trước"), not a date. `posted_date` is today minus that age in Ho Chi Minh City time, so it is approximate.
- LDP visits are capped per run (`--max-ldp-visits`, default 40) and can be turned off (`--no-ldp-enrich`). A bulk LDP crawl is what triggered alonhadat's wall once.
- `images IS NULL` means "LDP never successfully visited". Those rows stay eligible and the backlog is served oldest first.
- Land and rent categories are mapped for alonhadat's LDP breadcrumb but are not crawl roots, so none of those listings are scraped.

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

Each run writes a `ScrapeRun`. `status_counts` is a dict of failure code to count, and each failure bumps exactly one key.

| Where | Codes |
|---|---|
| Search page | `srp_bot_challenge`, `srp_rate_limited`, `srp_fetch_gave_up`, `srp_http_error`, `srp_http_404`, `srp_no_cards` |
| Detail page | `ldp_bot_challenge`, `ldp_fetch_gave_up`, `ldp_http_error`, `ldp_404`, `ldp_soft_gone`, `ldp_no_anchor`, `unmapped_breadcrumb` |
| Per listing | `required_field_missing`, `upsert_exception`, `unmapped_district` |
| Whole run | `run_aborted` |

`error_count` excludes five codes (`NON_ERROR_CODES`). `ldp_404` and `ldp_soft_gone` describe a page that is not real content. `ldp_no_anchor` and `unmapped_breadcrumb` are nullable-field parse failures. `unmapped_district` is a homedy district outside the known list, and the row is kept with no district.

`finished_at` means completed. An aborted run leaves it null so it never becomes the baseline for the next sweep. `posted_date_nulls` catches a silent markup change. A renamed date label nulls the field across the whole source without raising an error, so the run warns when the null rate passes 80%.

## Scoring and anomaly rules

`score_listings` runs after the alonhadat crawl. It does two jobs over active listings from `PUBLIC_SOURCES`.

**Which sources.** `PUBLIC_SOURCES` is alonhadat alone. A source with no delisting sweep would keep sold listings live forever, so homedy and batdongsan rows are never scored, never trained on and never shown one by one. The same tuple filters the list, detail and flagged pages and the API. It lives in `models.py`, not `district_analytics.py`, because `district_analytics.py` imports `ml/dataset.py`, and `ml/dataset.py` needs the tuple. Defining it in `district_analytics.py` would make that import circular.

**Prediction.** Sale listings with a positive area, a `posted_date`, an area of at most 500 m² and at most 10 bedrooms get a `predicted_price`. The caps are shared with the training set. A random forest does not extrapolate. It returns its top leaf, so a 19,884 m² building scored as an ordinary flat. Rows outside the caps get null, not a wrong number. Rent listings are skipped because rent is a monthly figure and sale price is a total.

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

The model estimates an expected asking price from other listings' asking prices.

- **Features:** `area_sqm`, `listing_age` (days since posting), `district_median_price`, and one-hot `property_type`. The shipped fit has no `property_type` column, because its training split held only apartments. `_design` drops the first dummy column, so a train split with one type keeps none. The cleaned set's only house landed in the test split.
- **Target:** `log(price)`. Raw VND is heavily right-skewed (4.675 measured) and log brings it to 0.836. Predictions are back-transformed with `exp`, so each value is a conditional median.
- **Candidates:** linear regression (scaled) and a random forest on one shared split. The lower held-out RMSE ships.
- **No leakage:** the split is grouped on `(price, area, district)`, so rows sharing all three always land on the same side. District medians are fitted on the training split only, then applied to both splits. A district with fewer than 10 training rows falls back to the training-wide median.
- **Cleaning** (`ml/dataset.py`, read-only): alonhadat rows only (`PUBLIC_SOURCES`), sale listings only, drop missing or non-positive price and area, drop rows with no district or no `posted_date`, drop whole buildings by the area and bedroom caps, drop exact reposts by `(title, price, area)`, then twins by `(price, area, district)`, keeping the lowest id, and drop rows priced above 3.5 times their district's 90th-percentile price per m². The percentile is the anchor, not the median, because Quận 1 has a real luxury tier that a median rule would mistake for a typo.
- **Delivery:** `train_model` is manual. The winning model is pickled to `listings/ml/model.pkl` and committed. A deploy loads that file and does not retrain. `train()` stores no metrics, so the retrain commit message is the record of held-out accuracy.

## District prices

`/districts/` and `snapshot_districts` share `district_stats` in `district_analytics.py`. Both report median **asking** price per m², never sale prices.

- **Sources.** `MARKET_SOURCES` is alonhadat and homedy, wider than `PUBLIC_SOURCES`. A median over recently seen rows does not depend on a delisting sweep, so homedy's rows count here even though no page lists them.
- **Freshness.** A row counts only if it is active and was seen in the last `FRESH_DAYS` (30). `is_active` alone is not enough, because the sweep is skipped after any `srp_` code and so rarely runs.
- **Population.** Sale listings with a positive price and area and a district, inside the same area and bedroom caps as training.
- **Apartment floor.** Apartments under 10 triệu per m² are left out of the medians as likely monthly rents filed as sales, down-payment teasers or prices typed without their decimal. The rows stay in the database and on the listing pages. The floor is apartments only, because outer-district land and houses can sit under it for real.
- **Reposts.** Rows with the same price and area in one district and type count once. `listing_count` keeps every row, `distinct_count` and every median use the collapsed set.
- **Thin districts.** Under `MIN_SUMMARY_N` (10) distinct rows, quartiles are null and the page shows a count, never a median.
- **The page** is computed live from `Listing`, apartments only. Villas are left out because their price per m² is not comparable with an apartment's.
- **Snapshots.** `DistrictSnapshot` is unique on `(snapshot_date, source_site, district, property_type)` and stores every sale property type, not only apartments. Each source gets its own rows, so adding a source never moves another's series. The date is the Ho Chi Minh City calendar day, not the server's UTC day. A rerun on the same day replaces that day's rows for that source.
- **Blocked days.** A blocked crawl stamps no `last_seen_at`, so the 30-day window still holds the earlier rows. That day's snapshot and the live page repeat earlier figures. A day the machine is off has no snapshot at all, and nothing backfills it.

## Web layer

Routes: `/` (list with filters and search), `/listing/<id>/`, `/flagged/`, `/districts/`, `/offices/`, `/health/`, the admin at `ADMIN_URL`, `/api/`.

- The list, detail and flagged pages and the API filter on `PUBLIC_SOURCES`. `/districts/` uses `MARKET_SOURCES`. `/health/` lists every source's runs.
- `/health/` charts scrape and scoring runs from `analytics.py`, so a broken parser shows up as a drop in listings seen or a spike in errors.
- `/districts/` is the median asking price per m² by district, backed by `district_analytics.py`.
- `/offices/` is the office rent view, backed by `office_analytics.py`.
- The API is Django REST Framework with `django-filter`, 20 per page. `AnonRateThrottle` limits anonymous clients to 60 requests per hour, then returns HTTP 429. The count lives in the default local-memory cache, so each server process keeps its own. The output has no `agent` field, and `agent` is not a filter.
- Access: there are no user accounts, so the Django admin is the only login. The pages and the API are open.
- Public pages show no agent name and no phone number. Both stay in the database and the admin.
- Styling is Tailwind CSS 4 through `django-tailwind-cli`, a standalone binary with no Node.js. The compiled stylesheet is gitignored, so a deploy has to build it. Content scanning is an allowlist (`@source`), because automatic detection compiled selectors out of unrelated files.
- Static files are served by WhiteNoise.

## Deployment and scheduling

The plan is for the web app to deploy to Render and read a remote Postgres. It has not been deployed yet. The pipeline stays on a local Windows machine.

`run_scraper.ps1` runs four steps in order, with all output streams appended to one timestamped report:

1. `scrape_listings --source alonhadat`
2. `score_listings`
3. `scrape_listings --source homedy --pages 10`
4. `snapshot_districts`

Windows Task Scheduler fires it daily. The scraper stays local because it already works that way and moving it adds schedule risk for no gain. Run monitoring through `ScrapeRun` is the same either way.

The cost is gaps in `PriceHistory`, delisting detection and district snapshots when the machine is off or asleep. That is accepted until it causes a real problem.

Secrets and host settings come from environment variables through `python-decouple`. `DEBUG` defaults to off and `ALLOWED_HOSTS` defaults to empty, so a misconfigured deploy would fail closed. `ADMIN_URL` sets the admin path. It defaults to `admin/` and takes a trailing slash with no leading slash, like `your-admin-path/`. Render sets its own value.

## Known limits

Current figures for each of these are in the README's [Limits](README.md#limits) section. This list names the mechanism only, so it does not go stale.

- **Asking prices only.** Neither the model nor the district medians see what a property sold for.
- **Stale `is_active`.** Any `srp_` code blocks the sweep, and alonhadat runs keep logging one, so `is_active` overstates what is still listed. `/districts/` works around it with the 30-day window. The list pages do not.
- **Partial crawls.** Search pages give up on 429s and network errors, which ends a root early. A root that fails on its first page adds nothing to that run. README Limits names which roots.
- **House and villa prices.** The shipped model has no `property_type` column, so it prices a house or villa as an apartment of the same size in the same district.
- **Listing age.** `listing_age` saturates past the oldest training row.
- **homedy.** Posted dates are approximate, rows never delist, and there are no photos or agents.
- **Dead pages.** alonhadat can serve a dead LDP as a 200 with a placeholder shell. The parser detects it by title (`ldp_soft_gone`). A permanently dead page stays retry-eligible and spends LDP budget every run.
- **Posted dates.** alonhadat's `posted_date` appears to be a refresh date, so `days_on_market` can undercount.
- **Bot wall.** Extra manual crawl starts can re-trigger alonhadat's wall. Let the schedule run on its own.
- **Local gaps.** A sleeping or offline machine leaves holes in history and in the district snapshots.
- **Mixed fonts.** The site font lacks many Vietnamese diacritics, so the browser draws those from a fallback face.
