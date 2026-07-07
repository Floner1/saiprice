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
