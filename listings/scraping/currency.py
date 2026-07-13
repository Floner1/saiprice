import re
from decimal import Decimal

UNITS = {"tỷ": Decimal("1e9"), "triệu": Decimal("1e6"), "tr": Decimal("1e6")}


def parse_vnd(text: str) -> Decimal | None:
    if not text or "thỏa thuận" in text.lower():
        return None
    # homedy renders multi-unit project listings as a range ("2 - 2,1 Tỷ");
    # a range is not one price, so it parses to null like "Thỏa thuận"
    if re.search(r"\d\s*-\s*\d", text):
        return None
    match = re.search(r"([\d,.]+)\s*(tỷ|triệu|tr)", text, re.IGNORECASE)
    if not match:
        return None
    number = Decimal(match.group(1).replace(".", "").replace(",", "."))
    return (number * UNITS[match.group(2).lower()]).quantize(Decimal("1"))
