import re
from decimal import Decimal

UNITS = {"tỷ": Decimal("1e9"), "triệu": Decimal("1e6"), "tr": Decimal("1e6")}


def _price_match(text):
    if not text or "thỏa thuận" in text.lower():
        return None
    # homedy renders multi-unit project listings as a range ("2 - 2,1 Tỷ");
    # a range is not one price, so it parses to null like "Thỏa thuận"
    if re.search(r"\d\s*-\s*\d", text):
        return None
    return re.search(r"([\d,.]+)\s*(tỷ|triệu|tr)", text, re.IGNORECASE)


def parse_vnd(text: str) -> Decimal | None:
    match = _price_match(text)
    if not match:
        return None
    number = Decimal(match.group(1).replace(".", "").replace(",", "."))
    return (number * UNITS[match.group(2).lower()]).quantize(Decimal("1"))


def parse_vnd_unit(text: str) -> str | None:
    # Shares _price_match with parse_vnd so price and price_unit can't disagree
    match = _price_match(text)
    if not match:
        return None
    unit = match.group(2).lower()
    return "triệu" if unit == "tr" else unit
