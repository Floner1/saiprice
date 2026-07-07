import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from .currency import parse_vnd

# ponytail: only category IDs confirmed from saved HTML are mapped: 324/53
# from the attached LDP samples, 41 from a saved SRP card (batdongsan search
# html.txt, productId 45880042) whose href is /ban-nha-rieng-... (house sale).
# An unmapped one raises RequiredFieldMissing("property_type") and the caller
# skips the listing per CLAUDE.md §7/§8 (required-field parse failure ->
# skip, log). Expand this dict as real SRP crawling surfaces more categories.
CATEGORY_TO_PROPERTY_TYPE = {
    324: "apartment",
    41: "house",
    53: "land",
}

INTENT_TO_LISTING_INTENT = {
    "Sale": "sale",
    "Rent": "rent",
}


class RequiredFieldMissing(Exception):
    def __init__(self, field):
        self.field = field
        super().__init__(f"required field missing: {field}")


@dataclass
class ParsedListing:
    source_site: str
    source_id: str
    agent_source_id: str | None
    agent_name: str | None
    expired: bool
    fields: dict

    @property
    def price(self):
        return self.fields.get("price")

    @property
    def price_per_sqm(self):
        return self.fields.get("price_per_sqm")


def _label_value_map(container, item_selector, title_selector, value_selector):
    result = {}
    if not container:
        return result
    for item in container.select(item_selector):
        title = item.select_one(title_selector)
        value = item.select_one(value_selector)
        if title and value:
            result[title.get_text(strip=True)] = value.get_text(strip=True)
    return result


def _parse_area(text):
    if not text:
        return None
    match = re.search(r"([\d,.]+)", text)
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def _parse_int(text):
    if not text:
        return None
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def _parse_posted_date(text):
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def _extract_page_tracking_data(soup):
    pattern = re.compile(r"window\.pageTrackingData\s*=\s*\{\s*\.\.\.JSON\.parse\('(.+?)'\)")
    for script in soup.find_all("script"):
        text = script.string or ""
        match = pattern.search(text)
        if match:
            raw = match.group(1).encode().decode("unicode_escape")
            return json.loads(raw)
    return None


def _extract_map_coords(soup):
    iframe = soup.select_one(".re__pr-map iframe")
    if not iframe or not iframe.get("data-src"):
        return None, None
    qs = parse_qs(urlparse(iframe["data-src"]).query)
    q = qs.get("q", [None])[0]
    if not q or "," not in q:
        return None, None
    lat_str, _, lng_str = q.partition(",")
    try:
        return Decimal(lat_str), Decimal(lng_str)
    except InvalidOperation:
        return None, None


def _extract_images(soup):
    images = []
    for img in soup.select(".re__media-preview .swiper-slide[data-filter='image'] img"):
        src = img.get("data-src") or img.get("src")
        if src and src not in images:
            images.append(src)
    return images


def _extract_video_url(soup):
    source = soup.select_one(".re__pr-media-slide video source")
    return source.get("src") if source else None


def _extract_agent_name(soup):
    name_el = soup.select_one(".re__agent-infor.re__agent-name .re__contact-name")
    return name_el.get_text(strip=True) if name_el else None


def parse_ldp(html: str, url: str) -> ParsedListing:
    soup = BeautifulSoup(html, "html.parser")

    tracking = _extract_page_tracking_data(soup)
    product = (tracking or {}).get("products", [{}])[0] if tracking else {}

    source_id = str(product.get("productId")) if product.get("productId") else None
    category_id_source = product.get("cateId")
    listing_intent = INTENT_TO_LISTING_INTENT.get(product.get("intent"))
    property_type = (
        CATEGORY_TO_PROPERTY_TYPE.get(category_id_source)
        if category_id_source is not None
        else None
    )

    canonical = soup.select_one("link[rel='canonical']")
    resolved_url = canonical["href"] if canonical and canonical.get("href") else url

    title_el = soup.select_one("h1.re__pr-title")
    title = title_el.get_text(strip=True) if title_el else None

    for field_name, value in (
        ("source_site", "batdongsan"),
        ("source_id", source_id),
        ("url", resolved_url),
        ("title", title),
        ("property_type", property_type),
        ("listing_intent", listing_intent),
        ("category_id_source", category_id_source),
    ):
        if not value and value != 0:
            raise RequiredFieldMissing(field_name)

    specs_container = soup.select_one(".re__pr-specs-content-v2")
    specs = _label_value_map(
        specs_container,
        ".re__pr-specs-content-item",
        ".re__pr-specs-content-item-title",
        ".re__pr-specs-content-item-value",
    )

    config_container = soup.select_one(".re__pr-config")
    config = _label_value_map(config_container, ".js__pr-config-item", ".title", ".value")

    address_el = soup.select_one(".re__address-line-1")
    address_raw = address_el.get_text(strip=True) if address_el else None
    address_parts = [p.strip() for p in address_raw.split(",")] if address_raw else []
    district = address_parts[-2] if len(address_parts) >= 2 else None
    ward = address_parts[-3] if len(address_parts) >= 3 else None

    price = parse_vnd(specs.get("Khoảng giá"))
    area_sqm = _parse_area(specs.get("Diện tích"))
    price_per_sqm = (price / area_sqm).quantize(Decimal("1")) if price and area_sqm else None

    description_el = soup.select_one(".re__pr-description .re__section-body")
    description = (
        description_el.get_text(separator="\n", strip=True) if description_el else None
    )

    project_el = soup.select_one(".re__project-title")
    project_name = project_el.get_text(strip=True) if project_el else None
    project_id_source = str(product.get("projectId")) if product.get("projectId") else None

    map_lat, map_lng = _extract_map_coords(soup)

    fields = {
        "url": resolved_url,
        "title": title,
        "category_id_source": category_id_source,
        "property_type": property_type,
        "project_name": project_name,
        "project_id_source": project_id_source,
        "listing_intent": listing_intent,
        "is_verified": bool(product.get("verified", False)),
        "vip_type": str(product.get("vipType")) if product.get("vipType") is not None else None,
        "price": price,
        "price_unit": None,
        "price_per_sqm": price_per_sqm,
        "area_sqm": area_sqm,
        "bedrooms": _parse_int(specs.get("Số phòng ngủ")),
        "bathrooms": _parse_int(specs.get("Số phòng tắm, vệ sinh")),
        "address_raw": address_raw,
        "district_id_source": product.get("districtId"),
        "ward_id_source": product.get("wardId"),
        "district": district,
        "ward": ward,
        "specs_raw": specs or None,
        "description": description,
        "images": _extract_images(soup) or None,
        "video_url": _extract_video_url(soup),
        "map_lat": map_lat,
        "map_lng": map_lng,
        "phone_number": None,
        "posted_date": _parse_posted_date(config.get("Ngày đăng")),
    }

    agent_source_id = str(product.get("createByUser")) if product.get("createByUser") else None

    return ParsedListing(
        source_site="batdongsan",
        source_id=source_id,
        agent_source_id=agent_source_id,
        agent_name=_extract_agent_name(soup),
        expired=bool(product.get("expired", False)),
        fields=fields,
    )


def parse_srp(html: str, base_url: str = "https://batdongsan.com.vn") -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for link in soup.select("a.js__product-link-for-product-id[href]"):
        # ponytail: data-product-id="0" marks a sponsored ad card
        # (re__card-full-ads), not a real listing -- its href points off-site.
        if link.get("data-product-id") == "0":
            continue
        href = link["href"]
        full_url = href if href.startswith("http") else f"{base_url}{href}"
        if full_url not in urls:
            urls.append(full_url)
    return urls


def parse_srp_total_pages(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    pages = [
        int(a["pid"]) for a in soup.select(".re__pagination-number[pid]") if a.get("pid", "").isdigit()
    ]
    return max(pages) if pages else 1
