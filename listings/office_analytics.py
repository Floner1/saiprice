"""Read-side aggregation for the office rent dashboard at /offices/.

Apart from analytics.py on purpose: OfficeBuilding never mixes with the
residential pipeline, and neither does the code that summarizes it. Pure
functions over the dicts dashboard() reads, so the maths is testable without
a request.
"""

import math
import statistics

from django.db.models import Max, Min

from listings.models import OfficeBuilding

# Below this many priced buildings, quartiles are a few numbers posing as a
# distribution, and a median sort ranks Huyện Bình Chánh (2 buildings) third,
# above Quận 2. Those groups list their raw range instead of a bar.
MIN_SUMMARY_N = 10

# Each chart's rent scale rounds up to this many USD.
AXIS_STEP = 10

FIELDS = (
    "district",
    "grade",
    "rent_min_usd",
    "rent_max_usd",
    "typical_floor_sqm",
    "service_fee_usd",
)


def group_rows(buildings, key):
    """One row per distinct value of `key`, null included.

    A building's rent is the midpoint of its quoted USD/m2/month range. An
    unpriced building counts toward `total` and nothing else, so a group with
    no published rent still gets a row: the gap is information, the same rule
    as the no-run days on /health/.
    """
    groups = {}
    for b in buildings:
        group = groups.setdefault(
            b[key], {"total": 0, "rents": [], "floors": [], "fees": []}
        )
        group["total"] += 1
        if b["rent_min_usd"] is not None and b["rent_max_usd"] is not None:
            group["rents"].append(float(b["rent_min_usd"] + b["rent_max_usd"]) / 2)
        if b["typical_floor_sqm"] is not None:
            group["floors"].append(float(b["typical_floor_sqm"]))
        if b["service_fee_usd"] is not None:
            group["fees"].append(float(b["service_fee_usd"]))

    rows = []
    for value, group in groups.items():
        rents = sorted(group["rents"])
        # Fee 0 means "bundled into the rent", so it is counted, not averaged
        # in: about half of grade C bundles it, which would drag that median to 0.
        charged = [fee for fee in group["fees"] if fee > 0]
        row = {
            "key": value,
            "total": group["total"],
            "priced": len(rents),
            "low": rents[0] if rents else None,
            "high": rents[-1] if rents else None,
            "p25": None,
            "median": None,
            "p75": None,
            "floor_median": statistics.median(group["floors"]) if group["floors"] else None,
            "fee_median": statistics.median(charged) if charged else None,
            "fee_bundled": len(group["fees"]) - len(charged),
        }
        if len(rents) >= MIN_SUMMARY_N:
            row["p25"], row["median"], row["p75"] = statistics.quantiles(rents, n=4)
        rows.append(row)
    return rows


def axis_top(rows):
    """One rent scale for every bar in a chart, rounded up to AXIS_STEP."""
    tops = [row["p75"] for row in rows if row["p75"] is not None]
    return math.ceil(max(tops) / AXIS_STEP) * AXIS_STEP if tops else None


def place(rows, top):
    """Give each summarized row its `bar`: three widths, in % of the track.

    Width is the only inline style CLAUDE.md §11 allows, so the band's offset
    is an empty lead span rather than a `left:` position, and the median is
    where the two filled spans meet. Each edge is rounded once and the widths
    are differences of edges, so they never sum past the p75 edge. Whole
    numbers, so a locale with a decimal comma can't write `24,2%` into a style.
    """

    def edge(value):
        return round(value / top * 100)

    for row in rows:
        if row["median"] is None:
            continue
        p25, median, p75 = edge(row["p25"]), edge(row["median"]), edge(row["p75"])
        row["bar"] = {"lead": p25, "low": median - p25, "high": p75 - median}


def coverage(buildings):
    """How many buildings publish each nullable field, for the coverage bars."""

    def published(field):
        return sum(b[field] is not None for b in buildings)

    return [
        {"label": "Rent", "count": published("rent_min_usd")},
        {"label": "Grade", "count": published("grade")},
        {"label": "Typical floor area", "count": published("typical_floor_sqm")},
        {
            "label": "Service fee",
            "count": published("service_fee_usd"),
            "bundled": sum(b["service_fee_usd"] == 0 for b in buildings),
        },
    ]


def dashboard():
    """Everything /offices/ renders. Two queries: the rows, then the dates."""
    active = OfficeBuilding.objects.filter(is_active=True)
    buildings = list(active.values(*FIELDS))

    grades = sorted(
        group_rows(buildings, "grade"), key=lambda r: (r["key"] is None, r["key"] or "")
    )
    for row in grades:
        row["label"] = f"Grade {row['key']}" if row["key"] else "Not graded"

    districts = group_rows(buildings, "district")
    for row in districts:
        row["label"] = row["key"] or "No district"
    ranked = sorted(
        (r for r in districts if r["median"] is not None),
        key=lambda r: (-r["median"], -r["priced"], r["label"]),
    )
    thin = sorted(
        (r for r in districts if r["median"] is None),
        key=lambda r: (-r["priced"], -r["total"], r["label"]),
    )

    # One scale per chart, not one per page: a shared 0-60 axis (grade A)
    # squeezed every district band into a tenth of the track.
    grade_axis = axis_top(grades)
    if grade_axis:
        place(grades, grade_axis)
    district_axis = axis_top(ranked)
    if district_axis:
        place(ranked, district_axis)

    return {
        "total": len(buildings),
        "priced": sum(row["priced"] for row in grades),
        "grades": grades,
        "districts": ranked,
        "thin_districts": thin,
        "district_table": ranked + thin,
        "min_summary_n": MIN_SUMMARY_N,
        "grade_axis": grade_axis,
        "district_axis": district_axis,
        "coverage": coverage(buildings),
        "dates": active.aggregate(
            crawled=Max("last_seen_at"),
            edited_from=Min("source_modified_at"),
            edited_to=Max("source_modified_at"),
        ),
    }
