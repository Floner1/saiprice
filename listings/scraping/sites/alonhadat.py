import re
from datetime import date
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

from ..currency import parse_vnd
from ..parsers import ParsedListing, RequiredFieldMissing, _parse_area, _parse_int

BASE_URL = "https://alonhadat.com.vn"

# Confirmed from the live homepage nav 2026-07-10; robots.txt permits /can-ban-*
# (only /publish/*, /nha-dat/can-mua/*, /nha-dat/can-thue/* are disallowed).
# ponytail: apartment-sale only until a run is proven stable, then add the
# other tracked-scope roots one at a time -- same rollout rule the batdongsan
# crawler used. Full scope: /can-ban-nha, /can-ban-biet-thu-nha-lien-ke,
# /can-ban-dat-tho-cu-dat-o (+ dat-nen, dat-nong-lam), and cho-thue-* mirrors.
CATEGORY_ROOTS = {
    "/can-ban-can-ho-chung-cu/ho-chi-minh": ("apartment", "sale"),
}

# Breadcrumb category slug -> (property_type, listing_intent), full sale
# taxonomy read live from the homepage nav 2026-07-10; every slug has an
# exact /cho-thue-* rent mirror (also confirmed live), generated below.
# Slugs outside the tracked scope (van-phong, kho-xuong, phong-tro, ...)
# are deliberately unmapped: parse_ldp_extras returns property_type None
# and the caller keeps the value it already has.
BREADCRUMB_CATEGORIES = {
    "/can-ban-nha": ("house", "sale"),
    "/can-ban-nha-trong-hem": ("house", "sale"),
    "/can-ban-nha-mat-tien": ("house", "sale"),
    "/can-ban-biet-thu-nha-lien-ke": ("villa", "sale"),
    "/can-ban-can-ho-chung-cu": ("apartment", "sale"),
    "/can-ban-dat-tho-cu-dat-o": ("land", "sale"),
    "/can-ban-dat-nen-lien-ke-dat-du-an": ("land", "sale"),
    "/can-ban-dat-nong-lam-nghiep": ("land", "sale"),
}
BREADCRUMB_CATEGORIES.update(
    {
        slug.replace("/can-ban-", "/cho-thue-"): (property_type, "rent")
        for slug, (property_type, _) in list(BREADCRUMB_CATEGORIES.items())
    }
)


def parse_ldp_extras(html):
    soup = BeautifulSoup(html, "html.parser")
    category = None
    for crumb in soup.select("[itemtype*='Breadcrumb'] a[href]"):
        category = BREADCRUMB_CATEGORIES.get(crumb["href"].rstrip("/"))
        if category:
            break
    # images is [] (not None) when the LDP parsed but the gallery is empty:
    # null means "LDP never successfully visited" and triggers a re-visit
    # on the next crawl pass, [] means "visited, no photos".
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
    }


def page_url(root, page):
    return f"{BASE_URL}{root}" if page == 1 else f"{BASE_URL}{root}/trang-{page}"


def parse_srp(html, property_type, listing_intent):
    soup = BeautifulSoup(html, "html.parser")
    listings, skips = [], []
    for card in soup.select("article.property-item"):
        try:
            listings.append(_parse_card(card, property_type, listing_intent))
        except RequiredFieldMissing as exc:
            link = card.select_one("a[itemprop='url']")
            ref = (link.get("href") if link else None) or card.get_text(strip=True)[:80]
            skips.append((ref, exc.field))
    return listings, skips


def _parse_price(card):
    tag = card.select_one("[itemprop='price']")
    if tag is None:
        return None
    if tag.has_attr("content"):
        try:
            # or None: content="0" is how a no-price card would render, not a price
            return Decimal(tag["content"]) or None
        except InvalidOperation:
            pass
    return parse_vnd(tag.get_text(strip=True))


def _parse_posted_date(card):
    tag = card.select_one("[itemprop='datePosted']")
    if not tag or not tag.has_attr("datetime"):
        return None
    try:
        return date.fromisoformat(tag["datetime"])
    except ValueError:
        return None


def _parse_card(card, property_type, listing_intent):
    link = card.select_one("a[itemprop='url']")
    href = link.get("href") if link else None
    id_match = re.search(r"-(\d+)\.html$", href or "")
    source_id = id_match.group(1) if id_match else None
    url = f"{BASE_URL}{href}" if href and href.startswith("/") else href
    title_tag = card.select_one("[itemprop='name']")
    title = title_tag.get_text(strip=True) if title_tag else None

    for field_name, value in (("source_id", source_id), ("url", url), ("title", title)):
        if not value:
            raise RequiredFieldMissing(field_name)

    price = _parse_price(card)
    area_tag = card.select_one("[itemprop='floorSize'] [itemprop='value']")
    area_sqm = _parse_area(area_tag.get_text(strip=True)) if area_tag else None
    price_per_sqm = (price / area_sqm).quantize(Decimal("1")) if price and area_sqm else None

    bedroom_tag = card.select_one("[itemprop='numberOfBedrooms'] [itemprop='value']")

    vipstar = card.select_one("div[class*='vipstar']")
    vip_type = None
    if vipstar:
        vip_type = next((c for c in vipstar.get("class", []) if c.startswith("vip-")), None)

    # 2025 admin reform: the new-format address (microdata) has no district
    # level, so district comes from the "(cũ)" old-format line via the same
    # second-to-last comma-segment rule as batdongsan; ward is the current
    # official one from addressLocality. Mixed epochs, best available each.
    new_addr_parts = [
        s.get_text(strip=True) for s in card.select(".new-address [itemprop]")
    ]
    address_raw = ", ".join(new_addr_parts) or None
    ward_tag = card.select_one(".new-address [itemprop='addressLocality']")
    ward = ward_tag.get_text(strip=True) if ward_tag else None
    old_addr = card.select_one(".old-address span")
    district = None
    if old_addr:
        parts = [p.strip() for p in old_addr.get_text(strip=True).split(",")]
        district = parts[-2] if len(parts) >= 2 else None

    specs = {}
    for key, selector in (
        ("street_width", ".street-width"),
        ("floors", ".floors"),
        ("size", ".size span"),
    ):
        tag = card.select_one(selector)
        if tag:
            specs[key] = tag.get_text(strip=True)

    # property_type here is the crawled category, provisional only: vip
    # cards can be cross-category injections (seen live 2026-07-10). The
    # crawl confirms it from the listing's own LDP breadcrumb on first
    # insert via parse_ldp_extras, and never overwrites it from the SRP
    # category on later passes.
    fields = {
        "url": url,
        "title": title,
        "property_type": property_type,
        "listing_intent": listing_intent,
        "vip_type": vip_type,
        "price": price,
        "price_per_sqm": price_per_sqm,
        "area_sqm": area_sqm,
        "bedrooms": _parse_int(bedroom_tag.get_text(strip=True)) if bedroom_tag else None,
        "address_raw": address_raw,
        "district": district,
        "ward": ward,
        "specs_raw": specs or None,
        "posted_date": _parse_posted_date(card),
    }

    return ParsedListing(
        source_site="alonhadat",
        source_id=source_id,
        agent_source_id=link.get("data-memberid") or None,
        agent_name=None,
        expired=False,
        fields=fields,
    )
