# SaiPrice — Technical Specification

This document is the single source of truth for how SaiPrice is built. It is written for an AI coding agent, not a human collaborator. Rules here are facts, not suggestions. Where the source plan (`combined-plan-2026-v47.html`) and this document conflict, this document wins — it resolves every ambiguity the plan left open.

Deadline: Standard scope must be deployed with a live public URL by **August 10, 2026**.

## 1. Project Overview

SaiPrice is a pricing-transparency pipeline for the Ho Chi Minh City residential property market. It scrapes public listings, stores them in Postgres, tracks price changes over time, estimates a fair price per listing with a regression model, and serves both a public REST API and a server-rendered dashboard.

It is not a lead-generation tool and does not claim to surface off-market inventory. It only ever sees what is publicly listed.

Current repo state (as of this document): Django project `saiprice` created, no apps yet. `requirements.txt` has `Django==5.2.15`, `psycopg2-binary`, `python-decouple`, `sqlparse`, `tzdata`. Postgres connection is wired through `python-decouple` reading `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` from `.env` (gitignored).

## 2. Scope Boundaries

### Standard (locked, ships by Aug 10)
- Primary automated source: **alonhadat.com.vn**. Secondary automated source: **homedy.com**, scraped the same way, for wider coverage and as a cross-check on alonhadat's price/field parsing where a listing happens to appear on both — still exact `(source_site, source_id)` dedup per listing, never a cross-source merge (§7). Both confirmed reachable via plain `requests` on SRP and LDP, no browser, no bot challenge — see §6.
- **batdongsan.com.vn dropped as the automated source.** Confirmed Cloudflare-gated on every navigation (§6); that finding is unchanged. `ingest_saved_listings` (manual HTML capture) stays in the repo as a rare, opportunistic fallback only — unscheduled, not relied on for volume, not required by any phase's Definition of Done (§15). Decided 2026-07-10: kept rather than deleted because it's already built and tested and costs nothing to leave dormant, not because it's still a launch-data source.
- `property_type` in `{apartment, house, land, villa}`, `listing_intent` in `{sale, rent}`. `office` is a valid schema value but is never produced by the Standard scraper.
- Full pipeline: scraper → Postgres → Django backend + REST API → dashboard → ML price model → Render deployment → published research piece.
- Price history tracking, delisting detection, and anomaly flagging (price-gap, low-photo, stale-listing rules only — see §12) are in Standard scope. They are pipeline requirements, not stretch features.

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

- Python: scraping (`requests` + `beautifulsoup4` for both SRP and LDP on alonhadat.com.vn and homedy.com — both confirmed to serve full listing HTML to a plain request, no browser needed, see §6), ML (`scikit-learn`, `pandas`, `numpy`)
- PostgreSQL
- Django 5.2 + Django REST Framework + `django-filter` for the API
- Django templates + Tailwind CSS 4 via `django-tailwind-cli` (standalone binary — no Node.js/npm anywhere in the project) + Chart.js for the dashboard (server-rendered; Chart.js is largely unused in Standard since trend charts are Stretch). Theme tokens per §11; `{% tailwind_css %}` loaded in `base.html`. Installed and verified live 2026-07-15
- `whitenoise` for static file serving in production
- Render for deployment of the web service and API only. The scraper (SRP fetch + LDP fetch + DB write) runs entirely on the local machine via Windows Task Scheduler, not on Render — see §9 for why, now that the original browser-dependency rationale no longer applies.

Additions needed to `requirements.txt`: `beautifulsoup4`, `requests`, `scikit-learn`, `pandas`, `numpy`, `djangorestframework`, `django-filter`, `whitenoise`. `django-tailwind-cli` added 2026-07-15 (§11). `playwright` is no longer needed anywhere in the pipeline — no remaining source requires a browser (§6). Removed from `requirements.txt` 2026-07-14 together with the dead `scrape_batdongsan` command, its last dependent.

## 4. Repository / App Structure

One Django app: `listings`. Do not split into multiple apps — there is no team-scale or reuse reason to, and it only adds `INSTALLED_APPS`/import ceremony for a solo project.

```
saiprice/
  listings/
    models.py                          # Listing, PriceHistory, ScrapeRun, Agent
    admin.py                           # register all four models
    management/
      commands/
        scrape_listings.py             # scraper entrypoint, takes --source (alonhadat|homedy)
        ingest_saved_listings.py       # manual batdongsan HTML fallback, unscheduled — see §6
        score_listings.py              # ML prediction + anomaly scoring, run after each scrape
        train_model.py                 # one-off/occasional, not scheduled
    scraping/
      client.py                        # HTTP session, retry/backoff, rate limiting
      parsers.py                       # ParsedListing + shared helpers; batdongsan LDP extraction, manual-fallback path only
      sites/
        alonhadat.py                   # alonhadat SRP/LDP field extraction
        homedy.py                      # homedy SRP/LDP field extraction
      currency.py                      # Vietnamese price-text -> whole VND, shared across all three sites
    api/
      serializers.py
      views.py
      urls.py
    views.py                           # plain Django views for the dashboard (template rendering)
    templates/
      base.html                        # single base template, loads {% tailwind_css %} — see §11
      listings/
        listing_list.html
    ml/
      train.py
      predict.py
      model.pkl                        # committed to repo after training, not retrained on deploy
    tests/
      test_currency.py
      test_scraping.py
      test_models.py
      test_api.py
      test_views.py
      test_ingest.py
  saiprice/
    settings.py
    urls.py
  tailwind_src/
    source.css                         # only hand-written CSS in the project — see §11
  assets/
    css/
      tailwind.css                     # compiled output, gitignored — see §11
```

Dashboard views query the ORM directly. They do not call the app's own REST API internally — that would be an unnecessary self-HTTP round trip. The API in `listings/api/` exists as a separate, additional public interface.

Naming: snake_case for functions, variables, and fields (already fixed by the schema below). PascalCase singular for models (`Listing`, `PriceHistory`, `ScrapeRun`). Management commands are snake_case matching their file name.

Comments: none by default. Add one only where a non-obvious constraint, invariant, or workaround needs explaining — not to restate what the code already says.

Migrations: name them descriptively (`makemigrations listings --name add_tracking_fields`), not the auto-generated `0002_auto_20260706_1830` default.

Commit messages: never include any affiliation with Claude, Anthropic, Sonnet, Opus, or Fable, and never add any of them as a contributor, co-author, or commit trailer (e.g. `Co-Authored-By: Claude ...`). This applies to every commit, regardless of what tool wrote the code.

## 5. Database Schema

### 5.1 `Listing`

| Field | Type | Required/Nullable | Notes |
|---|---|---|---|
| `id` | `BigAutoField` | required (PK) | |
| `source_site` | `CharField(max_length=20)`, choices `[alonhadat, homedy, batdongsan, maisonoffice]` | required | Standard scraper writes `alonhadat`/`homedy`; `batdongsan` only from the manual `ingest_saved_listings` fallback (§6); `maisonoffice` is Stretch |
| `source_id` | `CharField(max_length=64)` | required | unique together with `source_site` |
| `url` | `URLField(max_length=500)` | required, unique | Default Django max_length of 200 is too short for real batdongsan LDP URLs. Confirmed and fixed 2026-07-07. |
| `title` | `CharField(max_length=255)` | required | |
| `category_id_source` | `IntegerField` | nullable | Populated only via the batdongsan manual-fallback path (`pageTrackingData.cateId`). Null for alonhadat/homedy until each site's real category signal is confirmed (§6) — `property_type` is the required, source-agnostic category signal instead (§7). Whether this field stays an `IntegerField` once alonhadat/homedy's signal is confirmed (it may turn out to be a slug string, not an int) is undecided — this fix only changes nullability, not type |
| `property_type` | `CharField(max_length=20)`, choices `[apartment, house, land, villa, office]` | required | Standard never produces `office` |
| `project_name` | `CharField(max_length=255)` | nullable | |
| `project_id_source` | `CharField(max_length=64)` | nullable | |
| `listing_intent` | `CharField(max_length=10)`, choices `[sale, rent]` | required | |
| `is_verified` | `BooleanField(default=False)` | required | |
| `vip_type` | `CharField(max_length=50)` | nullable | Store the raw code from `pageTrackingData.products[0].vipType` as a string (e.g. `"0"`). If a visible display label exists on the page, a code-to-label mapping can live in the parser for display purposes, but the stored value stays the raw code — same convention as `specs_raw` for other messy source variation. Decided 2026-07-06. |
| `price` | `DecimalField(max_digits=15, decimal_places=0)` | nullable | Whole VND. Null when source shows "Thỏa thuận" (negotiable) |
| `price_unit` | `CharField(max_length=50)` | nullable | Populated at ingest since 2026-07-14 with the source's price-magnitude text, normalized to `tỷ`/`triệu` (`tr` maps to `triệu`); null when `price` is null, and stays null on rows ingested earlier. Display formatting does not depend on it — see `price_display` (§5.5). maisonoffice's real unit semantics (Stretch) unchanged |
| `price_per_sqm` | `DecimalField(max_digits=15, decimal_places=0)` | nullable | Whole VND |
| `area_sqm` | `DecimalField(max_digits=10, decimal_places=2)` | nullable | |
| `bedrooms` | `IntegerField` | nullable | Absent for land/project listings — expected, not an error |
| `bathrooms` | `IntegerField` | nullable | Same as above |
| `address_raw` | `TextField` | nullable | Full string, district/ward parsed separately (see derivation rule below) |
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
| `anomaly_reason` | `JSONField` | nullable | Dict of rule code → `{"triggered": bool, "value": ...}`. See §12 for exact shape. |

Constraints: `unique_together = (("source_site", "source_id"),)`. `url` has `unique=True`.

Several notes in the table above — `category_id_source`'s `pageTrackingData` origin, `vip_type`'s raw-code convention, `agent` being "almost always populated," `phone_number` being always null, the district/ward derivation rule below — describe **batdongsan's** HTML specifically and stay accurate only for the dormant manual-fallback path. None of it automatically carries over to alonhadat/homedy. Before `scraping/sites/alonhadat.py`/`homedy.py` are written, each site's own equivalents need confirming against real HTML, not assumed from batdongsan: how it signals category/property type (alonhadat's URL slugs — `/ban-can-ho-chung-cu`, `/ban-biet-thu`, `/ban-dat` — look like a clean signal but this is unconfirmed), its address-string format, whether it gates agent phone numbers the way batdongsan's KYC reveal does or exposes them directly (if either doesn't gate it, `phone_number` becomes populatable for that source — a real difference from the permanent batdongsan null in §2), and whether it exposes a stable per-agent identifier at all versus just a name and phone per listing.

`district`/`ward` derivation rule, confirmed against real HTML 2026-07-07: split `address_raw` on commas, strip whitespace from each segment. `district` is the second-to-last segment, `ward` is the third-to-last. This holds regardless of the literal admin-unit word used (`Quận N`, `Thành phố Thuận An`, etc. all land correctly by position). Verified against both attached LDP samples, including a listing where the district-level segment reads `Thành phố Thuận An` rather than the more common `Quận N` pattern. This is a heuristic on address text, not a lookup against `district_id_source`/`ward_id_source` — if batdongsan ever changes its address-string ordering, this breaks silently. No ID-to-name lookup table exists yet; revisit if address formats prove inconsistent across a larger real crawl.

`days_on_market` and cumulative `price_change_pct` are **not columns**. See §5.5.

### 5.2 `Agent`

One row per distinct agent, deduplicated the same way as `Listing`: exact match on `(source_site, source_id)`, no fuzzy name matching (same philosophy as §2 and §7). Added so an agent's full listing history can be queried directly (`agent.listing_set.all()`) instead of filtering `Listing` on a repeated name string, and so the API can filter by agent identity rather than substring-matching a text field.

| Field | Type | Required/Nullable | Notes |
|---|---|---|---|
| `id` | `BigAutoField` | required (PK) | |
| `source_site` | `CharField(max_length=20)`, choices `[alonhadat, homedy, batdongsan, maisonoffice]` | required | Same choices as `Listing.source_site` |
| `source_id` | `CharField(max_length=64)` | required, unique together with `source_site` | batdongsan's agent ID from `pageTrackingData`. Named `source_id` (not `agent_id_source`) to mirror `Listing`'s own `(source_site, source_id)` pattern now that this is its own row, not a foreign value living on `Listing` |
| `name` | `CharField(max_length=255)` | nullable | Parse-failure fallback only, same rule as any other nullable text field (§8). batdongsan always exposes an agent name per §2, so null here should be rare |

Constraints: `unique_together = (("source_site", "source_id"),)`.

Not modeled: agency vs. individual seller distinction (§2, `individual_seller` is explicitly out of scope), agent contact fields beyond `name` (phone stays out per §2's KYC note), and any development/project entity. `project_name`/`project_id_source` stay flat fields on `Listing` — they were never raised as a filtering need the way agent was, and splitting them out is a separate decision, not a required consequence of this one. Revisit only if a real need for project-level querying shows up.

The `source_id`/`name` notes above describe batdongsan's `pageTrackingData`-based agent exposure specifically. Whether alonhadat/homedy expose a comparably stable per-agent identifier, versus just a name and phone number repeated per listing, is unconfirmed — check before assuming `Agent` dedup works identically for those two sources.

### 5.3 `PriceHistory`

One row per observed price change. Not a mirror of every scrape, only inserted when the price actually differs from what's currently stored (see §7 for the exact upsert sequence).

| Field | Type | Required/Nullable |
|---|---|---|
| `id` | `BigAutoField` | required (PK) |
| `listing` | `ForeignKey(Listing, on_delete=CASCADE)` | required |
| `price` | `DecimalField(max_digits=15, decimal_places=0)` | nullable | Matches `Listing.price`'s own nullability — a listing can be re-observed while still showing "Thỏa thuận" (negotiable). Fixed 2026-07-13: §7's `upsert()` had no null-price guard, and this field was previously `required`, which would raise `IntegrityError` the first time a negotiable listing got scraped. Making this nullable, rather than adding a skip branch to `upsert()`, was chosen to preserve the "a new `Listing` always gets exactly one `PriceHistory` row at insert time" invariant below, which `days_on_market`'s `posted_date`-null fallback (§5.5) depends on. |
| `price_per_sqm` | `DecimalField(max_digits=15, decimal_places=0)` | nullable |
| `observed_at` | `DateTimeField` | required |

A new `Listing` always gets exactly one `PriceHistory` row at insert time (its first observed price).

### 5.4 `ScrapeRun`

One row per scraper invocation, scoped to a single source — one run = one source, `scrape_listings` is invoked once per source (§9). This is the primary way to notice a site's parser silently broke (e.g. alonhadat changed its HTML structure) — check this table, don't rely on reading log files. `source_site` was added 2026-07-10 when the pipeline moved from one automated source to two (§6); collapsing both sites' numbers into one row would hide a break in either parser behind the other's healthy numbers, defeating the point of this table.

| Field | Type | Required/Nullable |
|---|---|---|
| `id` | `BigAutoField` | required (PK) |
| `source_site` | `CharField(max_length=20)`, choices `[alonhadat, homedy, batdongsan]` | required |
| `started_at` | `DateTimeField` | required |
| `finished_at` | `DateTimeField` | nullable (null while the run is in progress) |
| `listings_seen` | `IntegerField(default=0)` | required |
| `inserted` | `IntegerField(default=0)` | required |
| `updated` | `IntegerField(default=0)` | required |
| `skipped` | `IntegerField(default=0)` | required |
| `error_count` | `IntegerField(default=0)` | required |
| `posted_date_nulls` | `IntegerField(default=0)` | required |

`posted_date_nulls` (added 2026-07-14) counts this run's listings that parsed with a null `posted_date`. A date-label/markup rename on the source nulls the field fleet-wide through §8's silent nullable-field path — no error, no skip, so `error_count` never shows it, and §7's full overwrite wipes previously-good dates within one run. At end of run, `scrape_listings` warns when the null rate passes 80%, unless the source's prior finished run was already past 80% (a fresh break warns once; a chronically date-less source doesn't warn forever). Run-level pipeline health only — deliberately not a fourth §12 anomaly rule, which would flag every listing in a broken run.

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

**`price_change_pct`** (used by the anomaly price-gap rule is a separate thing — see §12 — this is the general-purpose stat): previous-vs-latest, from the two most recent `PriceHistory` rows for a listing:

```python
def price_change_pct(listing):
    history = listing.pricehistory_set.order_by("-observed_at")[:2]
    if len(history) < 2:
        return None
    latest, previous = history
    return (latest.price - previous.price) / previous.price
```

**`price_display`** (added 2026-07-14): human-readable price, a `Listing` property exposed as a read-only field on the API serializer. Computed from `price` directly — deliberately not from `price_unit`, so rows ingested before `price_unit` was populated render too. Null `price` → null; `price >= 1_000_000_000` → `X.XX tỷ`; below → `X.XX triệu`; trailing zeros stripped (`8 tỷ`, `8.5 tỷ`, `999 triệu`). `min_price`/`max_price` filtering stays raw whole-VND integers — the shipped filter contract is unchanged.

## 6. Data Acquisition

Automated sources: **alonhadat.com.vn** (primary) and **homedy.com** (secondary), HCMC listings, `property_type` in `{apartment, house, land, villa}`, `listing_intent` in `{sale, rent}`. Manual fallback: batdongsan.com.vn, rare and opportunistic only — see below.

### Automated crawling: batdongsan dead, alonhadat/homedy confirmed working

batdongsan is dead for automation, confirmed live 2026-07-07, finding unchanged:

- Plain `requests` with realistic headers (`User-Agent`, `Accept-Language`, `Referer`): 403, a Cloudflare managed JS challenge (`title: Just a moment...`), on SRPs and on LDPs that previously worked.
- Headless browser: never clears the challenge.
- Headed vanilla Playwright (no stealth): the first navigation passes, then Cloudflare blocks every subsequent navigation in the same session **regardless of pacing** — verified live with 20-second gaps between pages; every page after the first still failed.

The 2026-07-06 no-evasion decision stands: no stealth patches, no `navigator.webdriver` spoofing, nothing designed to make automated traffic pass as human. This is why batdongsan is dropped rather than fought — clearing that challenge means exactly the automated-traffic-passing-as-human behavior §1 rules out, not a parsing problem.

alonhadat.com.vn and homedy.com, by contrast, are confirmed live 2026-07-10 with plain `requests` and a realistic `User-Agent`/`Accept-Language` header — no browser, no stealth, nothing beyond what §8's rate limiting already calls for:

- alonhadat SRP (`/can-ban-nha-dat/ho-chi-minh`) and a real LDP linked from it: both `200`, full server-rendered HTML, no Cloudflare/captcha markers. Listing cards carry schema.org microdata (`itemprop="price"`, `itemprop="floorSize"`, etc.) — a genuine parsing convenience over batdongsan's inline JSON blob.
- homedy SRP (`/ban-nha-dat-tp-ho-chi-minh`) and a real LDP linked from it: both `200`, full server-rendered HTML, no Cloudflare/captcha markers. A `recaptcha/api.js` tag is present but inert on these pages — it's a defensive include for the site's own login form, not an active challenge; both requests returned complete listing content without solving anything.

This was one manual request per page, not a sustained crawl — §8's retry/backoff and randomized rate limiting still apply once `scrape_listings` runs for real, and "has run successfully at least once against the live site" (§15) is the actual bar, not this spot-check. Update: alonhadat's SRP reachability was interrupted by the 2026-07-10 robot-verify escalation, then re-confirmed clean by the 2026-07-13 ten-page re-test — this finding is current state again for SRP crawling. LDP-side caution still applies; full dated history in the `Agent.source_id` bullet below.

Not yet confirmed, and needed before `scraping/sites/alonhadat.py`/`homedy.py` are written — do not assume these mirror batdongsan:
- Each site's exact category/property-type signal. batdongsan used `pageTrackingData.products[0].cateId`; alonhadat's URL slugs look like a clean signal (`/ban-can-ho-chung-cu`, `/ban-biet-thu`, `/ban-dat`), but this needs confirming against a full category list, not assumed from three examples.
- Each site's address-string format for the district/ward derivation rule (§5.1) — batdongsan's comma-split-by-position rule is specific to batdongsan.
- Whether either site gates agent phone numbers the way batdongsan's KYC reveal does, or exposes them directly. If either doesn't gate it, `phone_number` becomes populatable for that source — a real difference from the permanent batdongsan null in §2.
- `Agent.source_id` for alonhadat: **confirmed 2026-07-10** — every SRP card carries a `data-memberid` attribute, used directly as `Agent.source_id`. Agent rows now populate for alonhadat. `name` stays null at SRP level (the card doesn't expose it) — standard nullable-field handling (§8), not a skip. Blocking/VIP status as of 2026-07-10: page-1 `vip-N` SRP cards can be cross-category injections, so `property_type` on VIP rows is mislabel-suspect until LDP-verified — the diagnostic query (alonhadat rows with `vip_type` non-null) counts **18** of 90 alonhadat rows. Bulk LDP verification triggered alonhadat's IP-scoped robot-verify wall (HTTP 200 redirect to `/xac-thuc-nguoi-dung.html`). The LDP-volume side is mitigated, done and verified: `scrape_listings` caps LDP visits per run via `--max-ldp-visits` (default 20), `--no-ldp-enrich` skips LDP enrichment entirely, and `client.fetch` detects the challenge URL and returns None rather than retrying (no-evasion rule above). Later on 2026-07-10 the wall escalated: the identical two-page SRP pagination test that previously passed now served the challenge on `/trang-2` (page 1 still passed), so SRP crawling was blocked too, not just LDP enrichment. Root cause was never confirmed. The scraper was paused pending a 48–72h cooldown, no retries while the flag was active. **Re-tested 2026-07-13, cooldown elapsed**: a 10-page SRP pagination test (`/can-ban-can-ho-chung-cu/ho-chi-minh`, pages 1–10) via `client.fetch` at §8's exact rate limiting (`random.uniform(1, 3)`s between requests, no other pacing change) came back clean — all 10 pages HTTP 200 with real listing cards (20/page), no `xac-thuc-nguoi-dung` challenge marker on any page or redirect. The SRP-side block is resolved as of this date; treat the 2026-07-10 escalation as a past incident, not current state. The pause is lifted for SRP crawling. LDP-side risk is unchanged and untested by this probe — the `--max-ldp-visits` cap and `--no-ldp-enrich` mitigations above still apply, and bulk LDP verification is still the specific behavior that triggered the original wall.
- Whether homedy exposes anything usable as `Agent.source_id` (§5.2) — still unconfirmed; check this together with the phone-gating question above, not separately. If homedy exposes its agent phone number directly (not gated), that phone number is the candidate stable identifier for `Agent.source_id`. If it exposes nothing stable (no ID, no direct phone, nothing else identifying), `Agent` creation for homedy stays permanently skipped by the existing `upsert()` guard (`if parsed.agent_source_id:` — §7) — a graceful degradation to "no agent linked" per listing, not a bug. But it does mean `Agent`-based querying and API filtering (`agent.listing_set.all()`, `?agent=` on `/api/listings/`, §10) excludes homedy until/unless it turns out to expose something stable. No synthetic ID scheme is being invented to paper over this.
- Each site's own listing-removed/expired signal (a 404, a status banner, something else) — batdongsan's `pageTrackingData.products[0].expired` field doesn't exist on other sites. See §7.
- Whether alonhadat/homedy have any equivalent to batdongsan's `is_verified` badge concept. Lower stakes than the above: `is_verified` defaults to `False` (§5.1) and isn't in §7's required-field list, so an unconfirmed/unmapped source just reads as unverified until this is checked — not a skip, not a crash.

### Manual fallback: `ingest_saved_listings`, batdongsan only

batdongsan.com.vn stays reachable exactly one way: a human browses it normally and saves individual LDPs as "HTML Only." `ingest_saved_listings <folder>` reads every file in the folder (no filename convention), parses each with the existing `parse_ldp`, and upserts through the same verified sequence in `listings/upsert.py` the crawler uses (Agent resolution, PriceHistory on first insert and on price change, null-price guard). One bad file logs as an error and never stops the batch. Property type comes from each file's own `cateId` via `parse_ldp`, never from folder structure.

This is no longer the launch data path — alonhadat/homedy automation is. It's a rare, opportunistic top-up when a specific batdongsan listing is worth having and nothing else covers it: not scheduled, not weekly, not required by any phase's Definition of Done (§15).

Consequences, accepted:
- Coverage is partial by definition. A listing absent from a folder means nothing, so `ingest_saved_listings` performs no delisting sweep and never flips `is_active`. Delisting detection (§7) only runs against sources with full-coverage automated crawls — batdongsan is permanently excluded from it now, not just dormant.
- A saved file with `expired == true` is skipped and counted, not ingested and not delisted.

### Currency parsing

Vietnamese-site prices render as unit text: `"8 tỷ"` = 8 billion VND, `"~129,03 triệu/m²"` = 129.03 million VND/sqm (comma is the decimal separator). Confirmed the same convention on alonhadat — `"26 tỷ"` seen directly in a live SRP fetch 2026-07-10 — so `parse_vnd` below is shared across all three sites' parsers, not rewritten per site. Parse to whole VND integers:

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

Reversed 2026-07-14 (previously "stays null/unused, reserved for maisonoffice"): `price_unit` is now populated at ingest for every source from the matched unit text, via `parse_vnd_unit` in `currency.py` — it shares `parse_vnd`'s exact match so price and unit can never disagree, and normalizes `tr` to `triệu`. `price` itself stays normalized whole VND for all sources; maisonoffice's Stretch semantics (a real per-period unit string) are unchanged.

## 7. Deduplication and Upsert Semantics

Dedup key: `(source_site, source_id)`, exact match only. No fuzzy/cross-ID duplicate detection (see §2).

On each scrape pass, per listing:

1. Look up existing `Listing` by `(source_site, source_id)`.
2. If found: compare the newly parsed `price` to the **existing stored** `price` (before overwriting). If different, or if no `Listing` existed yet, insert a new `PriceHistory` row with `observed_at = now()`.
3. Overwrite every field on `Listing` with what was just parsed this pass — full overwrite, no per-field diffing. Set `last_seen_at = now()` and `is_active = True`.
4. If the required fields (`source_site`, `source_id`, `url`, `title`, `property_type`, `listing_intent`) failed to parse, do not save anything for this listing — increment `ScrapeRun.skipped`, log the URL and reason, move on. A nullable field failing to parse is not a skip condition; store it as null and proceed (self-heals next pass if the source is parseable again). `category_id_source` was dropped from this list 2026-07-10: it was batdongsan-specific (`pageTrackingData.cateId`) and, left in as a required check, would have skipped every alonhadat/homedy listing since neither site has a confirmed equivalent (§6). `property_type` is the required, source-agnostic category signal going forward — it already has a plausible derivation path from URL slugs for alonhadat (§6), which `category_id_source` never had for the new sources.

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

Because every run does a full crawl of the tracked scope for its one source, a listing that has genuinely been removed simply won't be touched this run. At the end of `scrape_listings` for a given (single-source) `run`:

```python
Listing.objects.filter(
    source_site=run.source_site, is_active=True, last_seen_at__lt=run.started_at,
).update(is_active=False, delisted_at=run.finished_at)
```

This only ever runs for `alonhadat` or `homedy` — batdongsan has no full-coverage automated crawl to make the sweep meaningful (§6), so it's permanently excluded, not conditionally skipped.

A 404 on a previously-known LDP URL during the crawl is also treated as an immediate delisting signal (`is_active=False`, `delisted_at=now()`) for that listing, not a scrape failure.

batdongsan exposed a `pageTrackingData.products[0].expired` boolean on every LDP, which made "still listed but marked expired" cheap to detect without an extra check. Whether alonhadat or homedy expose an equivalent signal (a banner, a status field, or nothing beyond a 404) is unconfirmed — check while building `scraping/sites/alonhadat.py`/`homedy.py`. If neither has one, a 404 plus the end-of-run sweep above is the whole delisting signal for that source, and that's an acceptable outcome, not a degraded one.

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

**Run tracking**: create a `ScrapeRun` row at the start of `scrape_listings` (`source_site` from the invocation's `--source`, `started_at=now()`), update it at the end (`finished_at`, `listings_seen`, `inserted`, `updated`, `skipped`, `error_count`). This is how a structural break in a site's HTML (e.g. a redesign that breaks every selector) becomes visible — check this table (via `/admin/`) periodically, filtered by `source_site`; a run with `listings_seen` near zero or `error_count` spiking relative to that source's own history means that site changed and its parser needs attention, not that the data is real.

## 9. Deployment

Must be fixed before deploying (standard practice, not a judgment call):
- `SECRET_KEY` from an env var via `python-decouple`, not the hardcoded value currently in `settings.py`.
- `DEBUG = config("DEBUG", default=False, cast=bool)`.
- `ALLOWED_HOSTS` from an env var (comma-split), including the Render-assigned domain.
- `whitenoise` added to `MIDDLEWARE` (right after `SecurityMiddleware`) and `requirements.txt` for static file serving — Render doesn't serve Django static files on its own.
- `STATIC_ROOT = BASE_DIR / "staticfiles"` — already set in `settings.py`, required for `collectstatic` to run at all. Added 2026-07-15 alongside the `source.css` move out of `STATICFILES_DIRS` (§11).
- Render build command must run `python manage.py tailwind build` before `collectstatic` — the compiled stylesheet (`assets/css/tailwind.css`) is gitignored, not committed, so it has to be built on deploy. The standalone Tailwind CLI downloads automatically at build time (version pinned via `TAILWIND_CLI_VERSION` in `settings.py`); no Node.js/npm on Render. See §11.

Scraper scheduling: the original rationale for keeping the scraper local — SRP fetching needing a local, non-stealth browser, confirmed 2026-07-07 for batdongsan — no longer holds. alonhadat and homedy are both confirmed reachable with plain `requests` (§6), so a Render Cron Job is now technically viable and isn't ruled out for the reason it used to be. The decision stands anyway, restated rather than re-derived 2026-07-10: the entire scraper pipeline, `scrape_listings` (run once per source) followed by `score_listings`, keeps running locally via Windows Task Scheduler, not on Render. Reasons that hold without the browser constraint: it's already built and working this way, migrating a working scheduled job to Render Cron this close to Aug 10 is schedule risk for zero functional gain, and `ScrapeRun` monitoring via `/admin/` works the same either way. Render's job stays hosting the Django web app and API only, both reading the same remote Postgres instance the local scraper writes to. This accepts the risk of gaps in `PriceHistory`/delisting detection when the local machine is off — revisit moving to Render Cron only if that gap actually causes a problem, not preemptively.

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

## 11. Frontend / Design System

Tailwind CSS 4 via `django-tailwind-cli` (in `requirements.txt`). It downloads the standalone Tailwind CLI binary — no Node.js, no npm, no `package.json`, no `tailwind.config.js` anywhere in the project, ever. Set up 2026-07-15, configured manually in `settings.py` rather than via the interactive `tailwind setup` wizard: `django_tailwind_cli` in `INSTALLED_APPS`, `STATICFILES_DIRS = [BASE_DIR / "assets"]`, `TAILWIND_CLI_SRC_CSS = "tailwind_src/source.css"`, `TAILWIND_CLI_VERSION` pinned (no `latest` drift on deploy).

Files:
- `tailwind_src/source.css` — the only hand-written CSS in the project: `@import "tailwindcss"` plus the `@theme` token block below. Committed. Moved out of `assets/` 2026-07-15: it must live outside every `STATICFILES_DIRS` path, because manifest static-file storage (Django's `ManifestStaticFilesStorage`, which whitenoise's production storage subclasses) post-processes CSS during `collectstatic` and crashes trying to resolve `@import "tailwindcss"` as a static-file reference — reproduced live before the move (`Post-processing 'css\source.css' failed!`), confirmed clean after (`155 post-processed`).
- `assets/css/tailwind.css` — compiled output, gitignored, rebuilt by `python manage.py tailwind build` (locally and in Render's build command, §9). The downloaded CLI binary lands in `.django_tailwind_cli/`, also gitignored.
- `listings/templates/base.html` — the single base template. Loads the compiled stylesheet via `{% tailwind_css %}`, sets `bg-paper text-ink font-serif` on `<body>`, wraps content in the centered `max-w-2xl` main column. Every page template `{% extends "base.html" %}` — no standalone HTML documents, no `<style>` blocks, no inline `style=` attributes.

Theme tokens — this is the entire palette, defined once in `@theme` and used as `bg-paper`, `text-ink`, `text-muted`, `border-line`, `text-accent`:

| Token | Value | Role |
|---|---|---|
| `--color-paper` | `#faf9f7` | page background |
| `--color-ink` | `#1c1917` | text, strong borders |
| `--color-muted` | `#78716c` | secondary text |
| `--color-line` | `#e7e5e4` | hairline borders |
| `--color-accent` | `#9a3412` | prices, links |

Rules for all Standard-scope templates:
- Colors and fonts come only from these five tokens plus Tailwind's built-in `font-serif` stack. No raw hex in templates, no arbitrary color values (`text-[#...]`), no second palette family. A new color means a new token in `source.css`, not a one-off utility.
- Spacing and type sizes use Tailwind's standard scale (`text-sm`, `py-3.5`, `mb-6`), no arbitrary values (`text-[0.95rem]`).
- Dev loop: `python manage.py tailwind watch` alongside `runserver`, or `tailwind runserver` which runs both. One-off rebuild: `tailwind build`.

Deliberately not built, add only when a real page needs it: component/partial library, dark mode, webfont loading (system serif stack only), Chart.js theming (trend charts are Stretch, §2).

## 12. Anomaly Detection

Computed by `score_listings`, run after every scrape, over `is_active=True` listings. Each rule scopes its own population from there (amended 2026-07-16; the previous "with both `price` and `predicted_price` populated" qualifier described only the price-gap rule's population, and applied literally to a pre-ML build it would score zero rows): `price_gap` needs `price` and `predicted_price` both populated; `low_photos` needs `images` non-null (see rule 2); `stale_listing` needs `days_on_market` computable (§5.5). Requires at least 20 listings with both `price` and `predicted_price` to compute the price-gap percentile meaningfully — below that, skip the price-gap rule for this run (too small a sample for a percentile to mean anything) but still compute the other two rules.

Three rules, each independently computed:

1. **`price_gap`**: `(predicted_price - price) / price`. Flag if in the top 5% of this ratio across all qualifying active listings this run (recomputed each run with `numpy.percentile`, not a fixed cutoff — there's no data yet to justify a fixed percentage, and this adapts to whatever the model's real accuracy turns out to be).
2. **`low_photos`**: `len(images) < 3`, computed only over listings with `images` non-null (amended 2026-07-16 from `len(images or []) < 3`, which treated null as 0). Null and `[]` mean different things and only `[]` is a real photo count. `images` is null when the gallery was never parsed: an alonhadat/homedy row still awaiting LDP enrichment under the `--max-ldp-visits` budget (§6), an alonhadat LDP visit that served a page without the `article.property` anchor — the structural wrapper on every real LDP regardless of gallery state (parser fixed 2026-07-16: `parse_ldp_extras` previously returned `[]` on any gallery-selector miss, so a markup redesign would have read as enrichment-done — the `images IS NULL` retry gate never re-visits an `[]` row — and mass-flagged the source as `low_photos`; anchor-less pages now parse to null, stay retry-eligible, and are skipped by scoring), or a batdongsan LDP whose gallery container (`.re__media-preview`) is absent — markup change or partial save, §8's nullable parse failure (parser fixed 2026-07-16: `parse_ldp` previously collapsed `[]` to null via `or None`). Null rows are skipped — `is_anomaly`/`anomaly_reason` untouched — and enter scoring once enrichment or re-ingest fills `images`. `[]` is a parsed gallery with zero photos (reachable for real: a video-only listing keeps the container with no image slides; for alonhadat, the `article.property` anchor parsed with no gallery images — visited once, done, never re-fetched) and flags at 0. Accepted residual gap (2026-07-16): a partial redesign that keeps `article.property` but renames the gallery markup still parses to `[]`, silently indistinguishable from a genuine zero-photo listing — accepted rather than closed because guarding the gallery container instead would retry genuinely photo-less rows forever (whether alonhadat keeps an empty container on such listings is unconfirmed). Validated against live data 2026-07-16: treating null as 0 would have flagged 63 of 93 active listings, almost all on missing data; the actual scored set was 31, flagging 1.
3. **`stale_listing`**: `days_on_market > 90` (see §5.5 for the computed value, including its `posted_date`-null fallback).

`individual_seller` is explicitly not a rule — see §2.

`is_anomaly = True` if any rule trips. `anomaly_reason` holds one key per rule that actually ran this scoring run — a partial build stores a partial dict, no faked `triggered: false` entries for rules that don't exist yet (decided 2026-07-16; as of that date only `low_photos` is built, so stored rows carry a one-key dict). Once all three rules exist, every scored row carries the full shape below, so the "why flagged" dashboard tooltip can show the full picture, not just the triggered ones. No migration is needed to get there: `score_listings` assigns a whole new dict each run (full replacement, verified — not a key merge), so one-key rows self-heal to the three-key shape on the first scoring run after the remaining rules land. Rows that leave the scored population (delisted, or `images` reverting to null) keep their last-written dict until they re-enter it:

```json
{
  "price_gap": {"triggered": true, "value": 0.23},
  "low_photos": {"triggered": false, "value": 6},
  "stale_listing": {"triggered": true, "value": 104}
}
```

`predicted_at` is set to the scoring run's timestamp only when the run actually computes `predicted_price` for that row (amended 2026-07-16 from "whenever `predicted_price`/`is_anomaly`/`anomaly_reason` are (re)computed"). A run that only recomputes anomaly fields — today's low_photos-only build — leaves `predicted_at` untouched: a timestamp next to a null `predicted_price` would read as "model ran and returned nothing," which never happened.

## 13. ML Model

Two candidates: linear regression and random forest (`scikit-learn`), trained on `district`, `area_sqm`, `property_type` at minimum. Pick whichever has the better RMSE/R² on a held-out test split. No fixed accuracy floor (see §2) — whatever it achieves ships, and gets reported honestly in the research piece.

Serialize with `pickle` (not `joblib`) per the existing plan. `train_model` is a manual/occasional command, not part of the scheduled pipeline — retrain by re-running it and committing the new `model.pkl` when there's meaningfully more data.

## 14. Testing

Django's built-in `TestCase` (`python manage.py test`) — ships with Django, zero new dependencies, no reason to add `pytest` for this project's size.

Required coverage for "done," not full coverage everywhere:
- Currency parsing (`parse_vnd`), including the negotiable-price-returns-null case.
- Upsert/dedup logic (`update_or_create` path creates exactly one `PriceHistory` row on price change, zero on no change).
- Agent dedup: two listings from the same `(source_site, source_id)` agent reuse one `Agent` row, not two.
- `days_on_market` computed value, including the `posted_date`-null fallback path.
- Anomaly rule computation (all three rules, both triggered and not-triggered cases).

Full view/endpoint test coverage and ML internals are not required to consider a phase done.

## 15. Definition of Done, Per Phase

**Scraper**
- `python manage.py scrape_listings --source alonhadat` and `--source homedy` each run end-to-end unattended.
- Full SRP crawl of the tracked scope + LDP visit per listing, every run, per source.
- Retry/backoff and rate limiting implemented as specified in §8.
- `ScrapeRun` row (with `source_site` set) created and updated every run.
- `(source_site, source_id)` uniqueness enforced at the DB level.
- Delisting detection working for alonhadat and homedy: previously-active listings not touched in a run flip to `is_active=False`. Same for a 404 on a known LDP URL and for whatever each site's own expired-listing signal turns out to be (§7).
- Fetch method matches §6: plain `requests`, no browser, for both alonhadat and homedy.
- `PriceHistory` row inserted on every detected price change, and on first insert.
- Has run successfully at least once against both live sites producing real data (not seed/fixture data).
- Running on a schedule unattended via local Windows Task Scheduler (§9), not only ever run by hand.
- `ingest_saved_listings` still works for the batdongsan manual fallback, but is explicitly not required to run on any schedule for this phase to count as done (§6).

**Database**
- `makemigrations` + `migrate` runs clean from an empty database.
- `Listing`, `PriceHistory`, `ScrapeRun`, `Agent` match §5 exactly — field names, types, nullability, constraints.
- All four models registered in `admin.py` so the data is inspectable without writing queries.

**Backend + API**
- DRF installed and configured.
- `GET /api/listings/` and `GET /api/listings/<id>/` live, filtering/pagination/ordering working as specified in §10.
- Endpoints return real data from the live DB, not fixtures.
- Tests from §14 passing.

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
