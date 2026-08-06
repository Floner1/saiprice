# Pipeline Error States and Analytics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every known scrape and score failure mode a named, counted, logged status, and add a `/health/` dashboard showing crawl volume per day and model accuracy over time.

**Architecture:** `client.fetch` stops returning a bare `None` and returns `(response, error_code)`, so callers can tell a bot challenge from a timeout from a 404. `scrape_listings` and `score_listings` accumulate a `{code: count}` dict and persist it — to a new `ScrapeRun.status_counts` column and a new `ScoringRun` table respectively. `ScoringRun` also stores per-run accuracy (median APE, MAE), which is the only way to get a trend, since `predicted_at` is overwritten every run. A new `listings/analytics.py` holds the read-side aggregation and the pure metric maths; a `TemplateView` renders it as percentage-width `<div>` bars.

**Tech Stack:** Django 5.2, PostgreSQL, `statistics.median` from the stdlib, Tailwind 4 via `django-tailwind-cli`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-06-pipeline-error-states-and-analytics-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `saiprice/settings.py` | Modify: add `LOGGING`. Nothing else. |
| `listings/models.py` | Modify: `ScrapeRun.status_counts`, new `ScoringRun`. |
| `listings/migrations/000N_add_run_status_and_scoring_run.py` | Generated. |
| `listings/admin.py` | Modify: register `ScoringRun`, add `status_counts` to `ScrapeRunAdmin`. |
| `listings/scraping/client.py` | Modify: `fetch` returns `(response, error_code)`. |
| `listings/scraping/sites/alonhadat.py` | Modify: `parse_ldp_extras` reports `soft_gone`. |
| `listings/management/commands/scrape_listings.py` | Modify: record status codes, persist counters via `try/finally`. |
| `listings/management/commands/score_listings.py` | Modify: `ScoringRun`, caught model failures, accuracy metrics. |
| `listings/analytics.py` | Create: read-side aggregation + `accuracy_metrics`. Pure enough to test without a crawl. |
| `listings/views.py` | Modify: add `PipelineHealthView`. |
| `listings/templates/listings/pipeline_health.html` | Create. |
| `saiprice/urls.py` | Modify: `/health/`. |
| `listings/tests/test_analytics.py` | Create. |
| `listings/tests/test_scraping.py` | Modify: update fetch mocks, add status-code tests. |
| `listings/tests/test_scoring.py` | Modify: add `ScoringRun` + failure tests. |
| `listings/tests/test_views.py` | Modify: add `/health/` render tests. |

### Final status code list

Codes are stage-prefixed and each failure increments exactly one key, so the values always sum to the number of failures.

Scraper: `srp_bot_challenge`, `srp_fetch_gave_up`, `srp_http_error`, `ldp_bot_challenge`, `ldp_fetch_gave_up`, `ldp_http_error`, `ldp_404`, `ldp_no_anchor`, `ldp_soft_gone`, `unmapped_breadcrumb`, `required_field_missing`, `upsert_exception`, `run_aborted`.

Scoring: `model_load_failed`, `inference_failed`, `listing_save_failed`.

Two codes are **not** errors and must not increment `ScrapeRun.error_count`: `ldp_404` and `ldp_soft_gone`. Both mean "the LDP is not real content"; neither means the crawl malfunctioned. Note this changes today's behaviour, where a 404 LDP does increment `error_count`.

Neither delists the listing. The listing appeared on the SRP during this same run, which is live evidence it is listed; and `upsert()` runs immediately afterwards and unconditionally sets `is_active=True`, so a delisting written here would be reversed microseconds later. Enrichment is skipped, `images` stays null, the row retries next run.

---

## Task 1: LOGGING configuration

Do this first — every later task logs, and without it anything below WARNING is discarded.

**Files:**
- Modify: `saiprice/settings.py` (append after `DEFAULT_AUTO_FIELD`)

- [ ] **Step 1: Confirm the current behaviour you are fixing**

Run:
```bash
cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe -c "
import os, django, logging
os.environ.setdefault('DJANGO_SETTINGS_MODULE','saiprice.settings')
django.setup()
log = logging.getLogger('listings.scraping.client')
log.info('INFO-SHOULD-BE-INVISIBLE-BEFORE-TASK-1')
log.warning('WARNING-VISIBLE-VIA-LASTRESORT')
"
```
Expected: only the WARNING line appears. The INFO line is silently dropped. That is the gap.

- [ ] **Step 2: Add the LOGGING block**

Append to `saiprice/settings.py`:

```python
# No LOGGING block existed before 2026-08-06: listings.* loggers propagated to
# a root logger with no handler, so only WARNING+ survived via Python's
# lastResort handler and every logger.info call was discarded. The pipeline's
# per-failure status lines are INFO, so they need a real handler.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "pipeline": {"format": "%(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "pipeline",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "listings": {"level": "INFO", "propagate": True},
    },
}
```

`disable_existing_loggers: False` and the absent `django` key are both deliberate: Django's own logger config stays on its default rather than being replaced.

- [ ] **Step 3: Verify INFO now reaches the console**

Run the same command as Step 1.
Expected: both lines appear, formatted as `INFO listings.scraping.client INFO-SHOULD-BE-INVISIBLE-BEFORE-TASK-1`.

- [ ] **Step 4: Verify the existing suite is unaffected**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings 2>&1 | tail -5`
Expected: same pass count as before the change.

- [ ] **Step 5: Commit**

```bash
git add saiprice/settings.py
git commit -m "Configure logging so pipeline INFO lines are not discarded"
```

---

## Task 2: Schema — ScrapeRun.status_counts and ScoringRun

**Files:**
- Modify: `listings/models.py` (`ScrapeRun`, end of file)
- Modify: `listings/admin.py`
- Create: `listings/migrations/000N_add_run_status_and_scoring_run.py` (generated)
- Test: `listings/tests/test_models.py`

- [ ] **Step 1: Write the failing test**

Append to `listings/tests/test_models.py`:

```python
class ScoringRunSchemaTests(TestCase):
    def test_scrape_run_status_counts_defaults_to_null(self):
        from listings.models import ScrapeRun

        run = ScrapeRun.objects.create(
            source_site="alonhadat", started_at=timezone.now()
        )
        run.refresh_from_db()
        self.assertIsNone(run.status_counts)

    def test_scrape_run_status_counts_round_trips_a_dict(self):
        from listings.models import ScrapeRun

        run = ScrapeRun.objects.create(
            source_site="alonhadat",
            started_at=timezone.now(),
            status_counts={"ldp_404": 2, "srp_bot_challenge": 1},
        )
        run.refresh_from_db()
        self.assertEqual(run.status_counts, {"ldp_404": 2, "srp_bot_challenge": 1})

    def test_scoring_run_counters_default_to_zero_and_metrics_to_null(self):
        from listings.models import ScoringRun

        run = ScoringRun.objects.create(started_at=timezone.now())
        run.refresh_from_db()
        self.assertEqual(
            (run.predicted, run.scored, run.flagged, run.n_compared, run.error_count),
            (0, 0, 0, 0, 0),
        )
        self.assertIsNone(run.finished_at)
        self.assertIsNone(run.mae_vnd)
        self.assertIsNone(run.median_ape)
        self.assertIsNone(run.model_fingerprint)
        self.assertIsNone(run.status_counts)

    def test_scoring_run_median_ape_keeps_four_decimal_places(self):
        from decimal import Decimal

        from listings.models import ScoringRun

        run = ScoringRun.objects.create(
            started_at=timezone.now(), median_ape=Decimal("0.2287")
        )
        run.refresh_from_db()
        self.assertEqual(run.median_ape, Decimal("0.2287"))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_models.ScoringRunSchemaTests -v 2`
Expected: FAIL — `ImportError: cannot import name 'ScoringRun'`.

- [ ] **Step 3: Add the column and the model**

In `listings/models.py`, add to `ScrapeRun` immediately after `posted_date_nulls`:

```python
    # {status code: count} for the run, one key per failure mode (CLAUDE.md §8).
    # error_count stays what it always was -- a single total that cannot say
    # whether the failure was a bot wall, a timeout or a dead URL. This can.
    # Same dict-of-codes shape as Listing.anomaly_reason, not a second convention.
    status_counts = models.JSONField(null=True)
```

Append at the end of `listings/models.py`:

```python
class ScoringRun(models.Model):
    """One row per score_listings invocation, mirroring ScrapeRun.

    Accuracy has to be stored per run, not derived: predicted_price and
    predicted_at are current-state columns overwritten on every run, so every
    active priced row shares one predicted_at and grouping by it yields a
    single bucket rather than a trend.
    """

    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    predicted = models.IntegerField(default=0)
    scored = models.IntegerField(default=0)
    flagged = models.IntegerField(default=0)
    # rows behind mae_vnd/median_ape: the accuracy population, which is smaller
    # than `predicted` because it also requires a non-null, non-zero price.
    n_compared = models.IntegerField(default=0)
    mae_vnd = models.DecimalField(max_digits=15, decimal_places=0, null=True)
    # In-sample: train_model fits on these same rows, so this reads better than
    # the model's held-out medAPE and must not be quoted as held-out accuracy.
    median_ape = models.DecimalField(max_digits=6, decimal_places=4, null=True)
    model_fingerprint = models.CharField(max_length=12, null=True)
    error_count = models.IntegerField(default=0)
    status_counts = models.JSONField(null=True)
```

- [ ] **Step 4: Generate the migration**

Run:
```bash
cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py makemigrations listings --name add_run_status_and_scoring_run
```
Expected: creates one migration adding field `status_counts` to `scraperun` and creating model `ScoringRun`.

- [ ] **Step 5: Inspect the SQL before applying it to the live database**

Run:
```bash
cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py sqlmigrate listings 000N
```
(substitute the number just generated)
Expected: an `ALTER TABLE listings_scraperun ADD COLUMN status_counts jsonb NULL` and a `CREATE TABLE listings_scoringrun`. **No `DROP`, no `ALTER COLUMN` on any pre-existing column.** If anything else appears, stop and report it — that is outside the approved scope.

- [ ] **Step 6: Apply and verify the tests pass**

Run:
```bash
cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py migrate listings && ./venv/Scripts/python.exe manage.py test listings.tests.test_models -v 2
```
Expected: migration applies, all `test_models` tests pass.

- [ ] **Step 7: Register in admin**

Rewrite `listings/admin.py`:

```python
from django.contrib import admin

from .models import Agent, Listing, PriceHistory, ScoringRun, ScrapeRun

admin.site.register(Listing)
admin.site.register(Agent)
admin.site.register(PriceHistory)


@admin.register(ScrapeRun)
class ScrapeRunAdmin(admin.ModelAdmin):
    list_display = (
        "id", "source_site", "started_at", "finished_at", "listings_seen",
        "inserted", "updated", "skipped", "error_count", "posted_date_nulls",
        "status_counts",
    )


@admin.register(ScoringRun)
class ScoringRunAdmin(admin.ModelAdmin):
    list_display = (
        "id", "started_at", "finished_at", "predicted", "scored", "flagged",
        "n_compared", "median_ape", "mae_vnd", "model_fingerprint",
        "error_count", "status_counts",
    )
```

- [ ] **Step 8: Commit**

```bash
git add listings/models.py listings/admin.py listings/migrations listings/tests/test_models.py
git commit -m "Add ScrapeRun.status_counts and the ScoringRun model"
```

---

## Task 3: client.fetch returns a reason

**Files:**
- Modify: `listings/scraping/client.py`
- Test: `listings/tests/test_scraping.py`

- [ ] **Step 1: Write the failing tests**

Append to `listings/tests/test_scraping.py`:

```python
class FetchErrorCodeTests(TestCase):
    def _response(self, status_code=200, url="https://alonhadat.com.vn/x-1.html"):
        return Mock(status_code=status_code, url=url, headers={})

    def test_success_returns_response_and_no_error(self):
        response = self._response()
        with patch.object(client.session, "get", return_value=response):
            with patch.object(client.time, "sleep"):
                got, error = client.fetch("https://alonhadat.com.vn/x-1.html")
        self.assertIs(got, response)
        self.assertIsNone(error)

    def test_bot_challenge_redirect_reports_bot_challenge(self):
        response = self._response(
            url="https://alonhadat.com.vn/xac-thuc-nguoi-dung.html"
        )
        with patch.object(client.session, "get", return_value=response):
            with patch.object(client.time, "sleep"):
                got, error = client.fetch("https://alonhadat.com.vn/x-1.html")
        self.assertIsNone(got)
        self.assertEqual(error, "bot_challenge")

    def test_404_reports_http_404_not_a_generic_error(self):
        with patch.object(client.session, "get", return_value=self._response(404)):
            with patch.object(client.time, "sleep"):
                got, error = client.fetch("https://alonhadat.com.vn/x-1.html")
        self.assertIsNone(got)
        self.assertEqual(error, "http_404")

    def test_other_non_200_reports_http_error(self):
        with patch.object(client.session, "get", return_value=self._response(403)):
            with patch.object(client.time, "sleep"):
                got, error = client.fetch("https://alonhadat.com.vn/x-1.html")
        self.assertIsNone(got)
        self.assertEqual(error, "http_error")

    def test_repeated_timeout_reports_fetch_gave_up_after_three_attempts(self):
        from requests.exceptions import Timeout

        with patch.object(client.session, "get", side_effect=Timeout) as get:
            with patch.object(client.time, "sleep"):
                got, error = client.fetch("https://alonhadat.com.vn/x-1.html")
        self.assertIsNone(got)
        self.assertEqual(error, "fetch_gave_up")
        self.assertEqual(get.call_count, 3)

    def test_persistent_500_reports_fetch_gave_up(self):
        with patch.object(client.session, "get", return_value=self._response(500)) as get:
            with patch.object(client.time, "sleep"):
                got, error = client.fetch("https://alonhadat.com.vn/x-1.html")
        self.assertIsNone(got)
        self.assertEqual(error, "fetch_gave_up")
        self.assertEqual(get.call_count, 3)
```

Ensure the file's imports include `from unittest.mock import Mock, patch`, `from django.test import TestCase`, and `from listings.scraping import client`. Add whichever are missing.

- [ ] **Step 2: Run to verify failure**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_scraping.FetchErrorCodeTests -v 2`
Expected: FAIL — `TypeError: cannot unpack non-sequence` / `NoneType`.

- [ ] **Step 3: Rewrite fetch**

Replace the `fetch` function in `listings/scraping/client.py` (keep the module header and `session` block above it exactly as they are):

```python
BOT_CHALLENGE = "bot_challenge"
FETCH_GAVE_UP = "fetch_gave_up"
HTTP_404 = "http_404"
HTTP_ERROR = "http_error"


def fetch(url):
    """Return (response, error_code). error_code is None only on a 200.

    The reason is returned rather than collapsed into None because the caller
    counts failures by mode: a bot wall, a timeout and a dead URL are three
    different operational facts and used to be one indistinguishable None.
    """
    for attempt in range(3):
        try:
            response = session.get(url, timeout=10)
        except (Timeout, ConnectionError, HTTPError):
            time.sleep(2**attempt * 2)
            continue

        # alonhadat serves its bot challenge as a 200 redirect to
        # /xac-thuc-nguoi-dung.html (seen live 2026-07-10 after ~30
        # sequential LDP fetches; IP-scoped, SRPs unaffected). Treat it as
        # a failed fetch, no retry: it won't clear in seconds, and solving
        # or routing around it is the evasion CLAUDE.md §6 rules out.
        if "xac-thuc-nguoi-dung" in response.url:
            logger.error(f"bot challenge served for {url}")
            return None, BOT_CHALLENGE

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else 2**attempt * 2)
            continue
        if response.status_code >= 500:
            time.sleep(2**attempt * 2)
            continue

        time.sleep(random.uniform(1, 3))
        if response.status_code == 404:
            # CLAUDE.md §7 calls a 404 a delisting signal, not a scrape
            # failure. Named separately so the caller can act on that.
            logger.info(f"404 for {url}")
            return None, HTTP_404
        if response.status_code != 200:
            logger.warning(f"HTTP {response.status_code} for {url}")
            return None, HTTP_ERROR
        return response, None

    logger.error(f"gave up on {url} after 3 attempts")
    return None, FETCH_GAVE_UP
```

- [ ] **Step 4: Run to verify pass**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_scraping.FetchErrorCodeTests -v 2`
Expected: 6 tests pass.

- [ ] **Step 5: Update the pre-existing fetch assertion**

`listings/tests/test_scraping.py:658` currently asserts `self.assertIsNone(client.fetch(...))`. Change that single assertion to:

```python
                self.assertEqual(
                    client.fetch("https://alonhadat.com.vn/x-1.html"),
                    (None, "bot_challenge"),
                )
```

- [ ] **Step 6: Commit**

```bash
git add listings/scraping/client.py listings/tests/test_scraping.py
git commit -m "Return a named error code from fetch instead of a bare None"
```

The other `scrape_listings.fetch` mocks in `test_scraping.py` stay broken until Task 5, which updates them alongside the caller. That is intentional — the caller and its mocks change together.

---

## Task 4: Soft-gone placeholder detection

**Files:**
- Modify: `listings/scraping/sites/alonhadat.py` (`parse_ldp_extras`)
- Test: `listings/tests/test_scraping.py`

Background: a dead alonhadat LDP can return 200 with `article.property` present while `[itemprop='name']` reads literally `Trang chủ` (Homepage) and there is no real listing content. Observed 2026-07-28 on 4 of a 5-URL sample. `article.property` alone is therefore not proof of real content. Without this, such a page parses as a valid zero-photo LDP, marks enrichment done, and gets flagged `low_photos`.

- [ ] **Step 1: Write the failing tests**

Append to `listings/tests/test_scraping.py`:

```python
class SoftGonePlaceholderTests(TestCase):
    REAL_LDP = """
      <div itemtype="http://schema.org/BreadcrumbList">
        <a href="/can-ban-can-ho-chung-cu">Căn hộ</a>
      </div>
      <article class="property">
        <h1 itemprop="name">Bán căn hộ Quận 7</h1>
        <section class="images"><ul class="image-list">
          <li><img src="/img/a.jpg"></li>
        </ul></section>
      </article>
    """
    SOFT_GONE = """
      <article class="property">
        <h1 itemprop="name">Trang chủ</h1>
      </article>
    """

    def test_real_ldp_is_not_soft_gone(self):
        extras = alonhadat.parse_ldp_extras(self.REAL_LDP)
        self.assertFalse(extras["soft_gone"])
        self.assertEqual(extras["images"], ["https://alonhadat.com.vn/img/a.jpg"])

    def test_placeholder_shell_is_soft_gone(self):
        extras = alonhadat.parse_ldp_extras(self.SOFT_GONE)
        self.assertTrue(extras["soft_gone"])

    def test_soft_gone_leaves_images_null_not_empty(self):
        # [] would mark enrichment done and mass-flag low_photos; null keeps
        # the row retry-eligible.
        extras = alonhadat.parse_ldp_extras(self.SOFT_GONE)
        self.assertIsNone(extras["images"])

    def test_page_without_the_anchor_is_not_soft_gone(self):
        extras = alonhadat.parse_ldp_extras("<html><body>nothing</body></html>")
        self.assertFalse(extras["soft_gone"])
        self.assertIsNone(extras["images"])
```

`alonhadat` must be imported in the test module as `from listings.scraping.sites import alonhadat`. Add it if missing.

- [ ] **Step 2: Run to verify failure**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_scraping.SoftGonePlaceholderTests -v 2`
Expected: FAIL — `KeyError: 'soft_gone'`.

- [ ] **Step 3: Implement**

In `listings/scraping/sites/alonhadat.py`, add above `parse_ldp_extras`:

```python
# A dead LDP URL can return 200 with article.property present while the page
# title reads literally "Trang chủ" and no real listing content is served
# (observed 2026-07-28, 4 of a 5-URL sample). article.property alone is not
# proof of content, so the title is checked too -- otherwise the shell parses
# as a valid zero-photo LDP and gets flagged low_photos.
SOFT_GONE_TITLE = "Trang chủ"
```

Replace the body of `parse_ldp_extras` from `images = None` through the `return` with:

```python
    images = None
    soft_gone = False
    anchor = soup.select_one("article.property")
    if anchor:
        name = anchor.select_one("[itemprop='name']")
        soft_gone = name is not None and name.get_text(strip=True) == SOFT_GONE_TITLE
    if anchor and not soft_gone:
        images = []
        for img in soup.select("article.property section.images ul.image-list img"):
            src = img.get("src")
            if src:
                url = f"{BASE_URL}{src}" if src.startswith("/") else src
                if url not in images:
                    images.append(url)
    return {
        "property_type": category[0] if category else None,
        "listing_intent": category[1] if category else None,
        "images": images,
        "soft_gone": soft_gone,
    }
```

Leave the long existing comment block above `images = None` in place — it still explains the null-versus-`[]` rule, which is unchanged.

- [ ] **Step 4: Run to verify pass**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_scraping.SoftGonePlaceholderTests -v 2`
Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add listings/scraping/sites/alonhadat.py listings/tests/test_scraping.py
git commit -m "Detect the alonhadat soft-gone placeholder page in parse_ldp_extras"
```

---

## Task 5: scrape_listings records status codes and never loses counters

**Files:**
- Modify: `listings/management/commands/scrape_listings.py`
- Test: `listings/tests/test_scraping.py`

- [ ] **Step 1: Update the existing fetch mocks so they return tuples**

In `listings/tests/test_scraping.py`, every `patch.object(scrape_listings, "fetch", ...)` now needs a tuple. Apply exactly these substitutions:

- `return_value=fetch_response` in `_run_enrich` (line ~365) stays as-is, but change the two `_run_enrich(item, ...)` argument forms at the call sites:
  - `Mock(status_code=200, text=ldp_html)` becomes `(Mock(text=ldp_html), None)`
  - bare `None` becomes `(None, "fetch_gave_up")`
- `return_value=Mock(status_code=200, text=self.LDP_HTML)` becomes `return_value=(Mock(text=self.LDP_HTML), None)` (lines ~523, ~552)
- `return_value=srp` becomes `return_value=(srp, None)` (lines ~595, ~630); where `srp` is built as `Mock(status_code=200, text=...)`, drop the now-unused `status_code`
- line ~444's `patch.object(..., "fetch", return_value=...)` takes the same treatment as its neighbours

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_scraping -v 2`
Expected: still failing, but now on the *command*, not on mock unpacking. This is the checkpoint that the mocks are right before the caller changes.

- [ ] **Step 2: Write the failing status tests**

Append to `listings/tests/test_scraping.py`:

```python
class ScrapeRunStatusCountsTests(TestCase):
    SRP_HTML = """
      <article class="property-item">
        <a itemprop="url" href="/x-777.html" data-memberid="m1">link</a>
        <span itemprop="name">Bán căn hộ</span>
      </article>
    """

    def _run(self, fetch_side_effect):
        with patch.object(
            scrape_listings, "fetch", side_effect=fetch_side_effect
        ):
            call_command(
                "scrape_listings", "--source", "alonhadat", "--pages", "1",
                stdout=io.StringIO(), stderr=io.StringIO(),
            )
        return ScrapeRun.objects.latest("id")

    def test_srp_bot_challenge_is_counted_by_name(self):
        run = self._run([(None, "bot_challenge")])
        self.assertEqual(run.status_counts, {"srp_bot_challenge": 1})
        self.assertEqual(run.error_count, 1)

    def test_srp_timeout_is_counted_separately_from_the_wall(self):
        run = self._run([(None, "fetch_gave_up")])
        self.assertEqual(run.status_counts, {"srp_fetch_gave_up": 1})

    def test_ldp_404_is_counted_but_is_not_an_error(self):
        run = self._run([
            (Mock(text=self.SRP_HTML), None),
            (None, "http_404"),
            (None, "fetch_gave_up"),
        ])
        self.assertEqual(run.status_counts.get("ldp_404"), 1)
        self.assertEqual(run.error_count, 0)

    def test_ldp_404_does_not_delist_a_listing_seen_on_the_srp(self):
        self._run([
            (Mock(text=self.SRP_HTML), None),
            (None, "http_404"),
            (None, "fetch_gave_up"),
        ])
        listing = Listing.objects.get(source_site="alonhadat", source_id="777")
        self.assertTrue(listing.is_active)

    def test_soft_gone_ldp_is_counted_and_leaves_images_null(self):
        soft_gone = '<article class="property"><h1 itemprop="name">Trang chủ</h1></article>'
        self._run([
            (Mock(text=self.SRP_HTML), None),
            (Mock(text=soft_gone), None),
            (None, "fetch_gave_up"),
        ])
        run = ScrapeRun.objects.latest("id")
        self.assertEqual(run.status_counts.get("ldp_soft_gone"), 1)
        self.assertEqual(run.error_count, 0)
        listing = Listing.objects.get(source_site="alonhadat", source_id="777")
        self.assertIsNone(listing.images)

    def test_no_failures_stores_null_not_an_empty_dict(self):
        run = self._run([
            (Mock(text=self.SRP_HTML), None),
            (Mock(text='<article class="property"><h1 itemprop="name">Real</h1></article>'), None),
            (None, "fetch_gave_up"),
        ])
        self.assertIsNone(run.status_counts)


class ScrapeRunAbortTests(TestCase):
    def test_counters_and_finished_at_survive_an_exception_mid_run(self):
        boom = RuntimeError("parser blew up")
        with patch.object(
            scrape_listings.alonhadat, "parse_srp", side_effect=boom
        ):
            with patch.object(
                scrape_listings, "fetch", return_value=(Mock(text="<html></html>"), None)
            ):
                with self.assertRaises(RuntimeError):
                    call_command(
                        "scrape_listings", "--source", "alonhadat",
                        stdout=io.StringIO(), stderr=io.StringIO(),
                    )
        run = ScrapeRun.objects.latest("id")
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.status_counts, {"run_aborted": 1})
        self.assertEqual(run.error_count, 1)

    def test_aborted_run_does_not_sweep_delistings(self):
        stale = _make_listing(
            source_site="alonhadat", source_id="old",
            url="https://alonhadat.com.vn/old-1.html",
            last_seen_at=timezone.now() - timedelta(days=3),
        )
        with patch.object(
            scrape_listings.alonhadat, "parse_srp", side_effect=RuntimeError("boom")
        ):
            with patch.object(
                scrape_listings, "fetch", return_value=(Mock(text="<html></html>"), None)
            ):
                with self.assertRaises(RuntimeError):
                    call_command(
                        "scrape_listings", "--source", "alonhadat",
                        stdout=io.StringIO(), stderr=io.StringIO(),
                    )
        stale.refresh_from_db()
        self.assertTrue(stale.is_active)
```

Required imports in the test module: `io`, `timedelta`, `Mock`/`patch`, `call_command`, `timezone`, `Listing`, `ScrapeRun`, `_make_listing` from `listings.tests.test_models`, and `from listings.management.commands import scrape_listings`. Add whichever are missing.

The trailing `(None, "fetch_gave_up")` in several side-effect lists satisfies the second SRP page request the crawler makes before concluding the category is exhausted.

- [ ] **Step 3: Run to verify failure**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_scraping.ScrapeRunStatusCountsTests listings.tests.test_scraping.ScrapeRunAbortTests -v 2`
Expected: FAIL — `status_counts` is `None` where a dict is expected, and the abort tests fail because no `ScrapeRun` row is saved at all.

- [ ] **Step 4: Implement**

In `listings/management/commands/scrape_listings.py`, add after the `logger = ...` line:

```python
def record(run, code):
    """Bump one status code on the run. Errors also bump error_count.

    ldp_404 and ldp_soft_gone are excluded from error_count deliberately: both
    mean the LDP is not real content, neither means the crawl malfunctioned.
    Each failure increments exactly one key, so status_counts values always sum
    to the number of failures.
    """
    counts = run.status_counts or {}
    counts[code] = counts.get(code, 0) + 1
    run.status_counts = counts
    if code not in NON_ERROR_CODES:
        run.error_count += 1
    logger.info(f"{run.source_site}: {code}")


NON_ERROR_CODES = {"ldp_404", "ldp_soft_gone"}
```

Replace `_enrich_from_ldp`'s body from `if self.ldp_budget <= 0:` to the end with:

```python
        if self.ldp_budget <= 0:
            return
        self.ldp_budget -= 1
        response, error = fetch(item.fields["url"])
        if error:
            # http_404 -> ldp_404, bot_challenge -> ldp_bot_challenge, etc.
            code = "ldp_404" if error == "http_404" else f"ldp_{error}"
            self.stderr.write(f"ldp {error} for {item.fields['url']}")
            record(run, code)
            return
        extras = alonhadat.parse_ldp_extras(response.text)
        if extras["soft_gone"]:
            # A placeholder shell, not a listing: leave images null so the row
            # stays retry-eligible instead of parsing as a zero-photo LDP. No
            # delisting -- the SRP listed it this run, and upsert() reactivates
            # unconditionally moments later anyway.
            self.stderr.write(f"soft-gone placeholder at {item.fields['url']}")
            record(run, "ldp_soft_gone")
            return
        if extras["images"] is None:
            self.stderr.write(
                f"no article.property anchor on {item.fields['url']}; "
                "images left null for retry (markup change?)"
            )
            record(run, "ldp_no_anchor")
        item.fields["images"] = extras["images"]
        if extras["property_type"]:
            item.fields["property_type"] = extras["property_type"]
            item.fields["listing_intent"] = extras["listing_intent"]
        else:
            self.stderr.write(
                f"unmapped breadcrumb category on {item.fields['url']}, "
                "property_type left as-is"
            )
            record(run, "unmapped_breadcrumb")
```

Replace `handle` from `self.ldp_budget = ...` to the end with:

```python
        self.ldp_budget = 0 if options["no_ldp_enrich"] else options["max_ldp_visits"]
        max_visits = self.ldp_budget
        seen = set()
        duplicates = 0
        # try/finally, not try/except: an unexpected exception must still
        # surface, but the run's counters must survive it. Before this, `run`
        # was saved only on the last line, so anything raising mid-loop
        # discarded the whole run's numbers rather than just the tail.
        try:
            for root, (property_type, listing_intent) in alonhadat.CATEGORY_ROOTS.items():
                page = 1
                while options["pages"] is None or page <= options["pages"]:
                    response, error = fetch(alonhadat.page_url(root, page))
                    if error:
                        record(run, f"srp_{error}")
                        break
                    parsed, skips = alonhadat.parse_srp(
                        response.text, property_type, listing_intent
                    )
                    for ref, field in skips:
                        self.stderr.write(
                            f"skipped {ref}: required field missing: {field}"
                        )
                        run.skipped += 1
                        record(run, "required_field_missing")
                    # out-of-range trang-N pages re-serve earlier content, so a
                    # page with no unseen ids means the category is exhausted
                    new = [p for p in parsed if p.source_id not in seen]
                    duplicates += len(parsed) - len(new)
                    if not new:
                        break
                    for item in new:
                        seen.add(item.source_id)
                        run.listings_seen += 1
                        if item.fields.get("posted_date") is None:
                            run.posted_date_nulls += 1
                        try:
                            self._enrich_from_ldp(item, run)
                            if upsert(item):
                                run.inserted += 1
                            else:
                                run.updated += 1
                        except Exception as exc:
                            self.stderr.write(f"error {item.fields['url']}: {exc}")
                            record(run, "upsert_exception")
                    page += 1
            if options["pages"] is None:
                sweep_delistings(run)
        except BaseException:
            # A partial crawl must not sweep, so the sweep above sits inside
            # the try. run_aborted only covers Python-level aborts; a hard kill
            # (0xC000013A) leaves finished_at null, which the reader side reads
            # as aborted instead.
            record(run, "run_aborted")
            raise
        finally:
            run.finished_at = timezone.now()
            check_posted_date_nulls(run)
            run.save()
        self.stdout.write(
            f"seen={run.listings_seen} inserted={run.inserted} "
            f"updated={run.updated} skipped={run.skipped} "
            f"duplicates={duplicates} errors={run.error_count} "
            f"ldp_visits={max_visits - self.ldp_budget} "
            f"status={run.status_counts or '{}'}"
        )
```

`record` must not raise inside the `except` block; it only touches in-memory attributes, so it cannot.

Note `run.finished_at` is now set *before* `sweep_delistings` would have used it — check `sweep_delistings`, which reads `run.finished_at` for `delisted_at`. Move the `run.finished_at = timezone.now()` assignment to just before the `if options["pages"] is None: sweep_delistings(run)` line, and keep only `check_posted_date_nulls(run)` and `run.save()` in the `finally`, adding `run.finished_at = run.finished_at or timezone.now()` there so an aborted run still gets stamped.

- [ ] **Step 5: Run to verify pass**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_scraping -v 2`
Expected: all `test_scraping` tests pass, including the pre-existing ones.

- [ ] **Step 6: Commit**

```bash
git add listings/management/commands/scrape_listings.py listings/tests/test_scraping.py
git commit -m "Record per-failure status codes on ScrapeRun and persist counters on abort"
```

---

## Task 6: Accuracy metrics helper

Written before `score_listings` uses it, so the maths is tested with no ORM or model involved.

**Files:**
- Create: `listings/analytics.py`
- Test: `listings/tests/test_analytics.py`

- [ ] **Step 1: Write the failing test**

Create `listings/tests/test_analytics.py`:

```python
from decimal import Decimal

from django.test import SimpleTestCase

from listings.analytics import accuracy_metrics


def _d(value):
    return Decimal(str(value))


class AccuracyMetricsTests(SimpleTestCase):
    def test_empty_population_returns_nulls_not_zeros(self):
        # A zero would read as a perfect model. Null reads as "not measured".
        self.assertEqual(accuracy_metrics([]), (None, None, 0))

    def test_perfect_predictions_score_zero_error(self):
        mae, median_ape, n = accuracy_metrics([(_d(100), _d(100)), (_d(200), _d(200))])
        self.assertEqual(mae, Decimal("0"))
        self.assertEqual(median_ape, Decimal("0.0000"))
        self.assertEqual(n, 2)

    def test_median_ape_is_the_middle_ratio_not_the_mean(self):
        # ratios 0.10, 0.20, 3.00 -> median 0.20, mean would be ~1.10
        pairs = [(_d(110), _d(100)), (_d(120), _d(100)), (_d(400), _d(100))]
        mae, median_ape, n = accuracy_metrics(pairs)
        self.assertEqual(median_ape, Decimal("0.2000"))
        self.assertEqual(n, 3)

    def test_mae_is_the_mean_absolute_error_in_whole_vnd(self):
        pairs = [(_d(110), _d(100)), (_d(70), _d(100))]
        mae, median_ape, n = accuracy_metrics(pairs)
        self.assertEqual(mae, Decimal("20"))

    def test_under_and_over_prediction_both_count_as_error(self):
        under = accuracy_metrics([(_d(50), _d(100))])
        over = accuracy_metrics([(_d(150), _d(100))])
        self.assertEqual(under[1], over[1])

    def test_null_prediction_rows_are_excluded(self):
        mae, median_ape, n = accuracy_metrics([(None, _d(100)), (_d(110), _d(100))])
        self.assertEqual(n, 1)
        self.assertEqual(median_ape, Decimal("0.1000"))

    def test_zero_and_null_actual_price_rows_are_excluded_not_divided_by(self):
        mae, median_ape, n = accuracy_metrics(
            [(_d(110), _d(0)), (_d(110), None), (_d(110), _d(100))]
        )
        self.assertEqual(n, 1)

    def test_even_population_averages_the_two_middle_ratios(self):
        pairs = [(_d(110), _d(100)), (_d(130), _d(100))]
        self.assertEqual(accuracy_metrics(pairs)[1], Decimal("0.2000"))
```

- [ ] **Step 2: Run to verify failure**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_analytics -v 2`
Expected: FAIL — `ModuleNotFoundError: No module named 'listings.analytics'`.

- [ ] **Step 3: Implement**

Create `listings/analytics.py`:

```python
"""Read-side aggregation for the pipeline health dashboard.

Kept out of views.py so the maths is testable without a request, and out of
the management commands so the dashboard does not import a Command class.
"""

import statistics
from decimal import Decimal


def accuracy_metrics(pairs):
    """(mae_vnd, median_ape, n) from an iterable of (predicted, actual).

    Median APE rather than mean: it is the metric already reported for the
    shipped model, and with a log-price target a VND mean is dominated by a
    handful of very large listings. Rows with a null prediction or a null/zero
    actual price are excluded -- the zero check is also what keeps the division
    safe. An empty population returns nulls, never zeros: a zero would read as
    a perfect model rather than as "not measured".
    """
    usable = [(p, a) for p, a in pairs if p is not None and a]
    if not usable:
        return None, None, 0
    errors = [abs(p - a) for p, a in usable]
    ratios = [abs(p - a) / a for p, a in usable]
    mae = (sum(errors) / len(errors)).quantize(Decimal("1"))
    median_ape = statistics.median(ratios).quantize(Decimal("0.0001"))
    return mae, median_ape, len(usable)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_analytics -v 2`
Expected: 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add listings/analytics.py listings/tests/test_analytics.py
git commit -m "Add the accuracy_metrics helper for median APE and MAE"
```

---

## Task 7: score_listings writes a ScoringRun and survives model failure

**Files:**
- Modify: `listings/management/commands/score_listings.py`
- Test: `listings/tests/test_scoring.py`

- [ ] **Step 1: Write the failing tests**

Append to `listings/tests/test_scoring.py`:

```python
class ScoringRunTests(TestCase):
    def test_a_run_row_is_written_with_finished_at_set(self):
        _make_listing(source_id="sr1", url="https://alonhadat.com.vn/sr1.html",
                      images=["a.jpg"])
        _score()
        run = ScoringRun.objects.latest("id")
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.scored, 1)

    def test_model_fingerprint_is_recorded(self):
        _make_listing(source_id="sr2", url="https://alonhadat.com.vn/sr2.html",
                      images=["a.jpg"])
        _score()
        run = ScoringRun.objects.latest("id")
        self.assertIsNotNone(run.model_fingerprint)
        self.assertEqual(len(run.model_fingerprint), 12)

    def test_empty_accuracy_population_stores_null_metrics(self):
        _make_listing(source_id="sr3", url="https://alonhadat.com.vn/sr3.html",
                      images=["a.jpg"], price=None)
        _score()
        run = ScoringRun.objects.latest("id")
        self.assertEqual(run.n_compared, 0)
        self.assertIsNone(run.median_ape)
        self.assertIsNone(run.mae_vnd)


class ScoringFailureTests(TestCase):
    def test_model_load_failure_is_counted_and_does_not_kill_the_command(self):
        _make_listing(source_id="mf1", url="https://alonhadat.com.vn/mf1.html",
                      images=["a.jpg"], area_sqm=Decimal("70"),
                      posted_date=timezone.localdate())
        with patch.object(score_listings, "load", side_effect=OSError("no model.pkl")):
            _score()
        run = ScoringRun.objects.latest("id")
        self.assertEqual(run.status_counts, {"model_load_failed": 1})
        self.assertEqual(run.error_count, 1)

    def test_anomaly_rules_still_run_when_the_model_fails(self):
        # This is the point of the change: low_photos and stale_listing do not
        # need the model, and used to die with it.
        listing = _make_listing(
            source_id="mf2", url="https://alonhadat.com.vn/mf2.html",
            images=["a.jpg"], area_sqm=Decimal("70"),
            posted_date=timezone.localdate(),
        )
        with patch.object(score_listings, "load", side_effect=OSError("no model.pkl")):
            _score()
        listing.refresh_from_db()
        self.assertTrue(listing.is_anomaly)
        self.assertEqual(
            listing.anomaly_reason["low_photos"], {"triggered": True, "value": 1}
        )

    def test_inference_failure_is_counted_separately_from_load_failure(self):
        _make_listing(
            source_id="mf3", url="https://alonhadat.com.vn/mf3.html",
            images=["a.jpg"], area_sqm=Decimal("70"),
            posted_date=timezone.localdate(),
        )
        with patch.object(score_listings, "predict", side_effect=ValueError("nan")):
            _score()
        run = ScoringRun.objects.latest("id")
        self.assertEqual(run.status_counts, {"inference_failed": 1})

    def test_a_row_that_fails_to_save_is_counted_and_the_run_continues(self):
        _make_listing(source_id="mf4", url="https://alonhadat.com.vn/mf4.html",
                      images=["a.jpg"])
        good = _make_listing(source_id="mf5", url="https://alonhadat.com.vn/mf5.html",
                             images=["a.jpg", "b.jpg", "c.jpg"])
        original = Listing.save
        calls = {"n": 0}

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise DatabaseError("write failed")
            return original(self, *args, **kwargs)

        with patch.object(Listing, "save", flaky):
            _score()
        run = ScoringRun.objects.latest("id")
        self.assertEqual(run.status_counts, {"listing_save_failed": 1})
        good.refresh_from_db()
        self.assertIsNotNone(good.anomaly_scored_at)
```

Required imports in the test module: `from decimal import Decimal`, `from unittest.mock import patch`, `from django.db import DatabaseError`, `from listings.models import Listing, ScoringRun`, `from listings.management.commands import score_listings`. Add whichever are missing.

- [ ] **Step 2: Run to verify failure**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_scoring.ScoringRunTests listings.tests.test_scoring.ScoringFailureTests -v 2`
Expected: FAIL — `ScoringRun` has no rows, and `score_listings` has no `load` attribute to patch.

- [ ] **Step 3: Implement**

In `listings/management/commands/score_listings.py`, change the imports at the top:

```python
import hashlib
import logging
from decimal import Decimal

import pandas as pd
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from listings.analytics import accuracy_metrics
from listings.ml.dataset import MAX_AREA_SQM, MAX_BEDROOMS
from listings.ml.predict import MODEL_PATH, load, predict
from listings.models import Listing, ScoringRun

logger = logging.getLogger(__name__)

LOW_PHOTOS_THRESHOLD = 3
STALE_LISTING_THRESHOLD_DAYS = 90


def record(statuses, code):
    statuses[code] = statuses.get(code, 0) + 1
    logger.error(f"score_listings: {code}")


def model_fingerprint():
    """First 12 hex of sha256(model.pkl), so a retrain is visible on the chart.

    None rather than a raise if the file is unreadable -- the same run already
    records model_load_failed, and a missing fingerprint must not be a second
    way for scoring to die.
    """
    try:
        return hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()[:12]
    except OSError:
        return None
```

`load` and `predict` are imported by name so the tests can patch them on this module.

Replace the tail of `_predictions` (from `if not rows:` onward) and its signature:

```python
def _predictions(statuses):
    rows = list(
        Listing.objects.filter(
            is_active=True,
            listing_intent="sale",
            area_sqm__gt=0,
            posted_date__isnull=False,
        )
        .exclude(Q(area_sqm__gt=MAX_AREA_SQM) | Q(bedrooms__gt=MAX_BEDROOMS))
        .values("id", "district", "property_type", "area_sqm", "posted_date")
    )
    # Also keeps an empty run from loading the 7.5 MB pickle for nothing.
    if not rows:
        return {}
    # load() is called explicitly, before predict() calls it internally, purely
    # so a missing/corrupt pickle is distinguishable from a bad input row. It
    # is @cache'd, so the second call inside predict() is free.
    try:
        load()
    except Exception:
        record(statuses, "model_load_failed")
        return {}
    frame = pd.DataFrame(rows)
    # area_sqm arrives as Decimal, which makes the column object-dtyped;
    # sklearn rejects that.
    frame["area_sqm"] = frame["area_sqm"].astype(float)
    try:
        values = predict(frame)
    except Exception:
        # A model failure must not take down low_photos/stale_listing, neither
        # of which needs the model. Empty dict, run continues.
        record(statuses, "inference_failed")
        return {}
    return {
        row_id: Decimal(float(value)).quantize(Decimal("1"))
        for row_id, value in zip(frame["id"], values)
    }
```

Keep the existing `_predictions` docstring as-is above the body.

Replace `handle` entirely:

```python
    def handle(self, *args, **options):
        # One timestamp for the whole run, so every row it touches agrees.
        now = timezone.now()
        run = ScoringRun.objects.create(
            started_at=now, model_fingerprint=model_fingerprint()
        )
        statuses = {}
        scored = flagged = 0
        try:
            predictions = _predictions(statuses)
            # §12: each rule scopes its own population from is_active=True.
            # anomaly_reason holds one key per rule that ran; a listing no rule
            # covers is left untouched, not written with an empty dict.
            for listing in Listing.objects.filter(is_active=True):
                # update_fields is built per row, never a fixed list: a scoring
                # write must not touch last_seen_at, and §12 forbids stamping
                # predicted_at on a row the model didn't actually price.
                fields = []
                if listing.pk in predictions:
                    listing.predicted_price = predictions[listing.pk]
                    listing.predicted_at = now
                    fields += ["predicted_price", "predicted_at"]
                elif listing.predicted_price is not None:
                    # §12 calls predicted_price current-state output. A row that
                    # left the population (area re-parsed past the cap,
                    # posted_date nulled by a markup change) must not keep the
                    # last run's number as though the model still stands behind it.
                    listing.predicted_price = listing.predicted_at = None
                    fields += ["predicted_price", "predicted_at"]

                reason = {}
                # images IS NULL means the LDP was never visited
                # (scrape_listings), not zero photos -- low_photos skips those.
                if listing.images is not None:
                    count = len(listing.images)
                    reason["low_photos"] = {
                        "triggered": count < LOW_PHOTOS_THRESHOLD,
                        "value": count,
                    }
                days = listing.days_on_market
                if days is not None:
                    reason["stale_listing"] = {
                        "triggered": days > STALE_LISTING_THRESHOLD_DAYS,
                        "value": days,
                    }
                if reason:
                    listing.is_anomaly = any(
                        rule["triggered"] for rule in reason.values()
                    )
                    listing.anomaly_reason = reason
                    listing.anomaly_scored_at = now
                    fields += ["is_anomaly", "anomaly_reason", "anomaly_scored_at"]

                if fields:
                    try:
                        listing.save(update_fields=fields)
                    except Exception as exc:
                        # One unwritable row must not abandon the rest of the
                        # table half-scored.
                        self.stderr.write(f"save failed for listing {listing.pk}: {exc}")
                        record(statuses, "listing_save_failed")
                        continue
                    if reason:
                        scored += 1
                        flagged += listing.is_anomaly

            run.predicted = len(predictions)
            run.scored = scored
            run.flagged = flagged
            run.mae_vnd, run.median_ape, run.n_compared = accuracy_metrics(
                Listing.objects.filter(
                    is_active=True,
                    listing_intent="sale",
                    predicted_price__isnull=False,
                    price__isnull=False,
                ).values_list("predicted_price", "price")
            )
        except BaseException:
            record(statuses, "run_aborted")
            raise
        finally:
            run.finished_at = timezone.now()
            run.status_counts = statuses or None
            run.error_count = sum(statuses.values())
            run.save()
        self.stdout.write(
            f"predicted={run.predicted} scored={run.scored} flagged={run.flagged} "
            f"compared={run.n_compared} median_ape={run.median_ape} "
            f"status={run.status_counts or '{}'}"
        )
```

Note the `scored`/`flagged` counters moved *after* the save, so a row that failed to write is not counted as scored.

- [ ] **Step 4: Run to verify pass**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_scoring -v 2`
Expected: all `test_scoring` tests pass, including the pre-existing `LowPhotosRuleTests`.

- [ ] **Step 5: Commit**

```bash
git add listings/management/commands/score_listings.py listings/tests/test_scoring.py
git commit -m "Write a ScoringRun per scoring pass and keep anomaly rules alive through model failure"
```

---

## Task 8: Dashboard aggregation queries

**Files:**
- Modify: `listings/analytics.py`
- Test: `listings/tests/test_analytics.py`

- [ ] **Step 1: Write the failing tests**

Append to `listings/tests/test_analytics.py`:

```python
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from listings.analytics import accuracy_trend, run_status, scrapes_per_day, with_bar_pct
from listings.models import ScoringRun, ScrapeRun


class ScrapesPerDayTests(TestCase):
    def _run(self, days_ago, seen, finished=True, errors=0):
        started = timezone.now() - timedelta(days=days_ago)
        return ScrapeRun.objects.create(
            source_site="alonhadat",
            started_at=started,
            finished_at=started + timedelta(minutes=5) if finished else None,
            listings_seen=seen,
            error_count=errors,
        )

    def test_returns_one_entry_per_day_including_days_with_no_run(self):
        self._run(0, 800)
        rows = scrapes_per_day(days=7)
        self.assertEqual(len(rows), 7)
        self.assertEqual([r["status"] for r in rows[:6]], ["no run"] * 6)

    def test_a_day_with_a_healthy_run_reports_its_volume(self):
        self._run(0, 800)
        self.assertEqual(scrapes_per_day(days=3)[-1]["seen"], 800)
        self.assertEqual(scrapes_per_day(days=3)[-1]["status"], "ok")

    def test_two_runs_on_one_day_sum_their_volume(self):
        self._run(0, 400)
        self._run(0, 350)
        row = scrapes_per_day(days=3)[-1]
        self.assertEqual(row["seen"], 750)
        self.assertEqual(row["runs"], 2)

    def test_a_day_of_only_unfinished_runs_reads_aborted_not_zero_volume(self):
        # This is DB reality: ScrapeRun 15/17/18/20 are exactly this shape.
        self._run(0, 0, finished=False)
        self.assertEqual(scrapes_per_day(days=3)[-1]["status"], "aborted")

    def test_a_finished_run_that_saw_nothing_reads_empty_not_aborted(self):
        self._run(0, 0, finished=True)
        self.assertEqual(scrapes_per_day(days=3)[-1]["status"], "empty")

    def test_runs_older_than_the_window_are_excluded(self):
        self._run(40, 900)
        self.assertEqual(sum(r["seen"] for r in scrapes_per_day(days=7)), 0)


class BarPctTests(TestCase):
    def test_largest_value_is_full_width_and_others_scale_to_it(self):
        rows = with_bar_pct([{"seen": 50}, {"seen": 100}], "seen")
        self.assertEqual([r["pct"] for r in rows], [50.0, 100.0])

    def test_all_zero_rows_do_not_divide_by_zero(self):
        rows = with_bar_pct([{"seen": 0}, {"seen": 0}], "seen")
        self.assertEqual([r["pct"] for r in rows], [0, 0])

    def test_empty_input_is_handled(self):
        self.assertEqual(with_bar_pct([], "seen"), [])


class RunStatusTests(TestCase):
    def test_unfinished_and_recent_reads_running(self):
        run = ScrapeRun.objects.create(
            source_site="alonhadat", started_at=timezone.now()
        )
        self.assertEqual(run_status(run), "running")

    def test_unfinished_and_old_reads_aborted(self):
        run = ScrapeRun.objects.create(
            source_site="alonhadat",
            started_at=timezone.now() - timedelta(hours=7),
        )
        self.assertEqual(run_status(run), "aborted")

    def test_finished_with_errors_says_so(self):
        run = ScrapeRun.objects.create(
            source_site="alonhadat", started_at=timezone.now(),
            finished_at=timezone.now(), listings_seen=800, error_count=2,
        )
        self.assertEqual(run_status(run), "ok, 2 errors")


class AccuracyTrendTests(TestCase):
    def _run(self, minutes_ago, ape, fingerprint="aaaaaaaaaaaa"):
        return ScoringRun.objects.create(
            started_at=timezone.now() - timedelta(minutes=minutes_ago),
            finished_at=timezone.now() - timedelta(minutes=minutes_ago),
            median_ape=Decimal(str(ape)),
            n_compared=700,
            model_fingerprint=fingerprint,
        )

    def test_returns_runs_oldest_first_so_the_chart_reads_left_to_right(self):
        self._run(30, "0.30")
        self._run(10, "0.20")
        rows = accuracy_trend()
        self.assertEqual(
            [r["median_ape"] for r in rows], [Decimal("0.3000"), Decimal("0.2000")]
        )

    def test_runs_without_a_metric_are_excluded(self):
        ScoringRun.objects.create(started_at=timezone.now())
        self.assertEqual(accuracy_trend(), [])

    def test_median_ape_is_exposed_as_a_percentage_for_display(self):
        self._run(10, "0.2287")
        self.assertAlmostEqual(accuracy_trend()[0]["median_ape_pct"], 22.87, places=2)

    def test_a_fingerprint_change_marks_the_run_as_a_new_model(self):
        self._run(30, "0.30", fingerprint="aaaaaaaaaaaa")
        self._run(20, "0.30", fingerprint="aaaaaaaaaaaa")
        self._run(10, "0.25", fingerprint="bbbbbbbbbbbb")
        self.assertEqual([r["new_model"] for r in accuracy_trend()], [False, False, True])
```

- [ ] **Step 2: Run to verify failure**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_analytics -v 2`
Expected: FAIL — `ImportError: cannot import name 'scrapes_per_day'`.

- [ ] **Step 3: Implement**

Append to `listings/analytics.py` (and extend the imports at the top of the file):

```python
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from listings.models import ScoringRun, ScrapeRun

# A hard kill (0xC000013A, CLAUDE.md §9) leaves finished_at null with no chance
# to record anything, so elapsed time is the only available evidence. Six hours
# is comfortably longer than any real run and shorter than the daily cadence.
ABORT_AFTER = timedelta(hours=6)


def scrapes_per_day(days=30, now=None):
    """One dict per calendar day, oldest first, with no gaps.

    Days with no run are returned as zero-volume rows rather than omitted: a
    gap in the crawl is the signal the chart exists to show, and dropping the
    row hides it. Bucketing is by TruncDate on started_at, which uses the
    project timezone (UTC).
    """
    now = now or timezone.now()
    start = timezone.localdate(now) - timedelta(days=days - 1)
    rows = (
        ScrapeRun.objects.filter(started_at__date__gte=start)
        .annotate(day=TruncDate("started_at"))
        .values("day")
        .annotate(
            seen=Sum("listings_seen"),
            runs=Count("id"),
            finished=Count("id", filter=Q(finished_at__isnull=False)),
            errors=Sum("error_count"),
        )
    )
    by_day = {row["day"]: row for row in rows}
    out = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        row = by_day.get(day)
        if row is None:
            out.append(
                {"day": day, "seen": 0, "runs": 0, "errors": 0, "status": "no run"}
            )
            continue
        if not row["finished"]:
            status = "aborted"
        elif not row["seen"]:
            status = "empty"
        elif row["errors"]:
            status = f"ok, {row['errors']} errors"
        else:
            status = "ok"
        out.append(
            {
                "day": day,
                "seen": row["seen"] or 0,
                "runs": row["runs"],
                "errors": row["errors"] or 0,
                "status": status,
            }
        )
    return out


def with_bar_pct(rows, key):
    """Add a `pct` to each row, scaled so the largest value is 100."""
    top = max((row[key] or 0 for row in rows), default=0)
    for row in rows:
        row["pct"] = round(float(row[key] or 0) / float(top) * 100, 1) if top else 0
    return rows


def run_status(run, now=None):
    """Plain-language status for one ScrapeRun or ScoringRun row."""
    now = now or timezone.now()
    if run.finished_at is None:
        return "aborted" if now - run.started_at > ABORT_AFTER else "running"
    if not getattr(run, "listings_seen", 1):
        return "empty"
    if run.error_count:
        return f"ok, {run.error_count} error{'s' if run.error_count != 1 else ''}"
    return "ok"


def accuracy_trend(limit=30):
    """Scoring runs that produced a metric, oldest first for left-to-right reading."""
    runs = list(
        ScoringRun.objects.filter(median_ape__isnull=False).order_by("-started_at")[
            :limit
        ]
    )
    runs.reverse()
    rows = []
    previous = None
    for run in runs:
        rows.append(
            {
                "started_at": run.started_at,
                "median_ape": run.median_ape,
                "median_ape_pct": float(run.median_ape) * 100,
                "n_compared": run.n_compared,
                "fingerprint": run.model_fingerprint,
                "new_model": previous is not None
                and run.model_fingerprint != previous,
            }
        )
        previous = run.model_fingerprint
    return rows


def recent_runs(model, limit=15):
    """Last N runs of either model, newest first, each with a status string."""
    runs = list(model.objects.order_by("-started_at")[:limit])
    return [{"run": run, "status": run_status(run)} for run in runs]
```

`getattr(run, "listings_seen", 1)` is what lets `run_status` serve both models: `ScoringRun` has no `listings_seen`, and the default of 1 makes the "empty" branch scrape-only.

- [ ] **Step 4: Run to verify pass**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_analytics -v 2`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add listings/analytics.py listings/tests/test_analytics.py
git commit -m "Add per-day scrape volume and accuracy trend aggregation"
```

---

## Task 9: The /health/ page

Read the `dataviz` skill before writing the template.

**Files:**
- Modify: `listings/views.py`
- Create: `listings/templates/listings/pipeline_health.html`
- Modify: `saiprice/urls.py`
- Test: `listings/tests/test_views.py`

- [ ] **Step 1: Write the failing tests**

Append to `listings/tests/test_views.py`:

```python
class PipelineHealthViewTests(TestCase):
    def test_renders_with_an_empty_database(self):
        # The page must not 500 before the first run ever happens.
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)

    def test_shows_scrape_volume_for_a_day_with_a_run(self):
        started = timezone.now()
        ScrapeRun.objects.create(
            source_site="alonhadat", started_at=started,
            finished_at=started, listings_seen=799, error_count=1,
        )
        response = self.client.get("/health/")
        self.assertContains(response, "799")

    def test_marks_an_unfinished_old_run_as_aborted(self):
        ScrapeRun.objects.create(
            source_site="alonhadat",
            started_at=timezone.now() - timedelta(hours=8),
        )
        response = self.client.get("/health/")
        self.assertContains(response, "aborted")

    def test_shows_median_ape_as_a_percentage(self):
        now = timezone.now()
        ScoringRun.objects.create(
            started_at=now, finished_at=now,
            median_ape=Decimal("0.2287"), n_compared=700,
            model_fingerprint="abc123abc123",
        )
        response = self.client.get("/health/")
        self.assertContains(response, "22.9")

    def test_labels_the_accuracy_figure_as_in_sample(self):
        # Non-negotiable: this number must never be read as held-out accuracy.
        now = timezone.now()
        ScoringRun.objects.create(
            started_at=now, finished_at=now, median_ape=Decimal("0.2287"),
        )
        response = self.client.get("/health/")
        self.assertContains(response, "in-sample")

    def test_expands_status_counts_into_readable_rows(self):
        started = timezone.now()
        ScrapeRun.objects.create(
            source_site="alonhadat", started_at=started, finished_at=started,
            listings_seen=10, error_count=1,
            status_counts={"srp_bot_challenge": 1},
        )
        response = self.client.get("/health/")
        self.assertContains(response, "srp_bot_challenge")
```

Required imports in the test module: `from decimal import Decimal` and `from listings.models import ScoringRun, ScrapeRun`. Add whichever are missing.

- [ ] **Step 2: Run to verify failure**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_views.PipelineHealthViewTests -v 2`
Expected: FAIL — 404 on `/health/`.

- [ ] **Step 3: Add the view**

In `listings/views.py`, extend the imports:

```python
from django.views.generic import DetailView, ListView, TemplateView

from listings.analytics import (
    accuracy_trend,
    recent_runs,
    scrapes_per_day,
    with_bar_pct,
)
from listings.models import Listing, ScoringRun, ScrapeRun
```

Append at the end of the file:

```python
class PipelineHealthView(TemplateView):
    template_name = "listings/pipeline_health.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["scrape_days"] = with_bar_pct(scrapes_per_day(), "seen")
        ctx["accuracy_runs"] = with_bar_pct(accuracy_trend(), "median_ape_pct")
        ctx["recent_scrapes"] = recent_runs(ScrapeRun)
        ctx["recent_scorings"] = recent_runs(ScoringRun)
        return ctx
```

- [ ] **Step 4: Add the route**

In `saiprice/urls.py`, add above the `admin/` line:

```python
    path("health/", views.PipelineHealthView.as_view(), name="pipeline-health"),
```

- [ ] **Step 5: Write the template**

Create `listings/templates/listings/pipeline_health.html`:

```html
{% extends "base.html" %}
{% block title %}Pipeline health — SaiPrice{% endblock %}

{% block content %}
<h1 class="text-2xl mb-1">Pipeline health</h1>
<p class="text-sm text-muted mb-8">
  Crawl volume and model residuals, from ScrapeRun and ScoringRun.
</p>

<section class="mb-12">
  <h2 class="text-lg mb-1">Listings seen per day</h2>
  <p class="text-sm text-muted mb-4">Last 30 days. A day with no bar had no run.</p>
  <ul>
    {% for day in scrape_days %}
    <li class="flex items-center gap-3 py-1 border-b border-line text-sm">
      <span class="w-20 shrink-0 text-muted tabular-nums">{{ day.day|date:"M j" }}</span>
      <span class="flex-1 h-3 bg-line/40 relative">
        {% if day.pct %}
        <span class="block h-3 bg-accent" style="width: {{ day.pct }}%"></span>
        {% endif %}
      </span>
      <span class="w-14 shrink-0 text-right tabular-nums">{{ day.seen }}</span>
      <span class="w-28 shrink-0 text-right text-muted">{{ day.status }}</span>
    </li>
    {% endfor %}
  </ul>
</section>

<section class="mb-12">
  <h2 class="text-lg mb-1">Model error per scoring run</h2>
  <p class="text-sm text-muted mb-4">
    Median absolute percentage error, predicted against listed price.
    <strong class="text-ink">This is in-sample</strong> — the model is fit on
    these same listings, so it reads better than its held-out accuracy and must
    not be quoted as one.
  </p>
  {% for run in accuracy_runs %}
  <div class="flex items-center gap-3 py-1 border-b border-line text-sm">
    <span class="w-20 shrink-0 text-muted tabular-nums">{{ run.started_at|date:"M j" }}</span>
    <span class="flex-1 h-3 bg-line/40">
      <span class="block h-3 bg-accent" style="width: {{ run.pct }}%"></span>
    </span>
    <span class="w-14 shrink-0 text-right tabular-nums">{{ run.median_ape_pct|floatformat:1 }}%</span>
    <span class="w-28 shrink-0 text-right text-muted">
      n={{ run.n_compared }}{% if run.new_model %} · new model{% endif %}
    </span>
  </div>
  {% empty %}
  <p class="text-sm text-muted">No scoring run has produced a metric yet.</p>
  {% endfor %}
</section>

<section class="mb-12">
  <h2 class="text-lg mb-4">Recent scrape runs</h2>
  {% for entry in recent_scrapes %}
  <div class="py-2 border-b border-line text-sm">
    <div class="flex justify-between">
      <span>{{ entry.run.started_at|date:"Y-m-d H:i" }} · {{ entry.run.source_site }}</span>
      <span class="text-muted">{{ entry.status }}</span>
    </div>
    <div class="text-muted">
      seen {{ entry.run.listings_seen }} · new {{ entry.run.inserted }} ·
      updated {{ entry.run.updated }} · skipped {{ entry.run.skipped }}
    </div>
    {% for code, count in entry.run.status_counts.items %}
    <div class="text-muted pl-4">{{ code }} × {{ count }}</div>
    {% endfor %}
  </div>
  {% empty %}
  <p class="text-sm text-muted">No scrape runs recorded.</p>
  {% endfor %}
</section>

<section>
  <h2 class="text-lg mb-4">Recent scoring runs</h2>
  {% for entry in recent_scorings %}
  <div class="py-2 border-b border-line text-sm">
    <div class="flex justify-between">
      <span>{{ entry.run.started_at|date:"Y-m-d H:i" }}</span>
      <span class="text-muted">{{ entry.status }}</span>
    </div>
    <div class="text-muted">
      predicted {{ entry.run.predicted }} · scored {{ entry.run.scored }} ·
      flagged {{ entry.run.flagged }} · compared {{ entry.run.n_compared }}
      {% if entry.run.model_fingerprint %} · model {{ entry.run.model_fingerprint }}{% endif %}
    </div>
    {% for code, count in entry.run.status_counts.items %}
    <div class="text-muted pl-4">{{ code }} × {{ count }}</div>
    {% endfor %}
  </div>
  {% empty %}
  <p class="text-sm text-muted">No scoring runs recorded.</p>
  {% endfor %}
</section>
{% endblock %}
```

Only CLAUDE.md §11 tokens are used (`text-ink`, `text-muted`, `border-line`, `bg-accent`, `bg-line`) plus Tailwind's standard scale. The single inline `style=` carries the computed bar width, which cannot be a utility class because the value is data.

- [ ] **Step 6: Run to verify pass**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings.tests.test_views -v 2`
Expected: all `test_views` tests pass.

- [ ] **Step 7: Rebuild the stylesheet so the new utilities exist**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py tailwind build`
Expected: `assets/css/tailwind.css` rebuilt. The `@source` allowlist in `tailwind_src/source.css` already covers `listings/templates`, so no change there. Confirm the byte count did not jump by an order of magnitude — that would mean the allowlist regressed.

- [ ] **Step 8: Commit**

```bash
git add listings/views.py listings/templates/listings/pipeline_health.html saiprice/urls.py listings/tests/test_views.py
git commit -m "Add the pipeline health dashboard at /health/"
```

---

## Task 10: Full verification

**Files:** none modified unless a check fails.

- [ ] **Step 1: Full test suite**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py test listings -v 1`
Expected: OK, zero failures, zero errors. Record the test count.

- [ ] **Step 2: Model self-check still passes**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe -m listings.ml.predict`
Expected: `ok  70sqm=4,302,344,966 VND  150sqm=12,022,580,496 VND`. This proves the accuracy work did not disturb inference.

- [ ] **Step 3: Real scoring run against the live database**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py score_listings`
Expected: a line of the form `predicted=770 scored=... flagged=... compared=... median_ape=0.xxxx status={}`. Confirm `ScoringRun.objects.latest("id")` now has non-null `median_ape`, `n_compared`, and a 12-character `model_fingerprint`.

- [ ] **Step 4: Dry scrape against the live site, LDP enrichment off**

Run: `cd "d:/coding projects/saiprice" && ./venv/Scripts/python.exe manage.py scrape_listings --source alonhadat --pages 1 --no-ldp-enrich`
Expected: a `seen=... status=...` line. `--no-ldp-enrich` and `--pages 1` keep this to a single SRP request, which does not risk re-triggering alonhadat's wall (CLAUDE.md §9 forbids manual crawl bursts). If the wall is currently active the run reports `srp_bot_challenge` — that is a *successful* demonstration of the taxonomy, not a failure of this task.

- [ ] **Step 5: Drive the live dev server**

Use the `verify` skill. At minimum: start `runserver`, load `/health/`, confirm the per-day bars render with real numbers, the aborted days are marked, and the in-sample wording is visible.

- [ ] **Step 6: Commit any fixes and update the spec's status**

```bash
git add -A
git commit -m "Verify pipeline health end to end against the live dev server"
```

---

## Self-review notes

**Spec coverage.** Every spec section maps to a task: error taxonomy → Tasks 3–5 and 7; the two scraper bugs → Task 5; scoring codes → Task 7; accuracy metric → Tasks 6–7; storage → Task 2; dashboard → Tasks 8–9; logging → Task 1; testing → tests inside every task.

**One addition beyond the committed spec.** `ldp_404` is not in the spec's taxonomy table. It belongs there: §7 calls a 404 a delisting signal, but `_enrich_from_ldp` currently counts it as a generic error. The spec table must be amended with an `ldp_404` row before Task 5 lands. This is the only divergence.

**Naming consistency.** `record(run, code)` in `scrape_listings`, `record(statuses, code)` in `score_listings` — same name, different first argument, because the two commands hold their counters differently (a model instance versus a local dict). Kept the same name deliberately; do not unify them into a shared helper, the signatures genuinely differ.

`with_bar_pct` mutates its input rows in place and returns them. That is fine here — the rows are freshly built dicts owned by the caller — and is why the tests can assert on the return value.
