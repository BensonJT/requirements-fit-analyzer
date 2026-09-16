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
    """(rows, reported_total, clamped, true_total) for one Workday board, from a single unfiltered page-0 call.

    `true_total` is the timeType sum -- the real posting count behind a clamped `total`, and the number a
    partition has to reproduce."""
    tenant, wd, site = row["identifier_1"], row["identifier_2"], row["identifier_3"]
    url = f"{A._workday_host(tenant, wd)}/wday/cxs/{tenant}/{site}/jobs"
    with A.client() as c:
        d = A._request(c, "POST", url, json={"appliedFacets": {}, "limit": 20, "offset": 0}).json()
    return (list(_flatten(d.get("facets"))), (d.get("total") or 0), A._workday_total_is_clamped(d),
            A.workday_true_total(d))


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


# A partition trades one pull for many, so it is only worth it while the pull count stays small; past this
# the board is better served by `truncate` (pull the window, never close-pass) until someone finds a facet
# with fewer values.
PARTITION_MAX_PULLS = 40
# Location facets are excluded as partition keys for two independent reasons: a posting can carry several
# locations (so the counts over-count and the sum test is meaningless), and the key you filter a nested
# location group on is not the group's own parameter name -- the trap that made `locationMainGroup` answer
# HTTP 400. Partitioning is restricted to FLAT facets, whose parameter IS the filter key.
PARTITION_EXCLUDED_PARAMS = {"locationMainGroup", "locationCountry", "locationRegionStateProvince",
                             "locationHierarchy1", "locationHierarchy2", "locationHierarchy3", "Location",
                             "Location_Country", "LocationCountry", "locations"}


def resolve_partition(rows, true_total, ceiling=A.WORKDAY_CEILING):
    """(facet_parameter, [value_ids], note) enumerating a clamped board one facet value at a time, or None.

    A facet qualifies when it is flat (its parameter is the filter key), every value sits under the ceiling
    (or that pull is clamped in turn and we have gained nothing), there are few enough values to be worth the
    pulls, and its counts sum to the board's true total -- the last being the only evidence that the facet is
    single-valued AND covers every posting.

    Among the qualifying facets the choice is the one with the SMALLEST largest value, not the fewest pulls:
    headroom under the ceiling is what keeps the partition working as the board grows. Leidos offers
    `Is_Evergreen` (2 pulls, largest 1,863 -- 137 reqs from clamping again) and `jobFamilyGroup` (27 pulls,
    largest 917). The 27 pulls are the better trade. Fewest pulls breaks a tie.

    The sum test is necessary but not sufficient, which is why `workday_jobs` re-checks the union size
    against the same true total after the pull instead of trusting this.
    """
    if not true_total:
        return None
    by_param = {}
    for param, group, vid, desc, count in rows:
        if not param or not vid or param in PARTITION_EXCLUDED_PARAMS:
            continue
        if (group or "").strip():                       # nested: the group is not the filter key
            continue
        by_param.setdefault(param, []).append((vid, desc, count or 0))
    best = None
    for param, values in by_param.items():
        if len(values) > PARTITION_MAX_PULLS or len(values) < 2:
            continue
        if any(c >= ceiling for _v, _d, c in values):
            continue
        total = sum(c for _v, _d, c in values)
        if total != true_total:                          # multi-valued (over) or incomplete (under)
            continue
        biggest = max(c for _v, _d, c in values)
        if best is None or (biggest, len(values)) < (best[0], len(best[2])):
            best = (biggest, param, values)
    if best is None:
        return None
    _biggest, param, values = best
    note = (f"partition on {param}: {len(values)} values summing to {true_total} "
            f"(largest {max(c for _v, _d, c in values)}, ceiling {ceiling})")
    return param, [v for v, _d, _c in values], note


def verify_partition(url, key, value_id, expected_count):
    """True when applying one partition value really filters to that value's own count.

    Same three tenant behaviours as `verify_scope` -- applied, rejected with 400, or accepted and silently
    ignored -- and the facet-count match is what separates the first from the third: an ignored filter comes
    back with the whole board's total, not this value's.
    """
    try:
        with A.client() as c:
            d = A._request(c, "POST", url, json={"appliedFacets": {key: [value_id]}, "limit": 1,
                                                 "offset": 0}).json()
    except Exception:
        return False, None
    total = d.get("total")
    return bool(total and abs(total - expected_count) <= max(1, expected_count * 0.02)), total


def strategy_for(clamped, us, partition=None):
    """US scope is applied wherever the board exposes one -- not only on clamped boards. Global reqs are
    hard-rejected by the rules anyway, so pulling them wastes a sweep and fills a clamped window with
    postings we delete (Accenture: 38,911 of 45,219 are India)."""
    if us:
        return "country"
    if clamped and partition:
        return "partition"
    return "truncate" if clamped else "plain"


def record(con, employer, platform, rows, reported_total, clamped, strategy, us, partition=None):
    """Stores the facet tree and the resolved plan. `country` stores the US value; `partition` stores every
    value of the chosen facet, which is what the plan executor turns into one pull each."""
    now = _now()
    con.execute("DELETE FROM board_facets WHERE employer = ? AND platform = ?", [employer, platform])
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO board_facets VALUES (?,?,?,?,?,?,?,?)",
            [[employer, platform, p, g or '', str(vid), d or '', cnt, now] for p, g, vid, d, cnt in rows if vid])
    if strategy == "partition" and partition:
        key, ids, note = partition
    elif us:
        key, ids, note = us[0], us[1], f"US = {us[2]!r}"
    else:
        key, ids, note = None, None, "no country facet exposed"
    con.execute("INSERT OR REPLACE INTO board_scope VALUES (?,?,?,?,?,?,?,?,?)",
                [employer, platform, strategy, key, json.dumps(ids) if ids else None,
                 reported_total, clamped, note, now])


def _workday_url(row):
    tenant, wd, site = row["identifier_1"], row["identifier_2"], row["identifier_3"]
    return f"{A._workday_host(tenant, wd)}/wday/cxs/{tenant}/{site}/jobs"


def discover_and_record(con, rows, log=print) -> list:
    """Discover, resolve, live-verify and store a scope for every Workday board in `rows`.

    The order matters: a US country facet is preferred over a partition because one pull beats many, and a
    partition is only reached when the board exposes no country filter at all (Booz Allen, Leidos, Sentara).
    Nothing is stored on the strength of the facet tree alone -- both `country` and `partition` are probed
    live first, because tenants reject a filter with HTTP 400 or accept it and ignore it.
    """
    out = []
    for row in rows:
        employer, platform = row["employer"], row["platform"]
        if platform != "workday":
            continue
        try:
            facet_rows, reported, clamped, true_total = discover_workday(row)
        except Exception as exc:  # noqa: BLE001 — one unreachable board must not stop discovery
            log(f"  {employer}: discovery FAILED — {type(exc).__name__}: {str(exc)[:90]}")
            continue
        url = _workday_url(row)
        us = resolve_us(facet_rows)
        if us:
            ok, scoped_total = verify_scope(url, us[0], us[1], reported)
            if not ok:
                log(f"  {employer}: country facet {us[0]!r} resolved but did not filter "
                    f"(total {scoped_total} vs {reported}); dropping it")
                us = None
        partition = None
        if us is None and clamped:
            partition = resolve_partition(facet_rows, true_total)
            if partition:
                key, ids, _note = partition
                biggest = max(((vid, cnt) for pm, g, vid, _d, cnt in facet_rows
                               if pm == key and not (g or "").strip() and vid in set(ids)),
                              key=lambda t: t[1] or 0)
                ok, got = verify_partition(url, key, biggest[0], biggest[1] or 0)
                if not ok:
                    log(f"  {employer}: partition facet {key!r} did not filter "
                        f"(value {biggest[0]} returned {got}, expected {biggest[1]}); dropping it")
                    partition = None
        strategy = strategy_for(clamped, us, partition)
        record(con, employer, platform, facet_rows, reported, clamped, strategy, us, partition)
        note = (partition[2] if strategy == "partition" else
                (f"US = {us[2]!r}" if us else "no country facet exposed"))
        log(f"  {employer}: total {reported}, true {true_total or '?'}, "
            f"clamped {clamped} -> {strategy} ({note})")
        out.append((employer, strategy, reported, true_total, note))
    return out
