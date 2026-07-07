# SaiPrice — Technical Specification

This document is the single source of truth for how SaiPrice is built. It is written for an AI coding agent, not a human collaborator. Rules here are facts, not suggestions. Where the source plan (`combined-plan-2026-v47.html`) and this document conflict, this document wins — it resolves every ambiguity the plan left open.

Deadline: Standard scope must be deployed with a live public URL by **August 10, 2026**.

## 1. Project Overview

SaiPrice is a pricing-transparency pipeline for the Ho Chi Minh City residential property market. It scrapes public listings, stores them in Postgres, tracks price changes over time, estimates a fair price per listing with a regression model, and serves both a public REST API and a server-rendered dashboard.

It is not a lead-generation tool and does not claim to surface off-market inventory. It only ever sees what is publicly listed.

Current repo state (as of this document): Django project `saiprice` created, no apps yet. `requirements.txt` has `Django==5.2.15`, `psycopg2-binary`, `python-decouple`, `sqlparse`, `tzdata`. Postgres connection is wired through `python-decouple` reading `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` from `.env` (gitignored).

## 2. Scope Boundaries

### Standard (locked, ships by Aug 10)
- Single source: **batdongsan.com.vn** only.
- `property_type` in `{apartment, house, land, villa}`, `listing_intent` in `{sale, rent}`. `office` is a valid schema value but is never produced by the Standard scraper.
- Full pipeline: scraper → Postgres → Django backend + REST API → dashboard → ML price model → Render deployment → published research piece.
- Price history tracking, delisting detection, and anomaly flagging (price-gap, low-photo, stale-listing rules only — see §11) are in Standard scope. They are pipeline requirements, not stretch features.

### Stretch (Y12, not this summer, no build time until Standard ships)
- **maisonoffice.vn** scraping (`property_type=office`). The schema already accounts for it (`source_site` choices, `price_unit` column) but the Standard scraper never touches this site.
- District trend charts over time, price-per-sqm comparisons across districts, map view.
- First-vs-latest cumulative price-change stat (a mini trend feature) — no dashboard UI or API endpoint for this in Standard. The underlying data (`PriceHistory`) already supports it; it can be queried ad hoc when writing the research piece, but it is not a pipeline deliverable.

### Parked (no date, not scheduled anywhere)
- Hotel listings. Dynamic pricing requires a headless browser; structurally a different problem. Not part of this repo's roadmap.

### Explicitly out of scope, permanently (not "later," decided)
- **Cross-listing fuzzy duplicate detection.** The same physical unit reposted under a new `source_id` (different agent, refreshed listing) is treated as a distinct `Listing` row. Dedup is exact-match only on `(source_site, source_id)`. This is a documented limitation, not an oversight.
- **`individual_seller` / "no agency tag" anomaly signal.** batdongsan always exposes an `agent_name`/`agent_id_source`, whether the poster is an individual or an agency — there is no reliable field distinguishing the two. This signal is not computed. Do not add a heuristic for it without new data showing it's actually separable.
- **batdongsan's KYC-gated phone-reveal endpoint.** `phone_number` stays in the schema but is never populated for batdongsan — the real number sits behind a gated reveal mechanism that is batdongsan's own lead-gen monetization. Scraping it is out of scope on both complexity and ToS grounds, and the product doesn't need it.
- **ML accuracy floor.** "ML phase done" has no required minimum R²/RMSE. Whatever the better of the two candidate models achieves is what ships, and gets reported honestly in the research piece.

## 3. Tech Stack

- Python: scraping (`requests` + `beautifulsoup4`), ML (`scikit-learn`, `pandas`, `numpy`)
- PostgreSQL
- Django 5.2 + Django REST Framework + `django-filter` for the API
- Django templates + Tailwind CSS + Chart.js for the dashboard (server-rendered; Chart.js is largely unused in Standard since trend charts are Stretch)
- `whitenoise` for static file serving in production
- Render for deployment (web service) and the scheduled scraper (Cron Job — see §9)

Additions needed to `requirements.txt`: `beautifulsoup4`, `requests`, `scikit-learn`, `pandas`, `numpy`, `djangorestframework`, `django-filter`, `whitenoise`.

## 4. Repository / App Structure

One Django app: `listings`. Do not split into multiple apps — there is no team-scale or reuse reason to, and it only adds `INSTALLED_APPS`/import ceremony for a solo project.

```
saiprice/
  listings/
    models.py                          # Listing, PriceHistory, ScrapeRun, Agent
    admin.py                           # register all four models
    management/
      commands/
        scrape_batdongsan.py           # scraper entrypoint
        score_listings.py              # ML prediction + anomaly scoring, run after each scrape
        train_model.py                 # one-off/occasional, not scheduled
    scraping/
      client.py                        # HTTP session, retry/backoff, rate limiting
      parsers.py                       # batdongsan SRP/LDP field extraction
      currency.py                      # Vietnamese price-text -> whole VND
    api/
      serializers.py
      views.py
      urls.py
    views.py                           # plain Django views for the dashboard (template rendering)
    ml/
      train.py
      predict.py
      model.pkl                        # committed to repo after training, not retrained on deploy
    tests/
      test_currency.py
      test_scraping.py
      test_models.py
      test_api.py
  saiprice/
    settings.py
    urls.py
```

Dashboard views query the ORM directly. They do not call the app's own REST API internally — that would be an unnecessary self-HTTP round trip. The API in `listings/api/` exists as a separate, additional public interface.

Naming: snake_case for functions, variables, and fields (already fixed by the schema below). PascalCase singular for models (`Listing`, `PriceHistory`, `ScrapeRun`). Management commands are snake_case matching their file name.

Comments: none by default. Add one only where a non-obvious constraint, invariant, or workaround needs explaining — not to restate what the code already says.

Migrations: name them descriptively (`makemigrations listings --name add_tracking_fields`), not the auto-generated `0002_auto_20260706_1830` default.

## 5. Database Schema

### 5.1 `Listing`

| Field | Type | Required/Nullable | Notes |
|---|---|---|---|
| `id` | `BigAutoField` | required (PK) | |
| `source_site` | `CharField(max_length=20)`, choices `[batdongsan, maisonoffice]` | required | Standard scraper only ever writes `batdongsan` |
| `source_id` | `CharField(max_length=64)` | required | unique together with `source_site` |
| `url` | `URLField` | required, unique | |
| `title` | `CharField(max_length=255)` | required | |
| `category_id_source` | `IntegerField` | required | batdongsan's `cateId` from `pageTrackingData` |
| `property_type` | `CharField(max_length=20)`, choices `[apartment, house, land, villa, office]` | required | Standard never produces `office` |
| `project_name` | `CharField(max_length=255)` | nullable | |
| `project_id_source` | `CharField(max_length=64)` | nullable | |
| `listing_intent` | `CharField(max_length=10)`, choices `[sale, rent]` | required | |
| `is_verified` | `BooleanField(default=False)` | required | |
| `vip_type` | `CharField(max_length=50)` | nullable | Store the raw code from `pageTrackingData.products[0].vipType` as a string (e.g. `"0"`). If a visible display label exists on the page, a code-to-label mapping can live in the parser for display purposes, but the stored value stays the raw code — same convention as `specs_raw` for other messy source variation. Decided 2026-07-06. |
| `price` | `DecimalField(max_digits=15, decimal_places=0)` | nullable | Whole VND. Null when source shows "Thỏa thuận" (negotiable) |
| `price_unit` | `CharField(max_length=50)` | nullable | Unused for batdongsan (VND implicit); populated when maisonoffice scraping starts |
| `price_per_sqm` | `DecimalField(max_digits=15, decimal_places=0)` | nullable | Whole VND |
| `area_sqm` | `DecimalField(max_digits=10, decimal_places=2)` | nullable | |
| `bedrooms` | `IntegerField` | nullable | Absent for land/project listings — expected, not an error |
| `bathrooms` | `IntegerField` | nullable | Same as above |
| `address_raw` | `TextField` | nullable | Full string, district/ward parsed separately |
| `district_id_source` | `IntegerField` | nullable | Always populated for batdongsan from `pageTrackingData` |
| `ward_id_source` | `IntegerField` | nullable | Same source as above |
| `district` | `CharField(max_length=100)` | nullable | |
| `ward` | `CharField(max_length=100)` | nullable | |
| `specs_raw` | `JSONField` | nullable | Variable-length spec list (legal/interior/direction), stored as-is, not fixed columns |
| `description` | `TextField` | nullable | Markup stripped before storing |
| `images` | `JSONField` | nullable | List of image URLs |
| `video_url` | `URLField` | nullable | |
| `map_lat` | `DecimalField(max_digits=9, decimal_places=6)` | nullable | |
| `map_lng` | `DecimalField(max_digits=9, decimal_places=6)` | nullable | |
| `agent` | `ForeignKey(Agent, on_delete=SET_NULL)` | nullable | See §5.2. Almost always populated for batdongsan (§2), null only on a nullable-field parse failure (§8) |
| `phone_number` | `CharField(max_length=32)` | nullable | **Always null for batdongsan by design.** See §2. |
| `posted_date` | `DateField` | nullable | From "Ngày đăng" on the LDP |
| `scraped_at` | `DateTimeField(auto_now_add=True)` | required | Set once, at first insert. Never updated again. |
| `last_seen_at` | `DateTimeField` | required | Set explicitly in ingestion code on every scrape pass that confirms the listing still exists. **Not** `auto_now` — an automatic field would also fire when the ML job updates `predicted_price`, which is not a re-confirmation that the listing is still live. |
| `is_active` | `BooleanField(default=True)` | required | |
| `delisted_at` | `DateTimeField` | nullable | Set when `is_active` flips to `False` |
| `predicted_price` | `DecimalField(max_digits=15, decimal_places=0)` | nullable | Current-state ML output, overwritten each scoring run |
| `predicted_at` | `DateTimeField` | nullable | Timestamp of the last scoring run that touched this row |
| `is_anomaly` | `BooleanField(default=False)` | required | |
| `anomaly_reason` | `JSONField` | nullable | Dict of rule code → `{"triggered": bool, "value": ...}`. See §11 for exact shape. |

Constraints: `unique_together = (("source_site", "source_id"),)`. `url` has `unique=True`.

`days_on_market` and cumulative `price_change_pct` are **not columns**. See §5.5.

### 5.2 `Agent`

One row per distinct agent, deduplicated the same way as `Listing`: exact match on `(source_site, source_id)`, no fuzzy name matching (same philosophy as §2 and §7). Added so an agent's full listing history can be queried directly (`agent.listing_set.all()`) instead of filtering `Listing` on a repeated name string, and so the API can filter by agent identity rather than substring-matching a text field.

| Field | Type | Required/Nullable | Notes |
|---|---|---|---|
| `id` | `BigAutoField` | required (PK) | |
| `source_site` | `CharField(max_length=20)`, choices `[batdongsan, maisonoffice]` | required | Same choices as `Listing.source_site` |
| `source_id` | `CharField(max_length=64)` | required, unique together with `source_site` | batdongsan's agent ID from `pageTrackingData`. Named `source_id` (not `agent_id_source`) to mirror `Listing`'s own `(source_site, source_id)` pattern now that this is its own row, not a foreign value living on `Listing` |
| `name` | `CharField(max_length=255)` | nullable | Parse-failure fallback only, same rule as any other nullable text field (§8). batdongsan always exposes an agent name per §2, so null here should be rare |

Constraints: `unique_together = (("source_site", "source_id"),)`.

Not modeled: agency vs. individual seller distinction (§2, `individual_seller` is explicitly out of scope), agent contact fields beyond `name` (phone stays out per §2's KYC note), and any development/project entity. `project_name`/`project_id_source` stay flat fields on `Listing` — they were never raised as a filtering need the way agent was, and splitting them out is a separate decision, not a required consequence of this one. Revisit only if a real need for project-level querying shows up.

### 5.3 `PriceHistory`

One row per observed price change. Not a mirror of every scrape, only inserted when the price actually differs from what's currently stored (see §7 for the exact upsert sequence).

| Field | Type | Required/Nullable |
|---|---|---|
| `id` | `BigAutoField` | required (PK) |
| `listing` | `ForeignKey(Listing, on_delete=CASCADE)` | required |
| `price` | `DecimalField(max_digits=15, decimal_places=0)` | required |
| `price_per_sqm` | `DecimalField(max_digits=15, decimal_places=0)` | nullable |
| `observed_at` | `DateTimeField` | required |

A new `Listing` always gets exactly one `PriceHistory` row at insert time (its first observed price).

### 5.4 `ScrapeRun`

One row per scraper invocation. This is the primary way to notice the scraper silently broke (e.g. batdongsan changed its HTML structure) — check this table, don't rely on reading log files.

| Field | Type | Required/Nullable |
|---|---|---|
| `id` | `BigAutoField` | required (PK) |
| `started_at` | `DateTimeField` | required |
| `finished_at` | `DateTimeField` | nullable (null while the run is in progress) |
| `listings_seen` | `IntegerField(default=0)` | required |
| `inserted` | `IntegerField(default=0)` | required |
| `updated` | `IntegerField(default=0)` | required |
| `skipped` | `IntegerField(default=0)` | required |
| `error_count` | `IntegerField(default=0)` | required |

### 5.5 Computed values (not stored)

**`days_on_market`**: `(delisted_at or now()) - posted_date`. Implement as a queryset annotation using `Coalesce` + `ExpressionWrapper`, or a model property for single-instance use:

```python
from django.db.models import F, ExpressionWrapper, DurationField
from django.db.models.functions import Coalesce, Now

Listing.objects.annotate(
    days_on_market=ExpressionWrapper(
        Coalesce(F("delisted_at"), Now()) - F("posted_date"),
        output_field=DurationField(),
    )
)
```

If `posted_date` is null (parse miss — batdongsan's LDP reliably exposes it, so a null here is almost always a parsing failure, not a genuinely unknown date), fall back to the listing's earliest `PriceHistory.observed_at` instead of leaving `days_on_market` null. This gives an honest floor ("at least N days," possibly an undercount for a listing that existed before it was first scraped) rather than silently dropping the anomaly signal on exactly the listings — old, messy, individually-sold — that signal is meant to catch.

**`price_change_pct`** (used by the anomaly price-gap rule is a separate thing — see §11 — this is the general-purpose stat): previous-vs-latest, from the two most recent `PriceHistory` rows for a listing:

```python
def price_change_pct(listing):
    history = listing.pricehistory_set.order_by("-observed_at")[:2]
    if len(history) < 2:
        return None
    latest, previous = history
    return (latest.price - previous.price) / previous.price
```

## 6. Scraper Target and Depth

Single source: batdongsan.com.vn, HCMC listings, `property_type` in `{apartment, house, land, villa}`, `listing_intent` in `{sale, rent}`.

Every scrape run does a full crawl: paginate through the search-results pages (SRP) for the tracked scope, then visit the listing detail page (LDP) for every listing found — every pass, not just on first discovery. `address_raw`, `specs_raw`, `description`, `images`, `map_lat`/`map_lng`, and `phone_number`'s absence all require the LDP; SRP alone can't populate this schema. Always-fresh data and one code path outweigh the extra requests at this project's volume (hundreds to low thousands of listings).

### Fetch method

Plain `requests` + `beautifulsoup4`. No headless browser by default, and no bot-detection evasion of any kind under any circumstance — no stealth patches, no `navigator.webdriver` spoofing, nothing designed to make automated traffic pass as human. Decided 2026-07-06, final: actively defeating a site's anti-bot protection is a different act than reading a public page, and it sits past the line §1 draws for this project (public listings only, never a lead-gen tool). It's also a poor engineering bet on its own terms — a scraper that only works because it fools one detection version breaks the moment that version changes, on a schedule nobody controls.

LDP pages are confirmed working via plain `requests.get()` with a standard `User-Agent` header (verified 2026-07-06 against real listing pages). SRP (search-results) pages still need the same test, with `Accept-Language` and `Referer` headers added, before concluding plain requests can't reach them. Don't skip straight to browser automation on an assumption; confirm the plain-request path actually fails first.

If SRP genuinely cannot be reached without a browser, even a vanilla one with no stealth patches, that piece runs locally only, never as part of the Render deployment (§9) — a hard boundary, not a cost tradeoff.

### Currency parsing

batdongsan prices render as Vietnamese-unit text: `"8 tỷ"` = 8 billion VND, `"~129,03 triệu/m²"` = 129.03 million VND/sqm (comma is the decimal separator). Parse to whole VND integers:

```python
# listings/scraping/currency.py
import re
from decimal import Decimal

UNITS = {"tỷ": Decimal("1e9"), "triệu": Decimal("1e6")}

def parse_vnd(text: str) -> Decimal | None:
    if not text or "thỏa thuận" in text.lower():
        return None
    match = re.search(r"([\d,.]+)\s*(tỷ|triệu)", text)
    if not match:
        return None
    number = Decimal(match.group(1).replace(".", "").replace(",", "."))
    return (number * UNITS[match.group(2)]).quantize(Decimal("1"))
```

`price_unit` stays null/unused for batdongsan — VND is implicit for this source.

## 7. Deduplication and Upsert Semantics

Dedup key: `(source_site, source_id)`, exact match only. No fuzzy/cross-ID duplicate detection (see §2).

On each scrape pass, per listing:

1. Look up existing `Listing` by `(source_site, source_id)`.
2. If found: compare the newly parsed `price` to the **existing stored** `price` (before overwriting). If different, or if no `Listing` existed yet, insert a new `PriceHistory` row with `observed_at = now()`.
3. Overwrite every field on `Listing` with what was just parsed this pass — full overwrite, no per-field diffing. Set `last_seen_at = now()` and `is_active = True`.
4. If the required fields (`source_site`, `source_id`, `url`, `title`, `property_type`, `listing_intent`, `category_id_source`) failed to parse, do not save anything for this listing — increment `ScrapeRun.skipped`, log the URL and reason, move on. A nullable field failing to parse is not a skip condition; store it as null and proceed (self-heals next pass if the source is parseable again).

```python
def upsert(parsed):
    agent = None
    if parsed.agent_source_id:
        agent, _ = Agent.objects.update_or_create(
            source_site=parsed.source_site, source_id=parsed.agent_source_id,
            defaults={"name": parsed.agent_name},
        )

    existing = Listing.objects.filter(
        source_site=parsed.source_site, source_id=parsed.source_id
    ).first()
    if existing and parsed.price != existing.price:
        PriceHistory.objects.create(
            listing=existing, price=parsed.price,
            price_per_sqm=parsed.price_per_sqm, observed_at=timezone.now(),
        )
    listing, created = Listing.objects.update_or_create(
        source_site=parsed.source_site, source_id=parsed.source_id,
        defaults={**parsed.fields, "agent": agent, "last_seen_at": timezone.now(), "is_active": True},
    )
    if created:
        PriceHistory.objects.create(
            listing=listing, price=listing.price,
            price_per_sqm=listing.price_per_sqm, observed_at=timezone.now(),
        )
    return created
```

`parsed.fields` no longer includes `agent_name`/`agent_id_source` — those two are consumed above to resolve the `Agent` row, and the resulting `agent` object is passed into `Listing`'s own `defaults` separately.

### Delisting detection

Because every run does a full crawl of the tracked scope, a listing that has genuinely been removed simply won't be touched this run. At the end of `scrape_batdongsan`:

```python
Listing.objects.filter(
    source_site="batdongsan", is_active=True, last_seen_at__lt=run.started_at,
).update(is_active=False, delisted_at=run.finished_at)
```

A 404 on a previously-known LDP URL during the crawl is also treated as an immediate delisting signal (`is_active=False`, `delisted_at=now()`) for that listing, not a scrape failure.

`pageTrackingData.products[0].expired` is a real boolean field batdongsan exposes on every LDP, confirmed present in sample HTML pulled 2026-07-06. If `expired == true` on a listing the crawler visits, treat it the same as the 404 case: `is_active=False`, `delisted_at=now()`, not a parse failure and not skipped. This was previously an open question (§2's earlier draft flagged it and deferred); it's resolved now that real data confirms the field costs nothing extra to check, since `parsers.py` already reads this same JSON object for `category_id_source`/`district_id_source`/`ward_id_source`.

## 8. Error Handling

**Transient network failures** (timeout, connection error, 5xx) fetching a page: retry up to 3 times with exponential backoff (2s, 4s, 8s). After 3 failures, log the URL and error, skip that page/listing, continue the run.

```python
def fetch(url):
    for attempt in range(3):
        try:
            return session.get(url, timeout=10)
        except (Timeout, ConnectionError, HTTPError):
            time.sleep(2 ** attempt * 2)
    logger.error(f"gave up on {url} after 3 attempts")
    return None
```

**Rate limiting**: `time.sleep(random.uniform(1, 3))` between requests. Randomized, not fixed — avoids the perfectly-even request spacing that's an easy anti-bot fingerprint, for barely more code than a fixed delay.

**429 responses**: respect a `Retry-After` header if present; otherwise back off the same as a 5xx.

**404 on a listing URL**: not a failure — see delisting detection above.

**Required-field parse failure**: skip the listing entirely, `ScrapeRun.skipped += 1`, log URL + which field.

**Nullable-field parse failure**: store null, continue. Not counted as a skip or an error.

**Run tracking**: create a `ScrapeRun` row at the start of `scrape_batdongsan` (`started_at=now()`), update it at the end (`finished_at`, `listings_seen`, `inserted`, `updated`, `skipped`, `error_count`). This is how a structural break in batdongsan's HTML (e.g. a redesign that breaks every selector) becomes visible — check this table (via `/admin/`) periodically; a run with `listings_seen` near zero or `error_count` spiking relative to history means the site changed and the parser needs attention, not that the data is real.

## 9. Deployment

Must be fixed before deploying (standard practice, not a judgment call):
- `SECRET_KEY` from an env var via `python-decouple`, not the hardcoded value currently in `settings.py`.
- `DEBUG = config("DEBUG", default=False, cast=bool)`.
- `ALLOWED_HOSTS` from an env var (comma-split), including the Render-assigned domain.
- `whitenoise` added to `MIDDLEWARE` (right after `SecurityMiddleware`) and `requirements.txt` for static file serving — Render doesn't serve Django static files on its own.

Scraper scheduling: **Render Cron Job** running `python manage.py scrape_batdongsan` on a daily schedule, followed by `python manage.py score_listings`. Verify Render's current Cron Job pricing before relying on this — not confirmed as free. If cost is prohibitive, documented fallback is Windows Task Scheduler on the local machine pointed at the same remote Postgres instance (accepts the risk of gaps in `PriceHistory`/delisting detection when the machine is off).

Separately from cost: if §6's fetch-method validation finds SRP crawling needs a browser at all, that piece runs locally only, never on Render, regardless of price. Stealth-patched bot-detection evasion is out of scope entirely (§6); a vanilla headless browser is the ceiling of what's on the table, and even that only runs from the local machine until proven otherwise.

ML model: trained locally via `train_model`, the resulting `model.pkl` is committed to the repo (`listings/ml/model.pkl`), loaded at Django startup. Not retrained automatically as part of deployment.

## 10. API

Django REST Framework + `django-filter`. Dashboard templates query the ORM directly and do not call this API internally.

- `GET /api/listings/` — list, filterable by `district`, `property_type`, `listing_intent`, `min_price`/`max_price`, `min_area`/`max_area`, `is_anomaly`, `agent`; orderable; `PageNumberPagination`, `page_size=20`.
- `GET /api/listings/<id>/` — detail.

```python
class ListingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Listing
        fields = "__all__"

class ListingListView(generics.ListAPIView):
    queryset = Listing.objects.filter(is_active=True)
    serializer_class = ListingSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["district", "property_type", "listing_intent", "is_anomaly", "agent"]
    pagination_class = PageNumberPagination
```

No dedicated filter-options-discovery endpoint (e.g. a `/districts/` list) — the dashboard's own filter sidebar queries `Listing.objects.values_list("district", flat=True).distinct()` directly from its own view; API consumers filter by value directly.

## 11. Anomaly Detection

Computed by `score_listings`, run after every scrape, over all `is_active=True` listings with both `price` and `predicted_price` populated. Requires at least 20 such listings to compute the price-gap percentile meaningfully — below that, skip the price-gap rule for this run (too small a sample for a percentile to mean anything) but still compute the other two rules.

Three rules, each independently computed:

1. **`price_gap`**: `(predicted_price - price) / price`. Flag if in the top 5% of this ratio across all qualifying active listings this run (recomputed each run with `numpy.percentile`, not a fixed cutoff — there's no data yet to justify a fixed percentage, and this adapts to whatever the model's real accuracy turns out to be).
2. **`low_photos`**: `len(images or []) < 3`.
3. **`stale_listing`**: `days_on_market > 90` (see §5.5 for the computed value, including its `posted_date`-null fallback).

`individual_seller` is explicitly not a rule — see §2.

`is_anomaly = True` if any rule trips. `anomaly_reason` shape (all three rules always present, so the "why flagged" dashboard tooltip can show the full picture, not just the triggered ones):

```json
{
  "price_gap": {"triggered": true, "value": 0.23},
  "low_photos": {"triggered": false, "value": 6},
  "stale_listing": {"triggered": true, "value": 104}
}
```

`predicted_at` is set to the scoring run's timestamp whenever a listing's `predicted_price`/`is_anomaly`/`anomaly_reason` are (re)computed.

## 12. ML Model

Two candidates: linear regression and random forest (`scikit-learn`), trained on `district`, `area_sqm`, `property_type` at minimum. Pick whichever has the better RMSE/R² on a held-out test split. No fixed accuracy floor (see §2) — whatever it achieves ships, and gets reported honestly in the research piece.

Serialize with `pickle` (not `joblib`) per the existing plan. `train_model` is a manual/occasional command, not part of the scheduled pipeline — retrain by re-running it and committing the new `model.pkl` when there's meaningfully more data.

## 13. Testing

Django's built-in `TestCase` (`python manage.py test`) — ships with Django, zero new dependencies, no reason to add `pytest` for this project's size.

Required coverage for "done," not full coverage everywhere:
- Currency parsing (`parse_vnd`), including the negotiable-price-returns-null case.
- Upsert/dedup logic (`update_or_create` path creates exactly one `PriceHistory` row on price change, zero on no change).
- Agent dedup: two listings from the same `(source_site, source_id)` agent reuse one `Agent` row, not two.
- `days_on_market` computed value, including the `posted_date`-null fallback path.
- Anomaly rule computation (all three rules, both triggered and not-triggered cases).

Full view/endpoint test coverage and ML internals are not required to consider a phase done.

## 14. Definition of Done, Per Phase

**Scraper**
- `python manage.py scrape_batdongsan` runs end-to-end unattended.
- Full SRP crawl of the tracked scope + LDP visit per listing, every run.
- Retry/backoff and rate limiting implemented as specified in §8.
- `ScrapeRun` row created and updated every run.
- `(source_site, source_id)` uniqueness enforced at the DB level.
- Delisting detection working: previously-active listings not touched in a run flip to `is_active=False`. Same for a 404 on a known LDP URL and for `pageTrackingData.products[0].expired == true`.
- Fetch method matches §6 exactly: plain `requests` where it works, local-only browser (no stealth, no evasion) only where proven necessary.
- `PriceHistory` row inserted on every detected price change, and on first insert.
- Has run successfully at least once against the live site producing real data (not seed/fixture data).
- Running on a schedule unattended (Render Cron Job or the documented fallback), not only ever run by hand.

**Database**
- `makemigrations` + `migrate` runs clean from an empty database.
- `Listing`, `PriceHistory`, `ScrapeRun`, `Agent` match §5 exactly — field names, types, nullability, constraints.
- All four models registered in `admin.py` so the data is inspectable without writing queries.

**Backend + API**
- DRF installed and configured.
- `GET /api/listings/` and `GET /api/listings/<id>/` live, filtering/pagination/ordering working as specified in §10.
- Endpoints return real data from the live DB, not fixtures.
- Tests from §13 passing.

**Frontend dashboard**
- Listing list page: filter sidebar (district, property_type, price range) + search, paginated, mobile-responsive.
- Listing detail page: full listing info, `predicted_price` shown alongside `price`, anomaly explanation rendered from `anomaly_reason`.
- Summary view showing currently-flagged (`is_anomaly=True`) active listings.

**ML price model**
- Both candidate models trained on real scraped data.
- Better model selected by RMSE/R², reported honestly — no required floor.
- `model.pkl` committed to the repo.
- `predicted_price`/`predicted_at` populated on active listings via `score_listings`.

**Deployment**
- Live public URL on Render.
- `DEBUG=False`, `SECRET_KEY`/DB credentials from env, `ALLOWED_HOSTS` set, static files served via `whitenoise`.
- Scraper running on schedule in production.
- Site reflects real scraped data (not seed/demo data) when checked against the Aug 10 deadline.

**Research writeup**
- Not a code deliverable. No engineering definition-of-done applies here — tracked in the plan, not in this document.
