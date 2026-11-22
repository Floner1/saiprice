import re
import unicodedata
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from django.utils import timezone

from ..currency import parse_vnd, parse_vnd_unit
from ..parsers import ParsedListing, RequiredFieldMissing, _parse_area, mask_phones

BASE_URL = "https://homedy.com"

# Search pages only. A listing page carries the agent's phone in plain HTML,
# so this module never reads one: no agent, no phone, no photos.
CATEGORY_ROOTS = {
    "/ban-can-ho-tp-ho-chi-minh": ("apartment", "sale"),
}

HCMC = ZoneInfo("Asia/Ho_Chi_Minh")

# The labels alonhadat stores, so the two sources group into the same
# districts on /districts/. Huyện Củ Chi has no alonhadat rows yet.
HCMC_DISTRICTS = (
    "Quận 1", "Quận 3", "Quận 4", "Quận 5", "Quận 6", "Quận 7", "Quận 8",
    "Quận 10", "Quận 11", "Quận 12", "Quận Bình Tân", "Quận Bình Thạnh",
    "Quận Gò Vấp", "Quận Phú Nhuận", "Quận Tân Bình", "Quận Tân Phú",
    "Thành phố Thủ Đức", "Huyện Bình Chánh", "Huyện Cần Giờ", "Huyện Củ Chi",
    "Huyện Hóc Môn", "Huyện Nhà Bè",
)

_PREFIX = re.compile(r"^(?:quận|huyện|thành phố|tp\.?)\s*", re.IGNORECASE)


def _bare(label):
    return _PREFIX.sub("", label).strip().lower()


# Keyed by the name without its quận/huyện/thành phố prefix, because homedy
# mixes them up ("Quận Bình Chánh" for a huyện). Quận 2 and Quận 9 were
# merged into Thành phố Thủ Đức in 2021 and alonhadat files them there.
_BY_BARE = {_bare(label): label for label in HCMC_DISTRICTS}
_BY_BARE.update({"2": "Thành phố Thủ Đức", "9": "Thành phố Thủ Đức"})


def normalize_district(text):
    """homedy's "district,  city" text to alonhadat's label, or None."""
    if not text:
        return None
    first = unicodedata.normalize("NFC", text).split(",")[0].strip().rstrip(".")
    return _BY_BARE.get(_bare(first))


_RELATIVE = re.compile(r"(\d+)\s*(phút|giờ|ngày|tuần)\s*trước", re.IGNORECASE)
_DAYS_PER = {"phút": 0, "giờ": 0, "ngày": 1, "tuần": 7}


def parse_relative_date(text, today):
    """The card's "2 ngày trước" style age as a date, or None if unrecognised."""
    if not text:
        return None
    text = unicodedata.normalize("NFC", text).strip().lower()
    if text == "hôm nay":
        return today
    if text == "hôm qua":
        return today - timedelta(days=1)
    match = _RELATIVE.fullmatch(text)
    if not match:
        return None
    return today - timedelta(days=int(match.group(1)) * _DAYS_PER[match.group(2)])


def page_url(root, page):
    return f"{BASE_URL}{root}" if page == 1 else f"{BASE_URL}{root}/p{page}"


def parse_srp(html, property_type, listing_intent, today=None):
    today = today or timezone.localdate(timezone=HCMC)
    soup = BeautifulSoup(html, "html.parser")
    listings, skips = [], []
    for card in soup.select("div.product-item"):
        try:
            listings.append(_parse_card(card, property_type, listing_intent, today))
        except RequiredFieldMissing as exc:
            link = card.select_one("h3 a[href]")
            ref = (link.get("href") if link else None) or card.get_text(strip=True)[:80]
            skips.append((mask_phones(ref), exc.field))
    return listings, skips


def _text(card, selector):
    tag = card.select_one(selector)
    return tag.get_text(" ", strip=True) if tag else None


_ID_IN_HREF = re.compile(r"-es(\d+)$")


def _parse_card(card, property_type, listing_intent, today):
    link = card.select_one("h3 a[href]")
    href = link["href"] if link else ""
    id_match = _ID_IN_HREF.search(href)
    if not id_match:
        raise RequiredFieldMissing("source_id")
    # A card outside the apartment root is a cross-category insert; the
    # crawled property_type would be a guess, so the card is skipped.
    if not href.startswith("/ban-can-ho-"):
        raise RequiredFieldMissing("property_type")
    # Sellers put their number in the title, so it lands in the slug too. The
    # -es<id> ending is kept as is; only the slug before it is cleaned.
    slug = re.sub(r"-{2,}", "-", mask_phones(href[: id_match.start()])).rstrip("-")
    url = f"{BASE_URL}{slug}-es{id_match.group(1)}"
    title = mask_phones(link.get_text(" ", strip=True))
    if not title:
        raise RequiredFieldMissing("title")

    price_text = _text(card, "span.price")
    price = parse_vnd(price_text)
    area_text = _text(card, "span.acreage")
    # A range ("55 - 120 m2") is a project's spread of units, not one area.
    is_range = area_text and re.search(r"\d\s*-\s*\d", area_text)
    area_sqm = None if is_range else _parse_area(area_text)
    address = card.select_one("li.address")

    fields = {
        "url": url,
        "title": title,
        "property_type": property_type,
        "listing_intent": listing_intent,
        "price": price,
        "price_unit": parse_vnd_unit(price_text) if price else None,
        "price_per_sqm": (price / area_sqm).quantize(Decimal("1")) if price and area_sqm else None,
        "area_sqm": area_sqm,
        "address_raw": mask_phones(address.get("title")) if address else None,
        "district": normalize_district(address.get_text(" ", strip=True) if address else None),
        "posted_date": parse_relative_date(_text(card, "div.time"), today),
    }
    return ParsedListing(
        source_site="homedy",
        source_id=id_match.group(1),
        agent_source_id=None,
        expired=False,
        fields=fields,
    )
