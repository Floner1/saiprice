# Office Pipeline (maisonoffice.vn) Implementation Plan

> **For agentic workers:** Execute inline with superpowers:executing-plans. Peter's standing choice for this repo: no subagent-driven execution, no worktrees. Steps use checkbox (`- [ ]`) syntax for tracking. Peter reviews the diffs and does all commits. No step in this plan commits, pushes or merges.

**Goal:** Scrape maisonoffice.vn's HCMC office buildings into their own tables, separate from the residential pipeline, and validate a 42-building batch before anything is built on top of it.

**Architecture:** Discovery goes through the sitemap (`sitemap_index.xml` → `van_phong_hcm-sitemap1..5.xml`), because robots.txt disallows `/*/page/` and `/*?`. One page is one building with a USD/m²/month rent range, so the data lands in two new models (`OfficeBuilding`, `OfficeRentHistory`), not in `Listing`. A new site module parses the HTML, and a new `scrape_offices` command fetches through the existing `client.fetch` (shared HTTP client: retries, 1–3 s pacing, reason logging) and saves.

**Tech Stack:** Django 5.2, PostgreSQL, `requests` + `beautifulsoup4` (installed), stdlib `xml.etree.ElementTree` (bundled expat 2.7.1 blocks billion-laughs, and ET never fetches external entities, so `defusedxml` isn't needed). No new dependencies.

**Scope decided 2026-10-01/02 (Peter):** source maisonoffice.vn per CLAUDE.md §2, sitemap discovery, the field list below, schema option B (separate models). First batch capped at 42 buildings: 7 each from `quan-1`, `quan-3`, `quan-2`, `quan-tan-binh`, `quan-binh-thanh`, `tp-thu-duc`.

**Facts this plan relies on (live recon 2026-10-01/02):**
- 1,838 building URLs across 5 HCMC sitemaps, 13 of them repeated across files. Building path: `/cho-thue-van-phong-tphcm/<district-slug>/<slug>/`. 5 slugs contain an extra `/`. Skip `/van_phong_hcm/` (the archive page) and `/van-phong-hcm/...` (1 stray URL).
- 20 district slugs, including two for one district (`huyen-binh-chanh` and `binh-chanh`).
- Stable ID: body class `postid-N`.
- Name: `aside.building-detail-head h1`.
- Address: `aside.building-detail-head div.building-meta p`, e.g. "136-138 Lê Thị Hồng Gấm, Phường Bến Thành, (Quận 1) TP.HCM". The `<title>` address can disagree, so ignore it.
- Spec pairs: `div.detail-content div.info > strong` (label) + `span` (value), in tabs `#thong_so` and `#chi_tiet_gia`. A "Giá thuê" row also appears in `div.fulltext` editor copy, so parsing must stay scoped to `div.detail-content`.
- Rent "27 - 28 usd/m2/tháng" or "Liên hệ". Service fee decimals mix "5,5", "3.8" and "2,17". Parking is quoted in USD or VND.
- JSON-LD is unusable: the offer price is "0", and the geo is Maison's own HQ.

**Out of this plan (each lands when its trigger arrives):**
- Phase 3, ML estimates vs dashboard display: Peter picks after Task 8, and it's planned then.
- Sitemap-based delisting sweep: lands with the first full crawl or scheduling. Until then `is_active` stays True. `save_building` already writes the delisting columns the way the sweep will need, so it lands without a migration.
- `OfficeScrapeRun` table and Task Scheduler entry: land with scheduling. Reusing `ScrapeRun` would leak office runs into `/health/` (`scrapes_per_day` has no source filter).
- CLAUDE.md §2/§4/§5/§6 update: written with Phase 3 so the doc describes the final shape once. §5.1's claim that `Listing` "already accounts for" office becomes wrong the moment this lands.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `listings/scraping/sites/maisonoffice.py` | create | Sitemap parsing, district slug → name, USD/m² parsing, building-page parsing. Pure functions, no DB. |
| `listings/management/commands/scrape_offices.py` | create | Discovery, fetch, parse, print, save (`save_building`). |
| `listings/models.py` | append only | `OfficeBuilding`, `OfficeRentHistory`. No existing class touched. |
| `listings/admin.py` | append only | Register the two office models. |
| `listings/migrations/0009_add_office_buildings.py` | create (generated) | Two `CreateModel` ops, nothing else. |
| `listings/tests/test_offices.py` | create | All office tests. |

Field list (the stored contract):

| Field | Source | Unit / rule |
|---|---|---|
| `source_id` | body class `postid-N` | required |
| `url` | sitemap `loc` | required |
| `name` | header `h1` | required |
| `district` | URL slug → residential naming (`Quận 1`, `Quận Bình Thạnh`, `Thành phố Thủ Đức`, `Huyện Bình Chánh`) | null + note `unmapped_district` if slug unknown. `quan-2` stays `Quận 2`. Maison treats it as its own submarket, residential files it under Thủ Đức |
| `ward` | header address, first `Phường/P./Xã/Thị trấn …` segment (case-insensitive, after NFC normalization), prefix written as `Phường` | null if absent. `P.` is word-anchored so "TP.HCM" never reads as a ward |
| `address_raw` | header address, whitespace collapsed | null if absent |
| `grade` | "Hạng tòa nhà" → `A`/`B`/`C` | null if absent or "Đang cập nhật", note `grade_unrecognized` for any other unparsed text |
| `rent_min_usd`, `rent_max_usd` | "Giá thuê" | USD/m²/month. Null on "Liên hệ". Null + note `rent_not_usd_per_sqm` on any other unit |
| `service_fee_usd` | "Phí dịch vụ" (low end if a range) | USD/m²/month. **0** for "Đã bao gồm trong giá thuê" (bundled into rent, decided 2026-10-03). Also 0 for the live variants "Bao gồm …" and the typo "Đã boa gồm …", but never when the text contains "chưa" ("Chưa bao gồm" = not included). Null for the no-figure phrases. Null + note `service_fee_not_usd_per_sqm` on other units. "sqm" counts as m² |
| `typical_floor_sqm` | "Tầng điển hình" via `parse_sqm` | m². A separator before exactly 3 digits is thousands, otherwise decimal ("1,163" → 1163, "167.75" stays). "Tháp/Tower/Khối/Block X:" labels are stripped first. First figure in the text: low end of a range, first tower/block (raw text stays in `specs_raw`). Null for "Đang cập nhật". Null + note `floor_area_unrecognized` if no `m2`/`m²` |

"Liên hệ", "Cập nhật"/"Đang cập nhật", "Thỏa thuận" and "Chưa bao gồm" (the source's "no figure published") null rent, fee, floor and grade without a note. Fixes A–D (number format, tower labels, NFC + ward case, vocabulary) were added 2026-10-04 after the full crawl's validation. That was added after Checkpoint A's first run on 2026-10-03, along with the bundled-fee 0 and the `P.` ward abbreviation.
| `specs_raw` | every `div.info` pair | raw strings, first label wins |
| `source_modified_at` | sitemap `lastmod` | timezone-aware datetime |
| `first_seen_at`, `last_seen_at`, `is_active`, `delisted_at` | pipeline | same semantics as `Listing` |

---

## Task 1: Sitemap and district helpers

**Files:**
- Create: `listings/scraping/sites/maisonoffice.py`
- Create: `listings/tests/test_offices.py`

- [ ] **Step 1: Write the failing tests**

`listings/tests/test_offices.py`:

```python
from django.test import SimpleTestCase

from listings.scraping.sites import maisonoffice

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?><?xml-stylesheet type="text/xsl" href="//maisonoffice.vn/main-sitemap.xsl"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://maisonoffice.vn/post-sitemap1.xml</loc><lastmod>2026-10-01T10:50:01+00:00</lastmod></sitemap>
<sitemap><loc>https://maisonoffice.vn/van_phong_hcm-sitemap1.xml</loc><lastmod>2026-10-01T08:22:50+00:00</lastmod></sitemap>
<sitemap><loc>https://maisonoffice.vn/vp_dien_tich_hcm-sitemap1.xml</loc><lastmod>2025-10-31T09:55:34+00:00</lastmod></sitemap>
</sitemapindex>"""


class SitemapTests(SimpleTestCase):
    def test_parses_loc_and_lastmod_from_an_index(self):
        self.assertEqual(
            maisonoffice.parse_sitemap(SITEMAP_INDEX.encode()),
            [
                ("https://maisonoffice.vn/post-sitemap1.xml", "2026-10-01T10:50:01+00:00"),
                ("https://maisonoffice.vn/van_phong_hcm-sitemap1.xml", "2026-10-01T08:22:50+00:00"),
                ("https://maisonoffice.vn/vp_dien_tich_hcm-sitemap1.xml", "2025-10-31T09:55:34+00:00"),
            ],
        )

    def test_only_hcm_office_sitemaps_match(self):
        locs = [loc for loc, _ in maisonoffice.parse_sitemap(SITEMAP_INDEX.encode())]
        self.assertEqual(
            [loc for loc in locs if maisonoffice.HCM_SITEMAP.search(loc)],
            ["https://maisonoffice.vn/van_phong_hcm-sitemap1.xml"],
        )


class DistrictTests(SimpleTestCase):
    def test_building_urls_yield_their_district_slug(self):
        base = "https://maisonoffice.vn/cho-thue-van-phong-tphcm"
        self.assertEqual(maisonoffice.district_slug(f"{base}/quan-1/the-one-sai-gon/"), "quan-1")
        # 5 live slugs contain an extra "/" -- still buildings
        self.assertEqual(
            maisonoffice.district_slug(f"{base}/quan-tan-binh/biet-thu-149/33-bvt/"),
            "quan-tan-binh",
        )

    def test_non_building_urls_yield_none(self):
        self.assertIsNone(maisonoffice.district_slug("https://maisonoffice.vn/van_phong_hcm/"))
        self.assertIsNone(
            maisonoffice.district_slug(
                "https://maisonoffice.vn/van-phong-hcm/quan-binh-thanh/toa-nha-aurora/"
            )
        )

    def test_slugs_map_to_residential_naming(self):
        cases = {
            "quan-1": "Quận 1",
            "quan-12": "Quận 12",
            "quan-2": "Quận 2",
            "quan-binh-thanh": "Quận Bình Thạnh",
            "tp-thu-duc": "Thành phố Thủ Đức",
            "huyen-binh-chanh": "Huyện Bình Chánh",
            "binh-chanh": "Huyện Bình Chánh",
        }
        for slug, name in cases.items():
            with self.subTest(slug=slug):
                self.assertEqual(maisonoffice.district_name(slug), name)

    def test_unknown_slug_maps_to_none(self):
        self.assertIsNone(maisonoffice.district_name("huyen-can-gio"))
```

- [ ] **Step 2: Run to verify failure**

Run: `venv\Scripts\python.exe manage.py test listings.tests.test_offices`
Expected: ERROR, `ImportError: cannot import name 'maisonoffice'`.

- [ ] **Step 3: Implement**

`listings/scraping/sites/maisonoffice.py`:

```python
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

BASE_URL = "https://maisonoffice.vn"
# robots.txt disallows /*/page/ and /*?, which rules out the paginated district
# archives; the sitemap is the sanctioned way to enumerate buildings.
SITEMAP_INDEX = f"{BASE_URL}/sitemap_index.xml"
HCM_SITEMAP = re.compile(r"/van_phong_hcm-sitemap\d*\.xml$")
BUILDING_PREFIX = "cho-thue-van-phong-tphcm"
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

# Residential's Listing.district naming, so the two pipelines line up.
# Numbered districts go through the pattern in district_name. quan-2 stays
# "Quận 2": Maison treats it as its own submarket while alonhadat files the
# same area under Thành phố Thủ Đức.
NAMED_DISTRICTS = {
    "quan-tan-binh": "Quận Tân Bình",
    "quan-binh-thanh": "Quận Bình Thạnh",
    "quan-phu-nhuan": "Quận Phú Nhuận",
    "quan-go-vap": "Quận Gò Vấp",
    "quan-tan-phu": "Quận Tân Phú",
    "quan-binh-tan": "Quận Bình Tân",
    "tp-thu-duc": "Thành phố Thủ Đức",
    "huyen-binh-chanh": "Huyện Bình Chánh",
    "binh-chanh": "Huyện Bình Chánh",
}


def parse_sitemap(xml):
    """(loc, lastmod) for every entry of a sitemap or a sitemap index.

    Stdlib ET on third-party XML is safe here: the bundled expat (>= 2.4.1)
    blocks billion-laughs and ET never resolves external entities.
    """
    root = ET.fromstring(xml)
    return [
        (entry.findtext(f"{SITEMAP_NS}loc"), entry.findtext(f"{SITEMAP_NS}lastmod"))
        for entry in root
    ]


def district_slug(url):
    parts = urlsplit(url).path.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == BUILDING_PREFIX:
        return parts[1]
    return None


def district_name(slug):
    match = re.fullmatch(r"quan-(\d+)", slug)
    if match:
        return f"Quận {match.group(1)}"
    return NAMED_DISTRICTS.get(slug)
```

- [ ] **Step 4: Run to verify pass**

Run: `venv\Scripts\python.exe manage.py test listings.tests.test_offices`
Expected: 6 tests, OK.

---

## Task 2: USD per m² parser

**Files:**
- Modify: `listings/scraping/sites/maisonoffice.py`
- Modify: `listings/tests/test_offices.py`

- [ ] **Step 1: Write the failing tests** (append to `test_offices.py`, add `from decimal import Decimal` to the imports)

```python
class ParseUsdPerSqmTests(SimpleTestCase):
    def test_ranges_and_single_values(self):
        cases = {
            "27 - 28 usd/m2/tháng": (Decimal("27"), Decimal("28")),
            "14 – 15 USD/m²/tháng": (Decimal("14"), Decimal("15")),
            "6 usd/m2/tháng": (Decimal("6"), Decimal("6")),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(maisonoffice.parse_usd_per_sqm(text), expected)

    def test_comma_and_dot_are_both_decimal_points(self):
        # parse_vnd reads "." as a thousands separator; here "3.8" must stay 3.8
        self.assertEqual(maisonoffice.parse_usd_per_sqm("3.8 usd/m2/tháng")[0], Decimal("3.8"))
        self.assertEqual(maisonoffice.parse_usd_per_sqm("5,5 usd/m2/tháng")[0], Decimal("5.5"))
        self.assertEqual(maisonoffice.parse_usd_per_sqm("2,17 usd/m2/tháng")[0], Decimal("2.17"))

    def test_non_usd_per_sqm_and_empty_give_none(self):
        for text in ("Liên hệ", "", None, "250.000 vnd/m2/tháng", "1500 usd/tháng"):
            with self.subTest(text=text):
                self.assertEqual(maisonoffice.parse_usd_per_sqm(text), (None, None))
```

- [ ] **Step 2: Run to verify failure**

Run: `venv\Scripts\python.exe manage.py test listings.tests.test_offices.ParseUsdPerSqmTests`
Expected: ERROR, `AttributeError: module ... has no attribute 'parse_usd_per_sqm'`.

- [ ] **Step 3: Implement** (append to `maisonoffice.py`, add `from decimal import Decimal`)

```python
def parse_usd_per_sqm(text):
    """(low, high) in USD/m2/month, or (None, None) for anything else.

    "Liên hệ" and an unrecognised unit both come back empty; parse_building
    tells them apart, because only the second one is worth a note.
    """
    lower = (text or "").lower()
    if "usd" not in lower or not re.search(r"m2|m²", lower):
        return None, None
    # ponytail: "," and "." both read as decimal points. Per-m2 USD figures sit
    # far below 1,000, so neither is ever a thousands separator here -- unlike
    # parse_vnd, where "3.8" would become 38.
    numbers = re.findall(r"\d+(?:[.,]\d+)?", lower.split("usd")[0])
    if not numbers:
        return None, None
    values = [Decimal(n.replace(",", ".")) for n in numbers]
    return values[0], values[-1]
```

- [ ] **Step 4: Run to verify pass**

Run: `venv\Scripts\python.exe manage.py test listings.tests.test_offices`
Expected: 9 tests, OK.

---

## Task 3: Building page parser

**Files:**
- Modify: `listings/scraping/sites/maisonoffice.py`
- Modify: `listings/tests/test_offices.py`

- [ ] **Step 1: Write the failing tests** (append; add `from listings.scraping.parsers import RequiredFieldMissing` to imports)

```python
# Trimmed from the live The One Sài Gòn page (2026-10-01): same selectors,
# same label/value markup, plus a decoy "Giá thuê" row in the editor copy that
# parsing must not pick up.
BUILDING_HTML = """<html><body class="wp-singular single single-van_phong_hcm postid-30648 wp-custom-logo">
<aside class="building-detail-head">
<h1>The One Sài Gòn</h1>
<div class="building-meta"><p>136-138  Lê Thị Hồng Gấm, Phường Bến Thành, (Quận 1) TP.HCM</p></div>
</aside>
<div class="detail-content">
<div id="thong_so" class="box-item tab-item thong_so"><div class="tab-content"><ul>
<li><div class="info"><strong>Hạng tòa nhà </strong><span>Hạng B</span></div></li>
<li><div class="info"><strong>Tầng điển hình </strong><span>Diện tích 627m2/sàn</span></div></li>
</ul></div></div>
<div id="chi_tiet_gia" class="box-item tab-item thong_so"><div class="box-item tab-content gia_thue"><ul>
<li><div class="info"><strong>Giá thuê </strong><span>27 - 28 usd/m2/tháng</span></div></li>
<li><div class="info"><strong>Phí dịch vụ </strong><span>5,5 usd/m2/tháng</span></div></li>
<li><div class="info"><strong>Đỗ ô tô </strong><span>2.500.000 vnd/xe/tháng</span></div></li>
</ul></div></div>
</div>
<div class="fulltext"><table><tr><td>Giá thuê</td><td>$99 – $99/ m2</td></tr></table></div>
</body></html>"""
BUILDING_URL = "https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/the-one-sai-gon/"


class ParseBuildingTests(SimpleTestCase):
    def test_full_page(self):
        fields, notes = maisonoffice.parse_building(BUILDING_HTML, BUILDING_URL)
        self.assertEqual(
            fields,
            {
                "source_id": "30648",
                "url": BUILDING_URL,
                "name": "The One Sài Gòn",
                "district": "Quận 1",
                "ward": "Phường Bến Thành",
                "address_raw": "136-138 Lê Thị Hồng Gấm, Phường Bến Thành, (Quận 1) TP.HCM",
                "grade": "B",
                "rent_min_usd": Decimal("27"),
                "rent_max_usd": Decimal("28"),
                "service_fee_usd": Decimal("5.5"),
                "typical_floor_sqm": Decimal("627"),
                "specs_raw": {
                    "Hạng tòa nhà": "Hạng B",
                    "Tầng điển hình": "Diện tích 627m2/sàn",
                    "Giá thuê": "27 - 28 usd/m2/tháng",
                    "Phí dịch vụ": "5,5 usd/m2/tháng",
                    "Đỗ ô tô": "2.500.000 vnd/xe/tháng",
                },
            },
        )
        self.assertEqual(notes, [])

    def test_contact_for_price_and_missing_grade_are_plain_nulls(self):
        html = BUILDING_HTML.replace("27 - 28 usd/m2/tháng", "Liên hệ").replace(
            "<li><div class=\"info\"><strong>Hạng tòa nhà </strong><span>Hạng B</span></div></li>", ""
        )
        fields, notes = maisonoffice.parse_building(html, BUILDING_URL)
        self.assertEqual((fields["rent_min_usd"], fields["rent_max_usd"]), (None, None))
        self.assertIsNone(fields["grade"])
        self.assertEqual(notes, [])

    def test_unexpected_units_are_nulled_and_noted(self):
        html = (
            BUILDING_HTML.replace("27 - 28 usd/m2/tháng", "250.000 vnd/m2/tháng")
            .replace("5,5 usd/m2/tháng", "Đã bao gồm")
            .replace("Diện tích 627m2/sàn", "Đang cập nhật")
            .replace("Hạng B", "Hạng S")
        )
        fields, notes = maisonoffice.parse_building(html, BUILDING_URL)
        self.assertIsNone(fields["rent_min_usd"])
        self.assertIsNone(fields["service_fee_usd"])
        self.assertIsNone(fields["typical_floor_sqm"])
        self.assertIsNone(fields["grade"])
        self.assertEqual(
            sorted(notes),
            [
                "floor_area_unrecognized",
                "grade_unrecognized",
                "rent_not_usd_per_sqm",
                "service_fee_not_usd_per_sqm",
            ],
        )

    def test_unmapped_district_is_noted(self):
        url = BUILDING_URL.replace("/quan-1/", "/huyen-can-gio/")
        fields, notes = maisonoffice.parse_building(BUILDING_HTML, url)
        self.assertIsNone(fields["district"])
        self.assertEqual(notes, ["unmapped_district"])

    def test_missing_identity_raises(self):
        with self.assertRaises(RequiredFieldMissing) as ctx:
            maisonoffice.parse_building(BUILDING_HTML.replace("postid-30648", ""), BUILDING_URL)
        self.assertEqual(ctx.exception.field, "source_id")
        with self.assertRaises(RequiredFieldMissing) as ctx:
            maisonoffice.parse_building(BUILDING_HTML.replace("The One Sài Gòn", ""), BUILDING_URL)
        self.assertEqual(ctx.exception.field, "name")
```

- [ ] **Step 2: Run to verify failure**

Run: `venv\Scripts\python.exe manage.py test listings.tests.test_offices.ParseBuildingTests`
Expected: ERROR, `AttributeError: ... no attribute 'parse_building'`.

- [ ] **Step 3: Implement** (append to `maisonoffice.py`, add `from bs4 import BeautifulSoup` and `from ..parsers import RequiredFieldMissing, _parse_area`)

```python
def parse_building(html, url):
    """(fields, notes) for one building page.

    notes names each nullable field that had text but did not parse, so a
    unit or markup change shows up in the run summary instead of passing as
    an ordinary null.
    """
    soup = BeautifulSoup(html, "html.parser")
    notes = []
    classes = soup.body.get("class", []) if soup.body else []
    source_id = next((c[len("postid-"):] for c in classes if c.startswith("postid-")), None)
    if not source_id:
        raise RequiredFieldMissing("source_id")
    h1 = soup.select_one("aside.building-detail-head h1")
    name = h1.get_text(" ", strip=True) if h1 else ""
    if not name:
        raise RequiredFieldMissing("name")

    # Scoped to div.detail-content: the editor copy in div.fulltext repeats
    # labels like "Giá thuê" with its own, differently formatted values.
    specs = {}
    for info in soup.select("div.detail-content div.info"):
        label, value = info.find("strong"), info.find("span")
        if label and value:
            specs.setdefault(label.get_text(" ", strip=True), value.get_text(" ", strip=True))

    meta = soup.select_one("aside.building-detail-head div.building-meta p")
    address = re.sub(r"\s+", " ", meta.get_text(" ", strip=True)) if meta else None
    ward = re.search(r"(?:Phường|Xã|Thị trấn) [^,()]+", address or "")

    slug = district_slug(url)
    district = district_name(slug) if slug else None
    if slug and district is None:
        notes.append("unmapped_district")

    rent_text = specs.get("Giá thuê")
    rent_min, rent_max = parse_usd_per_sqm(rent_text)
    if rent_text and rent_min is None and "liên hệ" not in rent_text.lower():
        notes.append("rent_not_usd_per_sqm")

    fee_text = specs.get("Phí dịch vụ")
    fee, _ = parse_usd_per_sqm(fee_text)
    if fee_text and fee is None and "liên hệ" not in fee_text.lower():
        notes.append("service_fee_not_usd_per_sqm")

    grade_text = specs.get("Hạng tòa nhà")
    grade = re.search(r"Hạng\s*([ABC])\b", grade_text or "")
    if grade_text and grade is None:
        notes.append("grade_unrecognized")

    floor_text = specs.get("Tầng điển hình")
    floor = _parse_area(floor_text) if floor_text and re.search(r"m2|m²", floor_text) else None
    if floor_text and floor is None:
        notes.append("floor_area_unrecognized")

    return {
        "source_id": source_id,
        "url": url,
        "name": name,
        "district": district,
        "ward": ward.group().strip() if ward else None,
        "address_raw": address,
        "grade": grade.group(1) if grade else None,
        "rent_min_usd": rent_min,
        "rent_max_usd": rent_max,
        "service_fee_usd": fee,
        "typical_floor_sqm": floor,
        "specs_raw": specs or None,
    }, notes
```

- [ ] **Step 4: Run to verify pass**

Run: `venv\Scripts\python.exe manage.py test listings.tests.test_offices`
Expected: 14 tests, OK.

---

## Task 4: `scrape_offices` command, print-only

**Files:**
- Create: `listings/management/commands/scrape_offices.py`
- Modify: `listings/tests/test_offices.py`

- [ ] **Step 1: Write the failing test** (append; add `import io`, `from unittest.mock import Mock, patch`, `from django.core.management import call_command`, `from listings.management.commands import scrape_offices`)

```python
HCM_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://maisonoffice.vn/van_phong_hcm/</loc><lastmod>2026-09-28T03:30:00+00:00</lastmod></url>
<url><loc>https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/a/</loc><lastmod>2026-09-28T03:30:00+00:00</lastmod></url>
<url><loc>https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/b/</loc><lastmod>2026-09-27T03:30:00+00:00</lastmod></url>
<url><loc>https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/a/</loc><lastmod>2026-09-28T03:30:00+00:00</lastmod></url>
<url><loc>https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-3/c/</loc><lastmod>2026-09-26T03:30:00+00:00</lastmod></url>
<url><loc>https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-7/d/</loc><lastmod>2026-09-26T03:30:00+00:00</lastmod></url>
</urlset>"""


def _fake_site():
    pages = {
        maisonoffice.SITEMAP_INDEX: SITEMAP_INDEX,
        "https://maisonoffice.vn/van_phong_hcm-sitemap1.xml": HCM_SITEMAP_XML,
        "https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/a/": BUILDING_HTML,
        "https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-3/c/": BUILDING_HTML.replace(
            "postid-30648", "postid-11"
        ),
    }
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        body = pages[url]
        return Mock(text=body, content=body.encode()), None

    return fake_fetch, fetched


class ScrapeOfficesCommandTests(SimpleTestCase):
    def test_capped_districts_fetch_only_the_sample(self):
        fake_fetch, fetched = _fake_site()
        out = io.StringIO()
        with patch.object(scrape_offices, "fetch", side_effect=fake_fetch):
            call_command(
                "scrape_offices", "--districts", "quan-1,quan-3", "--per-district", "1",
                stdout=out, stderr=io.StringIO(),
            )
        # index, the one HCM sitemap, then one building per wanted district:
        # no other sitemap, no archive URL, no duplicate, no quan-7
        self.assertEqual(
            fetched,
            [
                maisonoffice.SITEMAP_INDEX,
                "https://maisonoffice.vn/van_phong_hcm-sitemap1.xml",
                "https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/a/",
                "https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-3/c/",
            ],
        )
        lines = out.getvalue().splitlines()
        self.assertIn("30648 | Quận 1 | B | rent 27-28 | fee 5.5 | floor 627", lines[0])
        self.assertTrue(lines[1].startswith("11 | Quận 3 |"))
        self.assertIn("'parsed': 2", lines[-1])
```

The second building reuses the quan-1 page body, so its district comes from its own URL (`quan-3`). That's the point: district is read from the URL, never from the page.

- [ ] **Step 2: Run to verify failure**

Run: `venv\Scripts\python.exe manage.py test listings.tests.test_offices.ScrapeOfficesCommandTests`
Expected: ERROR, `ImportError: cannot import name 'scrape_offices'`.

- [ ] **Step 3: Implement** `listings/management/commands/scrape_offices.py`:

```python
from collections import Counter
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from listings.scraping.client import fetch
from listings.scraping.parsers import RequiredFieldMissing
from listings.scraping.sites import maisonoffice


class Command(BaseCommand):
    help = (
        "Office buildings from maisonoffice.vn (rent quotes in USD/m2/month), "
        "discovered through the sitemap. Separate from the residential pipeline."
    )

    def add_arguments(self, parser):
        parser.add_argument("--districts", help="comma-separated URL slugs, e.g. quan-1,quan-3")
        parser.add_argument("--per-district", type=int, help="cap buildings per district")

    def _sitemap(self, url):
        # A missing sitemap aborts: discovery has to be complete or not at all.
        response, error = fetch(url)
        if error:
            raise CommandError(f"sitemap {url}: {error}")
        return maisonoffice.parse_sitemap(response.content)

    def handle(self, *args, **options):
        wanted = set(options["districts"].split(",")) if options["districts"] else None
        cap = options["per_district"]
        entries = {}
        for sitemap_url, _ in self._sitemap(maisonoffice.SITEMAP_INDEX):
            if maisonoffice.HCM_SITEMAP.search(sitemap_url):
                for loc, lastmod in self._sitemap(sitemap_url):
                    # 13 live URLs appear in two sitemap files
                    entries.setdefault(loc, lastmod)

        taken = Counter()
        counts = Counter()
        for url, lastmod in entries.items():
            slug = maisonoffice.district_slug(url)
            if slug is None or (wanted and slug not in wanted):
                continue
            if cap and taken[slug] >= cap:
                continue
            taken[slug] += 1
            response, error = fetch(url)
            if error:
                counts[f"page_{error}"] += 1
                self.stderr.write(f"{error} for {url}")
                continue
            try:
                fields, notes = maisonoffice.parse_building(response.text, url)
            except RequiredFieldMissing as exc:
                counts["required_field_missing"] += 1
                self.stderr.write(f"skipped {url}: {exc}")
                continue
            fields["source_modified_at"] = datetime.fromisoformat(lastmod) if lastmod else None
            counts["parsed"] += 1
            counts.update(notes)
            self.stdout.write(self._line(fields, notes))
        self.stdout.write(f"summary: {dict(counts)}")

    def _line(self, fields, notes):
        parts = [
            fields["source_id"],
            fields["district"] or "-",
            fields["grade"] or "-",
            f"rent {fields['rent_min_usd']}-{fields['rent_max_usd']}",
            f"fee {fields['service_fee_usd']}",
            f"floor {fields['typical_floor_sqm']}",
            fields["ward"] or "-",
            fields["name"],
        ]
        if notes:
            parts.append(f"notes={notes}")
        return " | ".join(parts)
```

- [ ] **Step 4: Run to verify pass**

Run: `venv\Scripts\python.exe manage.py test listings.tests.test_offices`
Expected: 15 tests, OK.

- [ ] **Step 5: Full suite**

Run: `venv\Scripts\python.exe manage.py test listings`
Expected: 317 tests (302 + 15), OK.

---

## Checkpoint A: live parse of the batch (no DB writes)

- [ ] **Step 1: Run against the live site**

```powershell
$env:PYTHONIOENCODING='utf-8'; venv\Scripts\python.exe manage.py scrape_offices --districts quan-1,quan-3,quan-2,quan-tan-binh,quan-binh-thanh,tp-thu-duc --per-district 7
```

Expected: 48 requests (1 index, 5 sitemaps, 42 pages) at 1–3 s spacing, 42 printed lines, and a summary with `'parsed': 42` or fewer plus any `page_*` and note codes.

- [ ] **Step 2: Read every line.** Stop and report to Peter, without moving on to Task 5, if any of these show up:
  - a note code
  - `rent` outside roughly 5–100 USD
  - `fee` outside roughly 0.5–15 USD
  - `floor` outside roughly 50–5,000 m²
  - a `-` district

  This is the FALLBACK rule: don't guess units. If none show up, continue.

---

## Task 5: Models, migration, admin

**Files:**
- Modify (append only): `listings/models.py`
- Modify (append only): `listings/admin.py`
- Create (generated): `listings/migrations/0009_add_office_buildings.py`

- [ ] **Step 1: Append the models** to the end of `listings/models.py`:

```python
class OfficeBuilding(models.Model):
    """One maisonoffice.vn building: a broker's rent quote, not a unit listing.

    Apart from Listing on purpose. Listing.price is a whole-VND total while
    this source quotes a USD/m2/month range, and residential readers (API, ML
    dataset, scoring, the delisting sweep) must never see these rows.
    """

    # ponytail: one office source, so source_id alone is unique. Add a
    # source_site column if a second office source ever lands.
    source_id = models.CharField(max_length=64, unique=True)
    url = models.URLField(max_length=500, unique=True)
    name = models.CharField(max_length=255)
    district = models.CharField(max_length=100, null=True)
    ward = models.CharField(max_length=100, null=True)
    address_raw = models.TextField(null=True)
    grade = models.CharField(max_length=2, null=True)
    rent_min_usd = models.DecimalField(max_digits=7, decimal_places=2, null=True)
    rent_max_usd = models.DecimalField(max_digits=7, decimal_places=2, null=True)
    service_fee_usd = models.DecimalField(max_digits=7, decimal_places=2, null=True)
    typical_floor_sqm = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    specs_raw = models.JSONField(null=True)
    source_modified_at = models.DateTimeField(null=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField()
    # ponytail: no sweep yet, so every row stays active. A sitemap-based sweep
    # lands with the first full crawl; save_building already maintains both
    # columns, so it needs no migration.
    is_active = models.BooleanField(default=True)
    delisted_at = models.DateTimeField(null=True)


class OfficeRentHistory(models.Model):
    """One row per observed rent change, inserted on first sight too.

    The building row is overwritten every pass, so a change can't be
    reconstructed later -- same reasoning as PriceHistory and ScoringRun.
    """

    building = models.ForeignKey(OfficeBuilding, on_delete=models.CASCADE)
    rent_min_usd = models.DecimalField(max_digits=7, decimal_places=2, null=True)
    rent_max_usd = models.DecimalField(max_digits=7, decimal_places=2, null=True)
    observed_at = models.DateTimeField()
```

- [ ] **Step 2: Register in admin.** In `listings/admin.py`, change the import line to

```python
from .models import (
    Agent, Listing, OfficeBuilding, OfficeRentHistory, PriceHistory, ScoringRun, ScrapeRun,
)
```

and append:

```python
@admin.register(OfficeBuilding)
class OfficeBuildingAdmin(admin.ModelAdmin):
    list_display = (
        "id", "source_id", "name", "district", "grade", "rent_min_usd",
        "rent_max_usd", "service_fee_usd", "typical_floor_sqm", "last_seen_at",
        "is_active",
    )


admin.site.register(OfficeRentHistory)
```

- [ ] **Step 3: Generate the migration**

Run: `venv\Scripts\python.exe manage.py makemigrations listings --name add_office_buildings`
Expected: `listings\migrations\0009_add_office_buildings.py` with exactly `Create model OfficeBuilding` and `Create model OfficeRentHistory`. **If any other operation appears (e.g. an AlterField on a residential model), stop and report. Don't apply it.**

- [ ] **Step 4: Check and apply**

Run: `venv\Scripts\python.exe manage.py makemigrations --check --dry-run` → `No changes detected`.
Run: `venv\Scripts\python.exe manage.py migrate listings` → `Applying listings.0009_add_office_buildings... OK`.

---

## Task 6: Saving

**Files:**
- Modify: `listings/management/commands/scrape_offices.py`
- Modify: `listings/tests/test_offices.py`

- [ ] **Step 1: Write the failing tests** (append; add `from django.test import TestCase`, `from django.utils import timezone`, `from listings.models import OfficeBuilding, OfficeRentHistory`, `from listings.management.commands.scrape_offices import save_building`)

```python
def _fields(**overrides):
    fields, _ = maisonoffice.parse_building(BUILDING_HTML, BUILDING_URL)
    fields["source_modified_at"] = None
    return {**fields, **overrides}


class SaveBuildingTests(TestCase):
    def test_first_save_inserts_building_and_one_history_row(self):
        self.assertTrue(save_building(_fields()))
        building = OfficeBuilding.objects.get(source_id="30648")
        self.assertEqual(building.district, "Quận 1")
        self.assertEqual(
            list(OfficeRentHistory.objects.values_list("rent_min_usd", "rent_max_usd")),
            [(Decimal("27"), Decimal("28"))],
        )

    def test_unchanged_rent_adds_no_history_and_reactivates(self):
        save_building(_fields())
        OfficeBuilding.objects.update(is_active=False, delisted_at=timezone.now())
        self.assertFalse(save_building(_fields()))
        building = OfficeBuilding.objects.get()
        self.assertTrue(building.is_active)
        self.assertIsNone(building.delisted_at)
        self.assertEqual(OfficeRentHistory.objects.count(), 1)

    def test_changed_rent_adds_a_history_row(self):
        save_building(_fields())
        save_building(_fields(rent_min_usd=Decimal("29"), rent_max_usd=Decimal("30")))
        self.assertEqual(
            list(
                OfficeRentHistory.objects.order_by("id").values_list(
                    "rent_min_usd", "rent_max_usd"
                )
            ),
            [(Decimal("27"), Decimal("28")), (Decimal("29"), Decimal("30"))],
        )


class ScrapeOfficesSaveTests(TestCase):
    def _run(self, *extra):
        fake_fetch, _ = _fake_site()
        out = io.StringIO()
        with patch.object(scrape_offices, "fetch", side_effect=fake_fetch):
            call_command(
                "scrape_offices", "--districts", "quan-1,quan-3", "--per-district", "1",
                *extra, stdout=out, stderr=io.StringIO(),
            )
        return out.getvalue()

    def test_default_run_saves(self):
        out = self._run()
        self.assertEqual(
            sorted(OfficeBuilding.objects.values_list("source_id", "district")),
            [("11", "Quận 3"), ("30648", "Quận 1")],
        )
        self.assertIn("'inserted': 2", out)

    def test_dry_run_writes_nothing(self):
        self._run("--dry-run")
        self.assertEqual(OfficeBuilding.objects.count(), 0)
```

- [ ] **Step 2: Run to verify failure**

Run: `venv\Scripts\python.exe manage.py test listings.tests.test_offices`
Expected: ERROR, `ImportError: cannot import name 'save_building'`.

- [ ] **Step 3: Implement.** In `scrape_offices.py`, add the imports `from django.utils import timezone` and `from listings.models import OfficeBuilding, OfficeRentHistory`, and add above `class Command`:

```python
def save_building(fields):
    """Upsert one building; True if it was new.

    A rent history row goes in on first sight and whenever the stored range
    differs, compared before the overwrite -- the same order as upsert().
    """
    now = timezone.now()
    existing = OfficeBuilding.objects.filter(source_id=fields["source_id"]).first()
    building, created = OfficeBuilding.objects.update_or_create(
        source_id=fields["source_id"],
        defaults={
            **{k: v for k, v in fields.items() if k != "source_id"},
            "last_seen_at": now,
            "is_active": True,
            "delisted_at": None,
        },
    )
    rent = (fields["rent_min_usd"], fields["rent_max_usd"])
    if created or rent != (existing.rent_min_usd, existing.rent_max_usd):
        OfficeRentHistory.objects.create(
            building=building, rent_min_usd=rent[0], rent_max_usd=rent[1], observed_at=now
        )
    return created
```

In `add_arguments` add:

```python
        parser.add_argument("--dry-run", action="store_true", help="print parsed fields, write nothing")
```

In `handle`, replace

```python
            self.stdout.write(self._line(fields, notes))
```

with

```python
            self.stdout.write(self._line(fields, notes))
            if not options["dry_run"]:
                counts["inserted" if save_building(fields) else "updated"] += 1
```

In `ScrapeOfficesCommandTests.test_capped_districts_fetch_only_the_sample`, add `"--dry-run",` after `"--per-district", "1",`. It's a `SimpleTestCase` and must stay off the DB.

- [ ] **Step 4: Run to verify pass**

Run: `venv\Scripts\python.exe manage.py test listings.tests.test_offices`
Expected: 20 tests, OK.

- [ ] **Step 5: Full suite**

Run: `venv\Scripts\python.exe manage.py test listings`
Expected: 322 tests, OK.

---

## Task 7: Save the batch, confirm by query

- [ ] **Step 1: Live save run**

```powershell
$env:PYTHONIOENCODING='utf-8'; venv\Scripts\python.exe manage.py scrape_offices --districts quan-1,quan-3,quan-2,quan-tan-binh,quan-binh-thanh,tp-thu-duc --per-district 7
```

Expected: the same 42 buildings as Checkpoint A, summary `'inserted': N` where N equals `'parsed'`.

- [ ] **Step 2: Query** (via `manage.py shell -c`, following the verify skill's conventions):

```python
from django.db.models import Count
from listings.models import OfficeBuilding, OfficeRentHistory
print("buildings:", OfficeBuilding.objects.count(), "history:", OfficeRentHistory.objects.count())
print(list(OfficeBuilding.objects.values("district").annotate(n=Count("id")).order_by("district")))
for b in OfficeBuilding.objects.order_by("district", "name")[:42]:
    print(b.source_id, b.district, b.grade, b.rent_min_usd, b.rent_max_usd, b.service_fee_usd, b.typical_floor_sqm, b.ward, b.name, sep=" | ")
```

Expected: buildings == parsed count, history == buildings (one first-sight row each), 7 per district unless a page failed.

---

## Task 8: Validation pass and report (stop afterwards)

- [ ] **Step 1: Run the check script** (scratchpad, not committed; promote to a command only if it's needed again on a full crawl):

```python
import re
from collections import Counter
from listings.models import OfficeBuilding

rows = list(OfficeBuilding.objects.all())
fields = ["district", "ward", "address_raw", "grade", "rent_min_usd", "rent_max_usd",
          "service_fee_usd", "typical_floor_sqm", "specs_raw", "source_modified_at"]
print("== nulls (of", len(rows), ")")
for f in fields:
    print(f"  {f}: {sum(getattr(r, f) is None for r in rows)}")

print("== units / ranges")
for r in rows:
    problems = []
    if r.rent_min_usd is not None and not (5 <= r.rent_min_usd <= r.rent_max_usd <= 100):
        problems.append(f"rent {r.rent_min_usd}-{r.rent_max_usd}")
    # 0 is valid: "Đã bao gồm trong giá thuê" (fee bundled into rent), decided 2026-10-03
    if r.service_fee_usd is not None and r.service_fee_usd != 0 and not (0.5 <= r.service_fee_usd <= 15):
        problems.append(f"fee {r.service_fee_usd}")
    if r.typical_floor_sqm is not None and not (50 <= r.typical_floor_sqm <= 5000):
        problems.append(f"floor {r.typical_floor_sqm}")
    raw_rent = (r.specs_raw or {}).get("Giá thuê", "")
    if raw_rent and "usd" not in raw_rent.lower() and "liên hệ" not in raw_rent.lower():
        problems.append(f"raw rent {raw_rent!r}")
    if problems:
        print(" ", r.source_id, r.name, problems)

print("== duplicates")
print("  same name+address:", [k for k, n in Counter((r.name.lower(), r.address_raw) for r in rows).items() if n > 1])
print("  same address, different ids:", [k for k, n in Counter(r.address_raw for r in rows if r.address_raw).items() if n > 1])

print("== rent vs sale")
sale_words = re.compile(r"\b(bán|giá bán|sale)\b", re.I)
print("  sale wording in any spec label/value:", [r.source_id for r in rows if sale_words.search(" ".join(f"{k} {v}" for k, v in (r.specs_raw or {}).items()))])
print("  url outside the rent tree:", [r.url for r in rows if "/cho-thue-van-phong-tphcm/" not in r.url])

print("== districts (URL slug vs address parenthetical)")
def base(name):
    return re.sub(r"^(Quận|Thành phố|TP\.|Huyện)\s+", "", name or "").strip().lower()
for r in rows:
    paren = re.search(r"\(([^)]+)\)", r.address_raw or "")
    if not paren or base(paren.group(1)) != base(r.district):
        print(" ", r.source_id, r.district, "| address:", r.address_raw)
```

- [ ] **Step 2: Write the summary for Peter.** For each of the five checks (nulls, units, duplicates, rent vs sale, districts), give the count, the actual offending rows, and whether each is a parser defect, a source-data quirk or expected. Run `systematic-debugging` + `diagnosing-bugs` on anything that looks like a parser defect before proposing a fix.

- [ ] **Step 3: STOP.** Report and wait. Peter picks ML estimates or dashboard display before Phase 3 is planned.

---

## Self-review

- Spec coverage: sitemap discovery (Task 1, 4), field list (Task 3, table above), schema B (Task 5), 42-building cap (Checkpoint A, Task 7), DB save confirmed by query (Task 7), validation of nulls, units, duplicates, rent vs sale and districts (Task 8), stop before ML/dashboard (Task 8 Step 3). Delisting, run table and CLAUDE.md are explicitly deferred, with triggers.
- Placeholders: none. Every code step carries its code.
- Names: `parse_sitemap`, `district_slug`, `district_name`, `parse_usd_per_sqm`, `parse_building`, `save_building`, `HCM_SITEMAP`, `SITEMAP_INDEX` are used consistently across Tasks 1–8. The field keys returned by `parse_building` plus `source_modified_at` match the `OfficeBuilding` columns one to one.
