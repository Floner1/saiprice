# Pipeline error states and analytics dashboard

Date: 2026-08-06
Status: approved, ready for implementation plan

## Problem

Two gaps, one session.

**Error states.** The scrape and score pipeline has at least twelve distinct
failure modes. Five of them collapse into a single `ScrapeRun.error_count`
integer, two are written to stderr and counted nowhere at all, one is
undetectable, and three take the scoring command down entirely. Nothing
downstream can tell a bot-challenge wall from a flaky network, or a markup
redesign from a normal run.

**Analytics.** There is no view of crawl volume over time and no history of
model accuracy at all. `predicted_price` / `predicted_at` are current-state
columns overwritten on every scoring run, so every active priced row shares
one `predicted_at`. Grouping by that date yields a single bucket, not a trend.

## Findings that shaped the design

Confirmed against the code and the live database on 2026-08-06.

1. **`ScrapeRun` already supports scrapes-per-day.** It carries `started_at`,
   `finished_at`, `listings_seen`, `inserted`, `updated`, `skipped`,
   `error_count`, `posted_date_nulls`, `source_site`. 22 runs across 13
   distinct days exist. No new table is needed for the volume chart.

2. **Four `ScrapeRun` rows have `finished_at IS NULL` and `listings_seen = 0`**
   (pks 15, 17, 18, 20) — the orphaned fires from the `0xC000013A` incident
   (CLAUDE.md §9). A naive per-day chart counts these as scrape days. They are
   not. The chart must separate *ran and produced work*, *ran and produced
   nothing*, and *never finished*.

3. **Every completed run reports exactly `error_count = 1`.** Persistent and
   unexplained. The current schema cannot say which of five incrementing sites
   produced it. This is the concrete motivation for a per-code breakdown.

4. **Accuracy history cannot be derived from the current schema.** See above.
   Storing something per scoring run is forced, not a preference.

5. **`train_model` fits on the same `Listing` rows `score_listings` scores.**
   Residuals over active listings are therefore largely in-sample and will read
   better than the shipped model's held-out medAPE of 22.87%. The training date
   is not in the pickled bundle (keys are exactly `model`, `district_stats`,
   `model_type`), and adding it would mean touching training code, which is out
   of scope. The number is therefore labelled in-sample rather than corrected.

6. **"Actual price" is the current price.** `run_scraper.ps1` runs
   `scrape_listings` then `score_listings` unconditionally, and
   `_predictions()` reads the just-upserted `Listing.price`. Prediction and
   observed price are the same instant, so no `PriceHistory` lookup is needed.
   There is no prior definition to reuse: §12's `price_gap` rule is not built
   (only `low_photos` and `stale_listing` are), so this spec defines it.

7. **Chart.js is named in CLAUDE.md §3 but is not in the project.**
   `base.html` has no `<script>` tag and nothing is vendored. Charts are
   therefore server-rendered.

## Error taxonomy

Every code below gets a counter in a `status_counts` dict and one log line.
`error_count` and `skipped` keep their current meaning; nothing that reads them
today changes behaviour.

### Scraper codes

| code | today | after |
|---|---|---|
| `srp_fetch_failed` | `error_count += 1`, `break` out of the category | same, plus counted by code; log names the category root and page number |
| `bot_challenge` | `client.fetch` logs ERROR and returns `None`, indistinguishable to the caller from a give-up | `fetch` reports a distinguishable outcome; counted on its own |
| `fetch_gave_up` | same `None` after 3 attempts | counted separately from `bot_challenge` |
| `ldp_fetch_failed` | stderr write, `error_count += 1` | same, plus counted |
| `ldp_404` | folded into the non-200 branch and counted as a generic error | named on its own and **not** counted as an error; CLAUDE.md §7 calls a 404 a delisting signal, not a scrape failure |
| `ldp_no_anchor` | stderr write only, counted nowhere | counted; this is the alonhadat markup-change tripwire |
| `unmapped_breadcrumb` | stderr write only, counted nowhere | counted |
| `soft_gone_placeholder` | undetectable | detected and counted; treated as a delisting signal, not an error |
| `required_field_missing` | `skipped += 1`, stderr write | same, plus counted |
| `upsert_exception` | stderr write, `error_count += 1` | same, plus counted with the exception class name |

`bot_challenge` versus `fetch_gave_up` is the distinction that matters in
practice: a wall incident and a flaky connection currently produce an identical
`error_count = 1`.

Neither `ldp_404` nor `soft_gone_placeholder` delists the listing, despite §7's
404 rule. The listing appeared on the SRP during this same run, which is live
evidence it is listed, and `upsert()` runs immediately afterwards and
unconditionally sets `is_active=True` — a delisting written at that point would
be reversed microseconds later. Enrichment is skipped, `images` stays null, and
the row retries on the next pass.

`soft_gone_placeholder` detects a dead LDP URL that returns 200 with
`article.property` present and `itemprop="name"` reading `Trang chủ` — a real
observed state on alonhadat, neither a 404 nor the known bot challenge.

### Two bugs this exposes, both fixed here

**Run counters are lost on any uncaught exception.** `run` is created at the
top of `handle()` and saved only at the very bottom. An exception anywhere in
the page loop — `parse_srp` raising after a redesign, for instance — discards
the entire run's counters, not merely the tail. Fixed with `try/finally` so
`finished_at` and all counters always persist. `sweep_delistings` stays inside
the `try` and is skipped on abort: a partial crawl must never sweep.

**A hard kill cannot be caught.** `0xC000013A` terminates the process; no
Python handler runs. `finished_at IS NULL` is the only surviving evidence. The
reader side therefore treats an unfinished run older than 6 hours as `aborted`
and renders it as such.

### Scoring codes

| code | today | after |
|---|---|---|
| `model_load_failed` | uncaught in `_predictions()`; a corrupt `model.pkl` kills the whole command | caught, counted, `_predictions()` returns `{}` |
| `inference_failed` | uncaught; `predict()` raising kills the whole command | caught, counted, `_predictions()` returns `{}` |
| `listing_save_failed` | uncaught per-row `listing.save()` | caught per row, counted, run continues |

The substantive change: a model failure currently also takes down `low_photos`
and `stale_listing`, neither of which needs the model. After this, the anomaly
rules run regardless of model health.

## Accuracy metric

**Median absolute percentage error**, `median(|predicted_price - price| / price)`.

Population: `is_active=True`, `listing_intent="sale"`, `predicted_price` and
`price` both non-null. This is `_predictions()`'s own scope intersected with
price-present.

Why medAPE over MAE: it is the metric already reported for the shipped model
(22.87%), so the dashboard and the commit log use one language; and with a
log-price target the VND mean is dominated by a handful of very large listings.
MAE in whole VND is stored alongside it because it costs nothing, but it is not
charted.

`model_fingerprint` is `sha256(model.pkl bytes)[:12]`, stored per run, so a
retrain appears as a labelled boundary on the chart instead of an unexplained
step change.

**Honesty requirement.** The page states that this is residual error on current
inventory and in-sample, because the model is fit on these rows. This figure
must not be quoted as held-out accuracy in the research piece.

## Storage

One `ALTER` on an existing table and one new table. Both columns nullable or
defaulted, so the migration is safe against the live database.

```python
# listings/models.py — added to ScrapeRun
status_counts = models.JSONField(null=True)


class ScoringRun(models.Model):
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    predicted = models.IntegerField(default=0)
    scored = models.IntegerField(default=0)
    flagged = models.IntegerField(default=0)
    n_compared = models.IntegerField(default=0)
    mae_vnd = models.DecimalField(max_digits=15, decimal_places=0, null=True)
    median_ape = models.DecimalField(max_digits=6, decimal_places=4, null=True)
    model_fingerprint = models.CharField(max_length=12, null=True)
    error_count = models.IntegerField(default=0)
    status_counts = models.JSONField(null=True)
```

`status_counts` reuses the dict-of-codes shape `anomaly_reason` already uses,
rather than introducing a second convention. Both models are registered in
`admin.py` alongside the existing four.

Migration name: `add_run_status_and_scoring_run`, per CLAUDE.md §4's
descriptive-migration rule.

## Dashboard

New route `/health/` → `PipelineHealthView` → `listings/pipeline_health.html`,
extending `base.html` like every other page. It queries the ORM directly and
does not call the REST API (CLAUDE.md §4).

Three blocks:

1. **Scrapes per day, last 30 days.** One bar per calendar day, length from
   `listings_seen`. A day whose only runs are unfinished draws as a hairline
   marked `aborted`. Days with no run at all appear as empty slots, never
   omitted — a gap in the crawl is the signal, and dropping the row hides it.
2. **Median APE per scoring run.** One bar per run. A change in
   `model_fingerprint` is marked on the axis.
3. **Recent runs.** Last 15 of each kind, with `status_counts` expanded into
   readable per-code rows and a plain-language status per run.

Rendering is `<div>` elements with percentage widths, not SVG: percentage
widths stay responsive with no viewBox arithmetic, and bar charts need nothing
more. Colours and spacing come only from the CLAUDE.md §11 tokens
(`bg-accent`, `text-muted`, `border-line`, `text-ink`, `bg-paper`) plus
Tailwind's standard scale. No raw hex, no arbitrary values, no `<style>` block,
no inline `style=` for anything but the computed bar width.

## Logging

A `LOGGING` block is added to `settings.py`. There is none today, so `listings.*`
loggers propagate to a root logger with no handler and only WARNING and above
survive, via Python's last-resort handler. Anything logged at INFO is lost.

Root console handler at INFO, the `listings` namespace at INFO, `django` left
on Django's own default. Format: `%(levelname)s %(name)s %(message)s`.

This is purely additive — no existing settings key is modified — but it is the
one file every other feature also depends on, which is why it is called out
separately.

## Testing

New `listings/tests/test_pipeline_health.py`, plus additions to
`test_scraping.py` and `test_scoring.py`:

- Each scraper failure code increments its own counter and no other.
- Run counters and `finished_at` survive an exception raised mid page-loop, and
  the delisting sweep does not run on an aborted run.
- `model_load_failed` and `inference_failed` are caught, and `low_photos` /
  `stale_listing` still score afterwards.
- `listing_save_failed` is counted and the run continues to the next row.
- Per-day aggregation buckets correctly across an aborted run, a zero-run day,
  and two runs on one day.
- medAPE and MAE match hand-computed values, including the empty-population
  case, which must store null rather than raise or store zero.
- `/health/` renders with no runs at all in the database.

## Out of scope

- Per-listing prediction history. One aggregate row per scoring run is what the
  chart needs; a per-listing table adds roughly 800 rows a day for no query the
  dashboard makes.
- Alerting or thresholds on top of the counters.
- §12's `price_gap` anomaly rule. It shares the predicted-versus-actual
  arithmetic defined here but is a separate scoping decision.
- Retention or pruning of `ScoringRun`. One row per day.
- Any change to model training or inference. The accuracy layer wraps
  `predict()`; it does not alter how prediction works.
- `homedy`. `scrape_listings` still accepts `--source alonhadat` only.
