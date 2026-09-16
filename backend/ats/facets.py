"""Discover what each board's search API can filter by, and resolve a US scope from it.

Hardcoding a facet id is the pattern that bit us: `bc33aa3152...` is Workday's global id for the United
States and happens to be right on Accenture, but tenants publish their own ids and their own facet
parameters. Booz Allen has no country facet at all -- only a flat city list -- and answers
`locationCountry` with HTTP 400. So we ask each board what it has, store it, and resolve from that.
"""
import json
from datetime import datetime, timezone

from . import adapters as A

# Exact descriptors only. Substring matching on "United" would also catch United Kingdom and United Arab
# Emirates, both live in Accenture's country list.
US_NAMES = {"united states of america", "united states", "usa", "u.s.a.", "us", "u.s.", "united states (usa)"}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _flatten(facets):
    """(facet_parameter, group_descriptor, value_id, descriptor, count) for a CXS facet tree."""
    for f in facets or []:
        param = f.get("facetParameter")
        for v in f.get("values") or []:
            kids = v.get("values") or []
            if kids:                                     # nested: v is the group (Country / City / Locations)
                for k in kids:
                    yield param, v.get("descriptor"), k.get("id"), k.get("descriptor"), k.get("count")
            else:
                yield param, None, v.get("id"), v.get("descriptor"), v.get("count")


def discover_workday(row):
    """(rows, reported_total, clamped) for one Workday board, from a single unfiltered page-0 call."""
    tenant, wd, site = row["identifier_1"], row["identifier_2"], row["identifier_3"]
    url = f"{A._workday_host(tenant, wd)}/wday/cxs/{tenant}/{site}/jobs"
    with A.client() as c:
        d = A._request(c, "POST", url, json={"appliedFacets": {}, "limit": 20, "offset": 0}).json()
    return list(_flatten(d.get("facets"))), (d.get("total") or 0), A._workday_total_is_clamped(d)


# Workday publishes countries NESTED under `locationMainGroup` -> "Country", but the key you filter on is
# `locationCountry`. Filtering on the tree's own parameter name answers HTTP 400 (Compassion International,
# 2026-09-16). Tenants that expose country as a top-level parameter (Location_Country, LocationCountry, a
# custom CF_-_REC_... field) use that name as the filter key directly.
NESTED_LOCATION_PARAM = "locationMainGroup"
NESTED_COUNTRY_KEY = "locationCountry"


def resolve_us(rows):
    """(applied_facet_key, [value_id], descriptor) for the US, or None when the board exposes no country."""
    for param, group, vid, desc, _count in rows:
        if not desc or not vid or desc.strip().lower() not in US_NAMES:
            continue
        if (group or "").strip().lower() == "country":
            key = NESTED_COUNTRY_KEY if param == NESTED_LOCATION_PARAM else param
            return key, [vid], desc
        if "country" in (param or "").lower():
            return param, [vid], desc
    return None


def verify_scope(url, key, ids, unfiltered_total):
    """A resolved facet is only trusted when applying it actually works AND actually filters.

    Three tenant behaviours were observed: applied correctly (Accenture), rejected with HTTP 400 (Booz
    Allen, Sentara), and accepted-then-silently-ignored (GE Vernova returned an unchanged total and
    French locations). Only the first is usable, and nothing but a live probe tells them apart.
    """
    try:
        with A.client() as c:
            d = A._request(c, "POST", url, json={"appliedFacets": {key: ids}, "limit": 20, "offset": 0}).json()
    except Exception:
        return False, None
    total = d.get("total")
    if not total or (unfiltered_total and total >= unfiltered_total):
        return False, total
    return True, total


def strategy_for(clamped, us):
    """US scope is applied wherever the board exposes one -- not only on clamped boards. Global reqs are
    hard-rejected by the rules anyway, so pulling them wastes a sweep and fills a clamped window with
    postings we delete (Accenture: 38,911 of 45,219 are India)."""
    if us:
        return "country"
    return "truncate" if clamped else "plain"


def record(con, employer, platform, rows, reported_total, clamped, strategy, us):
    now = _now()
    con.execute("DELETE FROM board_facets WHERE employer = ? AND platform = ?", [employer, platform])
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO board_facets VALUES (?,?,?,?,?,?,?,?)",
            [[employer, platform, p, g or '', str(vid), d or '', cnt, now] for p, g, vid, d, cnt in rows if vid])
    con.execute("INSERT OR REPLACE INTO board_scope VALUES (?,?,?,?,?,?,?,?,?)",
                [employer, platform, strategy, us[0] if us else None,
                 json.dumps(us[1]) if us else None, reported_total, clamped,
                 (f"US = {us[2]!r}" if us else "no country facet exposed"), now])
