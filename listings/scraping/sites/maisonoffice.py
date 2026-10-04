import re
import unicodedata
import xml.etree.ElementTree as ET
from decimal import Decimal
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..parsers import RequiredFieldMissing

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
# How the source says "no figure published": a plain null, never a note.
# "chưa bao gồm" is "not included": the charge exists, its amount isn't given.
UNKNOWN = re.compile(r"liên hệ|cập nhật|thỏa thuận|chưa bao gồm")
# "boa" is a live typo of "bao".
BUNDLED = re.compile(r"(?:bao|boa) gồm")


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


def parse_usd_per_sqm(text):
    """(low, high) in USD/m2/month, or (None, None) for anything else.

    "Liên hệ" and an unrecognised unit both come back empty; parse_building
    tells them apart, because only the second one is worth a note.
    """
    lower = (text or "").lower()
    if "usd" not in lower or not re.search(r"m2|m²|sqm", lower):
        return None, None
    # ponytail: "," and "." both read as decimal points. Per-m2 USD figures sit
    # far below 1,000, so neither is ever a thousands separator here -- unlike
    # parse_vnd, where "3.8" would become 38.
    numbers = re.findall(r"\d+(?:[.,]\d+)?", lower.split("usd")[0])
    if not numbers:
        return None, None
    values = [Decimal(n.replace(",", ".")) for n in numbers]
    return values[0], values[-1]


def parse_sqm(text):
    """First area figure in the text, in m2.

    The site mixes number conventions ("1,163", "1.597", "167.75"), so a
    separator followed by exactly three digits is a thousands separator and
    anything else is a decimal point. Tower and block labels go first, so
    "Tháp 1: 411 m2" reads 411, not 1.
    """
    text = re.sub(r"(?:tháp|tower|khối|block)\s*\w+\s*:", " ", text, flags=re.I)
    match = re.search(r"\d+(?:[.,]\d+)*", text)
    if not match:
        return None
    number = re.sub(r"[.,](?=\d{3}(?!\d))", "", match.group())
    return Decimal(number.replace(",", "."))


def parse_building(html, url):
    """(fields, notes) for one building page.

    notes names each nullable field that had text but did not parse, so a
    unit or markup change shows up in the run summary instead of passing as
    an ordinary null.
    """
    # Some pages arrive decomposed (NFD), which no literal like "Phường" or
    # "Giá thuê" matches until the text is recomposed.
    soup = BeautifulSoup(unicodedata.normalize("NFC", html), "html.parser")
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
    ward = re.search(r"(?:Phường|\bP\.|Xã|Thị trấn)\s*[^,()]+", address or "", re.I)

    slug = district_slug(url)
    district = district_name(slug) if slug else None
    if slug and district is None:
        notes.append("unmapped_district")

    rent_text = specs.get("Giá thuê")
    rent_min, rent_max = parse_usd_per_sqm(rent_text)
    if rent_text and rent_min is None and not UNKNOWN.search(rent_text.lower()):
        notes.append("rent_not_usd_per_sqm")

    fee_text = specs.get("Phí dịch vụ")
    fee, _ = parse_usd_per_sqm(fee_text)
    fee_lower = (fee_text or "").lower()
    # Bundled into the rent: no separate charge. Any "chưa" (not yet) rules it
    # out, so "Chưa bao gồm" lands as a null, never a false 0.
    if BUNDLED.search(fee_lower) and "chưa" not in fee_lower:
        fee = Decimal("0")
    elif fee_text and fee is None and not UNKNOWN.search(fee_lower):
        notes.append("service_fee_not_usd_per_sqm")

    grade_text = specs.get("Hạng tòa nhà")
    grade = re.search(r"Hạng\s*([ABC])\b", grade_text or "")
    if grade_text and grade is None and not UNKNOWN.search(grade_text.lower()):
        notes.append("grade_unrecognized")

    floor_text = specs.get("Tầng điển hình")
    floor = parse_sqm(floor_text) if floor_text and re.search(r"m2|m²", floor_text) else None
    if floor_text and floor is None and not UNKNOWN.search(floor_text.lower()):
        notes.append("floor_area_unrecognized")

    return {
        "source_id": source_id,
        "url": url,
        "name": name,
        "district": district,
        "ward": re.sub(r"^(?:P\.|Phường)\s*", "Phường ", ward.group().strip(), flags=re.I)
        if ward
        else None,
        "address_raw": address,
        "grade": grade.group(1) if grade else None,
        "rent_min_usd": rent_min,
        "rent_max_usd": rent_max,
        "service_fee_usd": fee,
        "typical_floor_sqm": floor,
        "specs_raw": specs or None,
    }, notes
