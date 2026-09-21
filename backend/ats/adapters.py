"""One `list_jobs(row)` and (where the list call is thin) one `fetch_detail(row, posting)`
per ATS platform. Every adapter returns dicts in the shape of normalize.NORMALIZED_FIELDS.

Design (see DESIGN_ats_registry.md §6a / §14):
- The list call pulls the WHOLE board. There is no job cap: a cap makes the
  close-pass wrong (anything outside the window looks "taken down"). Runtime is
  handled by running boards concurrently in sweep.py, not by truncating them.
  `max_pages` exists only as an explicit safety valve; when it trips, the result is
  marked truncated and the caller skips the close-pass for that board.
- Workday and Oracle ORC list endpoints do not carry the full description (and
  Workday collapses multi-site reqs to "7 Locations"), so they get a `fetch_detail`
  that fills description_text, locations, dates and pay. Detail is fetched once per
  posting, never re-fetched — see sweep.py's detail stage.
- A detail call that 404s means the posting is gone; the caller closes it.
"""
import inspect
import json
import os
import re
import time
from urllib.parse import quote

import httpx

from backend import profile as P

from . import normalize as N

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
TIMEOUT = 30.0
PAGE_DELAY = 0.15       # seconds between pages of one board (same host)
RETRIES = 3


class Gone(Exception):
    """The posting no longer exists on the board (HTTP 404 / 410 on detail)."""


class Truncated(list):
    """A list of postings that stopped early (max_pages hit). Not safe to close-pass on."""
    truncated = True


class PlacesPulled(list):
    """Result of a `places`-strategy pull (sprint plan §31.3): postings from a UNION of
    place-scoped queries, each tagged on `_bridge_places` with the place name(s) that matched it.
    `place_status` ({place: bool}) says which places came back clean (no error, no page-cap
    truncation) -- the caller's close pass (store.record_bridge_board) closes a posting only when
    every place it is tagged with is both attempted and True here. Never safe to close-pass with
    the ORDINARY whole-board absence test (`truncated = True` is a defensive belt-and-suspenders
    in case this ever reaches sweep.py's generic fit-track path by accident -- the bridge sweep
    uses record_bridge_board, never record_board, for a PlacesPulled result)."""
    truncated = True

    def __init__(self, postings, place_status):
        super().__init__(postings)
        self.place_status = dict(place_status)


def client():
    return httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}, follow_redirects=True)


def _request(c, method, url, **kw):
    """One request with retries on transient failures (DNS blips, 429, 5xx, timeouts)."""
    last = None
    for attempt in range(RETRIES):
        try:
            resp = c.request(method, url, **kw)
            if resp.status_code in (429, 500, 502, 503, 504):
                last = httpx.HTTPStatusError(f"{resp.status_code} on {url}", request=resp.request, response=resp)
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code in (404, 410):
                raise Gone(f"{resp.status_code} {url}")
            resp.raise_for_status()
            return resp
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


# ================================================================ Workday
def _workday_host(tenant, wd):
    # A tenant with an underscore (osv_cfainstitute) is not a valid hostname label,
    # so it can't be reached at <tenant>.wdN.myworkdayjobs.com — but Workday serves
    # every tenant from the generic wdN.myworkdaysite.com host too.
    if "_" in tenant:
        return f"https://{wd}.myworkdaysite.com"
    return f"https://{tenant}.{wd}.myworkdayjobs.com"


WORKDAY_USA = "bc33aa3152ec42d4995f4791a106ed09"   # Workday's global location id for the United States


WORKDAY_CEILING = 2000          # the clamp value observed on every affected tenant so far


def _workday_total_is_clamped(data) -> bool:
    """True when CXS's `total` is a ceiling rather than the real count.

    Facet counts are not clamped, so a facet summing past `total` exposes the clamp -- but only a
    SINGLE-VALUED facet may be used. A posting in three cities is counted three times in the location
    facet, and `workerSubType` is multi-valued too (Accenture: 86,767 against 44,187 postings), so the
    widest-facet test called Autodesk (405) and Guidehouse (757) clamped when they are not.
    `timeType` (Full time / Part time) is one value per posting, so its sum is the true count:
    Accenture 44,216 vs total 2000, Booz Allen 2,386, Leidos 2,192 -- against Henry Schein, Autodesk,
    Guidehouse and GE Vernova all at ratio 1.00.

    Probing past the ceiling does not work as a test: Workday answers any out-of-range offset with
    page 1 rather than an empty page, so even a genuine board looks like it has more.
    """
    total = data.get("total") or 0
    if not total:
        return False
    real = workday_true_total(data)
    if real:
        return real > total * 1.05
    return total == WORKDAY_CEILING     # no single-valued facet to check: trust the known ceiling


def workday_true_total(data):
    """The board's real posting count from `timeType`, or None when the board does not publish it.

    `timeType` (Full time / Part time) is one value per posting, so its facet counts sum to the true count
    even while `total` is pinned at the ceiling. Every multi-valued facet over-counts -- a posting in three
    cities appears three times in the location facet -- so no other facet may be used for this.
    """
    for f in (data.get("facets") or []):
        if f.get("facetParameter") == "timeType":
            real = sum(v.get("count") or 0 for v in (f.get("values") or []))
            if real:
                return real
    return None


def _workday_scope(c, url):
    """(appliedFacets, clamped) for one board.

    A clamped board is re-scoped to the US, which is what the rules keep anyway and is usually
    far under the ceiling (Accenture 44,187 -> 710). The US facet is NOT universally supported:
    some tenants reject it with HTTP 400 (Booz Allen, Sentara) and some accept it and silently
    ignore it (GE Vernova returned an unchanged total and French locations), so the filter is
    used only when the total actually moves. `clamped` stays True either way -- a board we could
    not fully enumerate must never be close-passed, or every row outside the window looks taken
    down. Dead reqs on such a board are still closed individually by the detail stage's 404.
    """
    probe = _request(c, "POST", url, json={"appliedFacets": {}, "limit": 20, "offset": 0}).json()
    if not _workday_total_is_clamped(probe):
        return {}, False
    facets = {"locationCountry": [WORKDAY_USA]}
    try:
        scoped = _request(c, "POST", url, json={"appliedFacets": facets, "limit": 20, "offset": 0}).json()
    except Exception:
        return {}, True                                  # rejected outright: pull what we can, no close-pass
    total = scoped.get("total")
    if not total or total >= (probe.get("total") or 0):  # accepted but ignored, not applied
        return {}, True
    return facets, True


def _workday_plan(scope):
    """([(label, appliedFacets)], clamped) -- the pulls that together cover one board.

    Every strategy is the same execution path with a different plan, so `partition` is a plan builder
    rather than a second pull mechanism: `plain` is one unfiltered pull, `country` one US-filtered pull,
    `partition` one pull per facet value, unioned.
    """
    if not scope:
        return None, False                                   # caller probes live
    strategy = scope.get("strategy")
    param = scope.get("facet_parameter")
    ids = json.loads(scope.get("value_ids") or "null") or []
    if strategy == "country" and param and ids:
        return [("us", {param: ids})], False
    if strategy == "partition" and param and ids:
        return [(v, {param: [v]}) for v in ids], False
    return [("all", {})], strategy == "truncate"


def _workday_page_ids(postings):
    """The req/path keys of one CXS page, used to detect Workday repeating page 1."""
    ids = []
    for p in postings:
        bullets = p.get("bulletFields") or []
        ids.append(bullets[0] if bullets else p.get("externalPath"))
    return ids


def _workday_pull(c, url, public, applied, max_pages, search_text=""):
    """(postings, truncated) for one filtered pull, paginating CXS to the end. `search_text`
    (sprint plan §31.3) is the bridge track's per-place free-text query; every whole-board pull
    leaves it "" (unfiltered), exactly as before."""
    out, offset, limit, total, pages = [], 0, 20, None, 0     # CXS rejects limit > 20
    clamped, first_page_ids = False, None
    while True:
        body = {"appliedFacets": applied, "limit": limit, "offset": offset, "searchText": search_text}
        data = _request(c, "POST", url, json=body).json()
        postings = data.get("jobPostings") or []
        if total is None:
            # Only the first page reports a trustworthy total; Wells Fargo's tenant returns
            # total=0 on every later page (found 2026-09-14), so never re-read it.
            total = data.get("total") or 0
            # A board previously discovered as unclamped (or one with a stored `plain`/`country`
            # scope) can grow past the ceiling between runs -- check every pull's first page live
            # rather than trusting board_scope, or the close-pass silently closes real reqs.
            if _workday_total_is_clamped(data):
                clamped = True
            first_page_ids = _workday_page_ids(postings)
        elif offset > 0 and total == 0 and _workday_page_ids(postings) == first_page_ids:
            # Workday answers an out-of-range offset with page 1 rather than an empty page, so a
            # board with no trustworthy `total` (0 or missing) needs its own stop condition:
            # once a later page repeats page 1's ids, the board has been fully seen -- and since
            # we can't confirm that from `total`, the pull is never safe to close-pass on.
            return out, True
        for p in postings:
            bullets = p.get("bulletFields") or []
            req_id = bullets[0] if bullets else p.get("externalPath")
            loc_text = p.get("locationsText") or p.get("locationText")
            out.append(N.base(
                req_id=req_id,
                title=p.get("title"),
                url=f"{public}{p.get('externalPath', '')}",
                location_primary=N.primary_location(loc_text),
                workplace_type=N.workplace_type(None, loc_text),
                posted_at=N.parse_date(p.get("postedOn")),
                raw_json=N.raw(p),
                _external_path=p.get("externalPath"),
            ))
        pages += 1
        offset += limit
        if len(postings) < limit or (total and offset >= total):
            return out, clamped
        if max_pages and pages >= max_pages:
            return out, True
        time.sleep(PAGE_DELAY)


def _workday_probe_total(c, url):
    """The board's true posting count right now, from one cheap unfiltered page-0 call."""
    return workday_true_total(_request(c, "POST", url, json={"appliedFacets": {}, "limit": 1, "offset": 0}).json())


def _workday_places_jobs(row, places, max_pages=None, log=print):
    """PlacesPulled -- one CXS searchText query per place (sprint plan §31.3), unioned on req id
    and location-checked in code (backend.ats.bridge.location_matches). `places` is
    registry.load_bridge_places()'s list of {place, ring, search_text, state} dicts. Never a
    whole-board pull: a board with no places configured is the CALLER's problem (sweep.py skips
    it loudly), not this function's -- it simply pulls whatever `places` it is given."""
    from . import bridge as B

    tenant, wd, site = row["identifier_1"], row["identifier_2"], row["identifier_3"]
    if not site:
        raise ValueError(f"{row['employer']}: Workday row has no site slug (identifier_3)")
    url = f"{_workday_host(tenant, wd)}/wday/cxs/{tenant}/{site}/jobs"
    public = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
    page_cap = max_pages or B.PLACE_PAGE_CAP
    seen, place_status = {}, {}
    with client() as c:
        for place in places:
            search_text = place.get("search_text") or place["place"]
            # Seam for a later facet-based lookup (sprint plan §31.3): a place row MAY carry an
            # explicit facet key + ids once one is known for a given board; none of the boards
            # probed so far needed one -- searchText alone was enough (§31.1).
            applied = place.get("facet_applied") or {}
            try:
                got, truncated = _workday_pull(c, url, public, applied, page_cap, search_text=search_text)
            except Exception as e:  # noqa: BLE001 — one bad place must not stop the others
                log(f"    place {place['place']!r}: FAILED — {type(e).__name__}: {str(e)[:90]}")
                place_status[place["place"]] = False
                continue
            if truncated:
                log(f"    place {place['place']!r}: hit the {page_cap}-page cap -- not closing on this place")
            place_status[place["place"]] = not truncated
            city, parsed_state = B.parse_place(place["place"])
            state = place.get("state") or parsed_state
            allow_no_state = bool(place.get("allow_no_state"))
            dropped = multi_site = 0
            for p in got:
                loc = p.get("location_primary") or ""
                if B.is_multi_site_text(loc):
                    multi_site += 1
                    continue
                if not B.location_matches(loc, city, state, allow_no_state=allow_no_state):
                    dropped += 1
                    continue
                key = p.get("req_id") or p.get("url")
                entry = seen.get(key)
                if entry is None:
                    entry = p
                    entry["_bridge_places"] = []
                    seen[key] = entry
                if place["place"] not in entry["_bridge_places"]:
                    entry["_bridge_places"].append(place["place"])
            if dropped or multi_site:
                log(f"    place {place['place']!r}: dropped {dropped} result(s) whose location text "
                    f"did not actually name this place, {multi_site} multi-site result(s) skipped")
    return PlacesPulled(list(seen.values()), place_status)


def workday_jobs(row, max_pages=None, scope=None):
    """One board, pulled according to its stored `board_scope` plan (or a live probe when it has none).
    A `places` scope (sprint plan §31.3, the bridge track) is a different pull shape entirely --
    per-place searchText queries, never a live-discovered whole-board plan -- so it is handled by
    _workday_places_jobs and returned immediately."""
    if scope and scope.get("strategy") == "places":
        return _workday_places_jobs(row, scope.get("places") or [], max_pages=max_pages)
    tenant, wd, site = row["identifier_1"], row["identifier_2"], row["identifier_3"]
    if not site:
        raise ValueError(f"{row['employer']}: Workday row has no site slug (identifier_3)")
    url = f"{_workday_host(tenant, wd)}/wday/cxs/{tenant}/{site}/jobs"
    public = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
    plan, clamped = _workday_plan(scope)
    out, seen = [], set()
    with client() as c:
        if plan is None:                                     # never discovered: fall back to probing
            applied, clamped = _workday_scope(c, url)
            plan = [("all", applied)]
        # A partition claims to enumerate a board whose `total` lies, so it has to prove it: probe the true
        # count before and after, and check the union against it. A facet that leaves postings out (a req
        # with no job family, say) would otherwise close-pass every one of them as taken down.
        partitioned = len(plan) > 1
        before = _workday_probe_total(c, url) if partitioned else None
        for _label, applied in plan:
            got, truncated = _workday_pull(c, url, public, applied, max_pages)
            clamped = clamped or truncated
            for p in got:                                    # partitions overlap; union on the req key
                key = p.get("req_id") or p.get("url")
                if key not in seen:
                    seen.add(key)
                    out.append(p)
        if before:
            # A partition takes minutes, and reqs open and close inside that window: Booz Allen moved
            # 2,394 -> 2,396 during a 100-second pull, leaving the union two short of a facet that covers
            # the board exactly. So a shortfall is forgiven only up to the churn we actually measured --
            # drift explains a gap of two, never a gap of forty.
            after = _workday_probe_total(c, url) or before
            if len(out) + abs(after - before) < before:
                clamped = True                               # incomplete enumeration: pull it, never close-pass
    return Truncated(out) if clamped else out


def workday_detail(row, posting):
    tenant, wd, site = row["identifier_1"], row["identifier_2"], row["identifier_3"]
    path = posting.get("_external_path") or _path_from_url(posting.get("url"), site)
    url = f"{_workday_host(tenant, wd)}/wday/cxs/{tenant}/{site}{path}"
    with client() as c:
        info = _request(c, "GET", url).json().get("jobPostingInfo") or {}
    if info.get("posted") is False:
        raise Gone(f"unposted {url}")
    locs = [info.get("location")] + list(info.get("additionalLocations") or [])
    text = N.html_to_text(info.get("jobDescription"))
    pay = N.pay_from_text(text)
    country = ((info.get("country") or {}).get("alpha2Code")
               or ((info.get("jobRequisitionLocation") or {}).get("country") or {}).get("alpha2Code"))
    return dict(
        description_text=text,
        location_primary=info.get("location") or posting.get("location_primary"),
        locations=N.locations_json(locs),
        country=country,
        workplace_type=N.workplace_type(info.get("remoteType"), *[l for l in locs if l]),
        employment_type=N.employment_type(info.get("timeType")),
        posted_at=N.parse_date(info.get("startDate")) or posting.get("posted_at"),
        posting_end_at=N.parse_date(info.get("endDate")),
        pay_min=pay[0] if pay else None, pay_max=pay[1] if pay else None,
        pay_interval=pay[2] if pay else None, pay_source="text" if pay else None,
    )


def _path_from_url(url, site):
    if not url:
        raise ValueError("posting has no url to derive the Workday external path from")
    m = re.search(rf"/{re.escape(site)}(/job/.*)$", url)
    if not m:
        raise ValueError(f"cannot derive external path from {url}")
    return m.group(1)


# ================================================================ Oracle Recruiting Cloud
def oracle_orc_jobs(row, max_pages=None):
    host, site = row["identifier_1"].rstrip("/"), row["identifier_2"]
    # An empty site number hits the tenant's default career site (SAIC's row has none
    # confirmed yet, and the default returned 966 postings on 2026-09-14).
    site_arg = f"siteNumber={site}," if site else ""
    out, offset, limit, pages = [], 0, 100, 0
    with client() as c:
        while True:
            url = (f"{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                   f"?onlyData=true&expand=requisitionList.secondaryLocations"
                   f"&finder=findReqs;{site_arg}limit={limit},offset={offset},sortBy=POSTING_DATES_DESC")
            data = _request(c, "GET", url).json()
            items = (data.get("items") or [{}])[0].get("requisitionList") or []
            for j in items:
                locs = [j.get("PrimaryLocation")] + list(j.get("secondaryLocations") or [])
                out.append(N.base(
                    req_id=str(j.get("Id") or j.get("RequisitionNumber")),
                    title=j.get("Title"),
                    url=f"{host}/hcmUI/CandidateExperience/en/sites/{site}/requisitions/preview/{j.get('Id')}",
                    location_primary=j.get("PrimaryLocation"),
                    locations=N.locations_json(locs),
                    country=j.get("PrimaryLocationCountry"),
                    workplace_type=N.workplace_type(j.get("WorkplaceTypeCode") or j.get("WorkplaceType"), j.get("PrimaryLocation")),
                    employment_type=N.employment_type(j.get("JobSchedule") or j.get("WorkerType")),
                    job_family=j.get("JobFamily") or j.get("JobFunction"),
                    job_level=j.get("ManagerLevel"),
                    posted_at=N.parse_date(j.get("PostedDate")),
                    posting_end_at=N.parse_date(j.get("PostingEndDate")),
                    description_text=None,  # list only carries a teaser; detail fills it
                    raw_json=N.raw(j),
                ))
            pages += 1
            offset += limit
            if len(items) < limit:
                break
            if max_pages and pages >= max_pages:
                return Truncated(out)
            time.sleep(PAGE_DELAY)
    return out


def oracle_orc_detail(row, posting):
    host, site = row["identifier_1"].rstrip("/"), row["identifier_2"]
    site_arg = f",siteNumber={site}" if site else ""
    url = (f"{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
           f'?expand=all&onlyData=true&finder=ById;Id="{posting["req_id"]}"{site_arg}')
    with client() as c:
        items = _request(c, "GET", url).json().get("items") or []
    if not items:
        raise Gone(f"no detail items {url}")
    d = items[0]
    parts = [d.get("ExternalDescriptionStr"),
             f"<p>Responsibilities</p>{d['ExternalResponsibilitiesStr']}" if d.get("ExternalResponsibilitiesStr") else None,
             f"<p>Qualifications</p>{d['ExternalQualificationsStr']}" if d.get("ExternalQualificationsStr") else None]
    text = N.html_to_text("\n".join(p for p in parts if p))
    pay = N.pay_from_text(text)
    locs = [d.get("PrimaryLocation")] + list(d.get("secondaryLocations") or [])
    return dict(
        description_text=text,
        locations=N.locations_json(locs) or posting.get("locations"),
        workplace_type=N.workplace_type(d.get("WorkplaceTypeCode") or d.get("WorkplaceType"), *[l if isinstance(l, str) else "" for l in locs]) or posting.get("workplace_type"),
        job_level=d.get("JobLevel") or posting.get("job_level"),
        posted_at=N.parse_date(d.get("ExternalPostedStartDate")) or posting.get("posted_at"),
        posting_end_at=N.parse_date(d.get("ExternalPostedEndDate")) or posting.get("posting_end_at"),
        pay_min=pay[0] if pay else None, pay_max=pay[1] if pay else None,
        pay_interval=pay[2] if pay else None, pay_source="text" if pay else None,
    )


# ================================================================ Greenhouse
def greenhouse_jobs(row, max_pages=None):
    token = quote(row["identifier_1"], safe="")
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    with client() as c:
        data = _request(c, "GET", url).json()
    out = []
    for j in data.get("jobs") or []:
        loc = (j.get("location") or {}).get("name")
        offices = [o.get("name") for o in (j.get("offices") or [])]
        text = N.html_to_text(j.get("content"))
        pay = N.pay_from_text(text)
        out.append(N.base(
            req_id=str(j.get("id")),
            title=j.get("title"),
            url=j.get("absolute_url"),
            location_primary=loc,
            locations=N.locations_json([loc] + offices),
            workplace_type=N.workplace_type(None, loc, *offices),
            job_family=", ".join(d.get("name") for d in (j.get("departments") or []) if d.get("name")) or None,
            posted_at=N.parse_date(j.get("first_published") or j.get("updated_at")),
            posting_end_at=N.parse_date(j.get("application_deadline")),
            pay_min=pay[0] if pay else None, pay_max=pay[1] if pay else None,
            pay_interval=pay[2] if pay else None, pay_source="text" if pay else None,
            description_text=text,
            raw_json=N.raw({k: v for k, v in j.items() if k != "content"}),
        ))
    return out


# ================================================================ Lever
def lever_text(j: dict) -> str:
    """Opening + every `lists` section (with its heading) + closing, as plain text."""
    parts = [j.get("descriptionPlain") or N.html_to_text(j.get("description"))]
    for section in j.get("lists") or []:
        body = N.html_to_text(section.get("content"))
        heading = (section.get("text") or "").strip()
        if body:
            parts.append(f"{heading}\n{body}" if heading else body)
    parts.append(j.get("additionalPlain") or N.html_to_text(j.get("additional")))
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def lever_jobs(row, max_pages=None):
    company = quote(row["identifier_1"], safe="")
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    with client() as c:
        data = _request(c, "GET", url).json()
    out = []
    for j in data or []:
        cats = j.get("categories") or {}
        sal = j.get("salaryRange") or {}
        loc = cats.get("location")
        all_locs = [loc] + list(cats.get("allLocations") or [])
        # Lever splits a posting into three parts: `description` is the opening blurb, `lists` holds every
        # duties / requirements / skills section, and `additional` is the closing (EEO, benefits). Reading only
        # the first and last captured the wrapper and dropped the job itself.
        text = lever_text(j)
        pay = None if sal.get("min") else N.pay_from_text(text)
        out.append(N.base(
            req_id=j.get("id"),
            title=j.get("text"),
            url=j.get("hostedUrl"),
            location_primary=loc,
            locations=N.locations_json(all_locs),
            country=j.get("country"),
            workplace_type=N.workplace_type(j.get("workplaceType"), *[l for l in all_locs if l]),
            employment_type=N.employment_type(cats.get("commitment")),
            job_family=cats.get("team") or cats.get("department"),
            pay_min=sal.get("min") or (pay[0] if pay else None),
            pay_max=sal.get("max") or (pay[1] if pay else None),
            pay_currency=sal.get("currency"),
            pay_interval=("year" if "year" in str(sal.get("interval", "")) else "hour" if "hour" in str(sal.get("interval", "")) else None) if sal.get("min") else (pay[2] if pay else None),
            pay_source="ats" if sal.get("min") else ("text" if pay else None),
            posted_at=N.parse_date(j.get("createdAt")),
            description_text=text,
            raw_json=N.raw({k: v for k, v in j.items() if k not in ("description", "descriptionPlain", "descriptionBody", "descriptionBodyPlain", "additional", "additionalPlain", "lists", "opening", "openingPlain")}),
        ))
    return out


# ================================================================ Ashby
def ashby_jobs(row, max_pages=None):
    org = quote(row["identifier_1"], safe="")
    url = f"https://api.ashbyhq.com/posting-api/job-board/{org}"
    with client() as c:
        data = _request(c, "GET", url).json()
    out = []
    for j in data.get("jobs") or []:
        loc = j.get("location")
        secondary = [s.get("location") for s in (j.get("secondaryLocations") or []) if isinstance(s, dict)]
        text = j.get("descriptionPlain") or N.html_to_text(j.get("descriptionHtml"))
        comp = j.get("compensation") or {}
        tiers = comp.get("compensationTiers") or comp.get("compensationTierSummary") or []
        pay = N.pay_from_text(comp.get("compensationTierSummary") if isinstance(comp.get("compensationTierSummary"), str) else None) or N.pay_from_text(text)
        out.append(N.base(
            req_id=j.get("id"),
            title=j.get("title"),
            url=j.get("jobUrl") or j.get("applyUrl"),
            location_primary=loc,
            locations=N.locations_json([loc] + secondary),
            workplace_type=N.workplace_type(j.get("workplaceType") or j.get("isRemote"), loc, *secondary),
            employment_type=N.employment_type(j.get("employmentType")),
            job_family=j.get("department") or j.get("team"),
            pay_min=pay[0] if pay else None, pay_max=pay[1] if pay else None,
            pay_interval=pay[2] if pay else None, pay_source="text" if pay else None,
            posted_at=N.parse_date(j.get("publishedAt")),
            description_text=text,
            raw_json=N.raw({k: v for k, v in j.items() if k not in ("descriptionHtml", "descriptionPlain")}),
        ))
    return out


# ================================================================ Workable
def workable_jobs(row, max_pages=None):
    slug = quote(row["identifier_1"], safe="")
    url = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
    body = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
    out, pages = [], 0
    with client() as c:
        while True:
            data = _request(c, "POST", url, json=body, headers={"Accept": "application/json"}).json()
            for j in data.get("results") or []:
                locs = [_workable_loc(l) for l in (j.get("locations") or [j.get("location")])]
                out.append(N.base(
                    req_id=j.get("shortcode") or str(j.get("id")),
                    title=j.get("title"),
                    url=f"https://apply.workable.com/{row['identifier_1']}/j/{j.get('shortcode')}/",
                    location_primary=_workable_loc(j.get("location")),
                    locations=N.locations_json(locs),
                    country=(j.get("location") or {}).get("countryCode"),
                    workplace_type=N.workplace_type(j.get("workplace") or j.get("remote"), *locs),
                    employment_type=N.employment_type({"full": "full time", "part": "part time"}.get(j.get("type"), j.get("type"))),
                    job_family=", ".join(j.get("department") or []) or None,
                    posted_at=N.parse_date(j.get("published")),
                    raw_json=N.raw(j),
                ))
            pages += 1
            token = data.get("nextPage")
            if not token or not data.get("results"):
                break
            if max_pages and pages >= max_pages:
                return Truncated(out)
            body = dict(body, token=token)
            time.sleep(PAGE_DELAY)
    return out


def _workable_loc(l):
    if not isinstance(l, dict):
        return l
    return ", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x) or None


def workable_detail(row, posting):
    slug = quote(row["identifier_1"], safe="")
    url = f"https://apply.workable.com/api/v2/accounts/{slug}/jobs/{posting['req_id']}"
    with client() as c:
        d = _request(c, "GET", url, headers={"Accept": "application/json"}).json()
    if d.get("state") and d["state"] != "published":
        raise Gone(f"state={d['state']} {url}")
    parts = [d.get("description"), d.get("requirements"), d.get("benefits")]
    text = N.html_to_text("\n".join(p for p in parts if p))
    pay = N.pay_from_text(text)
    return dict(
        description_text=text,
        pay_min=pay[0] if pay else None, pay_max=pay[1] if pay else None,
        pay_interval=pay[2] if pay else None, pay_source="text" if pay else None,
    )


# ================================================================ BambooHR
def _bamboo_loc(j):
    al = j.get("atsLocation") or {}
    return ", ".join(x for x in (al.get("city"), al.get("state") or al.get("province"), al.get("country")) if x) or None


def bamboohr_jobs(row, max_pages=None):
    sub = row["identifier_1"]
    with client() as c:
        data = _request(c, "GET", f"https://{sub}.bamboohr.com/careers/list", headers={"Accept": "application/json"}).json()
    out = []
    for j in data.get("result") or []:
        loc = _bamboo_loc(j)
        remote = str(j.get("isRemote") or "").lower() in ("yes", "true", "1") or j.get("locationType") == "2"
        out.append(N.base(
            req_id=str(j.get("id")),
            title=j.get("jobOpeningName"),
            url=f"https://{sub}.bamboohr.com/careers/{j.get('id')}",
            location_primary=loc,
            locations=N.locations_json([loc]),
            country=(j.get("atsLocation") or {}).get("country"),
            workplace_type="remote" if remote else N.workplace_type(None, loc),
            employment_type=N.employment_type(j.get("employmentStatusLabel") or j.get("employmentType")),
            job_family=j.get("departmentLabel"),
            raw_json=N.raw(j),
        ))
    return out


def bamboohr_detail(row, posting):
    sub = row["identifier_1"]
    with client() as c:
        data = _request(c, "GET", f"https://{sub}.bamboohr.com/careers/{posting['req_id']}/detail",
                        headers={"Accept": "application/json"}).json()
    jo = (data.get("result") or {}).get("jobOpening") or {}
    if not jo:
        raise Gone(f"no jobOpening for {posting['req_id']}")
    text = N.html_to_text(jo.get("description"))
    pay = N.pay_from_text(text)
    return dict(
        description_text=text,
        posted_at=N.parse_date(jo.get("datePosted")) or posting.get("posted_at"),
        pay_min=pay[0] if pay else None, pay_max=pay[1] if pay else None,
        pay_interval=pay[2] if pay else None, pay_source="text" if pay else None,
    )


# ================================================================ SmartRecruiters
def smartrecruiters_jobs(row, max_pages=None):
    company = quote(row["identifier_1"], safe="")  # case-sensitive; a wrong id returns 200 with 0 jobs
    out, offset, limit, pages = [], 0, 100, 0
    with client() as c:
        while True:
            url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings?limit={limit}&offset={offset}"
            data = _request(c, "GET", url, headers={"Accept": "application/json"}).json()
            items = data.get("content") or []
            for j in items:
                lo = j.get("location") or {}
                loc = lo.get("fullLocation") or ", ".join(x for x in (lo.get("city"), lo.get("region"), lo.get("country")) if x) or None
                wt = "remote" if lo.get("remote") else "hybrid" if lo.get("hybrid") else None
                out.append(N.base(
                    req_id=str(j.get("id")),
                    title=j.get("name"),
                    url=f"https://jobs.smartrecruiters.com/{row['identifier_1']}/{j.get('id')}",
                    location_primary=loc,
                    locations=N.locations_json([loc]),
                    country=(lo.get("country") or "").upper() or None,
                    workplace_type=wt or N.workplace_type(None, loc),
                    employment_type=N.employment_type((j.get("typeOfEmployment") or {}).get("label")),
                    job_family=(j.get("function") or {}).get("label") or (j.get("department") or {}).get("label"),
                    job_level=(j.get("experienceLevel") or {}).get("label"),
                    posted_at=N.parse_date(j.get("releasedDate")),
                    raw_json=N.raw(j),
                ))
            pages += 1
            offset += limit
            if len(items) < limit or offset >= (data.get("totalFound") or 0):
                break
            if max_pages and pages >= max_pages:
                return Truncated(out)
            time.sleep(PAGE_DELAY)
    return out


def smartrecruiters_detail(row, posting):
    company = quote(row["identifier_1"], safe="")
    with client() as c:
        d = _request(c, "GET", f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting['req_id']}",
                     headers={"Accept": "application/json"}).json()
    if d.get("active") is False:
        raise Gone(f"inactive {posting['req_id']}")
    secs = (d.get("jobAd") or {}).get("sections") or {}
    parts = [(secs.get(k) or {}).get("text") for k in ("jobDescription", "qualifications", "additionalInformation", "companyDescription")]
    text = N.html_to_text("\n".join(p for p in parts if p))
    pay = N.pay_from_text(text)
    return dict(
        description_text=text,
        url=d.get("postingUrl") or posting.get("url"),
        pay_min=pay[0] if pay else None, pay_max=pay[1] if pay else None,
        pay_interval=pay[2] if pay else None, pay_source="text" if pay else None,
    )


# ================================================================ USAJobs
# Not a whole-board pull like every adapter above: it is a KEYWORD search (the same phrases
# backend/profile.py already holds for sweep.py's older aggregator pass, so nothing is
# duplicated), paged over data.usajobs.gov. Sprint plan §22.1 "Known gap": sweep.py's own
# usajobs() only feeds the rule engine and never reaches DuckDB, so federal postings skipped
# the whole cascade (coverage, models, rank, report). This adapter puts them in it.
#
# The search API returns the full duties/qualifications text inline, so description_text is
# complete from the list call alone -- this platform is deliberately absent from _DETAIL and
# never spends detail budget.
#
# CLOSING: a keyword result set never enumerates the board, so a posting missing from it is
# never evidence it's gone -- the list call always comes back `Truncated`, which already makes
# record_board's absence-based close-pass a no-op for this platform (see backend/ats/store.py's
# CLOSE_BY_DATE_PLATFORMS / close_expired_postings, called from backend/ats/sweep.py). The
# posting's own ApplicationCloseDate (-> posting_end_at) is the only thing that ever closes it.
USAJOBS_HOST = "https://data.usajobs.gov/api/search"
USAJOBS_PAGE = 500   # API's documented ResultsPerPage ceiling

# Values meaning "no clearance required". Anything else becomes one sentence in the OBTAINABLE form
# ("ability to obtain and maintain a Secret security clearance"): a federal agency sponsors the
# investigation as a condition of employment, so the structured field states the position's level,
# not something the applicant must already hold. screen.clearance_call reads that as `sponsored`
# (a flag, never a reject). A posting that truly needs a clearance held on day one says so in its
# Requirements text, which is also in description_text, and the rule rejects on that.
_USAJOBS_NO_CLEARANCE = {"", "none", "not required", "not applicable", "n/a", "none required"}


def _usajobs_creds():
    key, email = os.getenv("USAJOBS_API_KEY"), os.getenv("USAJOBS_EMAIL")
    if not key or not email:
        return None
    return key, email


def _usajobs_clearance_line(value):
    v = (value or "").strip()
    if not v or v.lower() in _USAJOBS_NO_CLEARANCE:
        return None
    return (f"Security clearance: the agency sponsors the investigation; ability to obtain and maintain "
            f"a {v} security clearance is a condition of employment.")


def _usajobs_workplace(details, *locs):
    remote = details.get("RemoteIndicator")
    if isinstance(remote, str):
        remote = remote.strip().lower() in ("true", "yes", "1")
    if remote:
        return "remote"
    return N.workplace_type(None, *locs)


def _usajobs_grade(m, details):
    code = (m.get("JobGrade") or [{}])[0].get("Code") or ""
    lo, hi = details.get("LowGrade") or "", details.get("HighGrade") or ""
    if not (code or lo or hi):
        return None
    return f"{code}-{lo}/{hi}".strip("-/")


def _usajobs_position(m, control_number=None):
    """One SearchResultItem's MatchedObjectDescriptor -> normalized posting. `control_number` is the
    item's MatchedObjectId (the USAJobs control number, unique per announcement)."""
    details = (m.get("UserArea") or {}).get("Details") or {}
    locs = [l.get("LocationName") for l in (m.get("PositionLocation") or []) if l.get("LocationName")]
    pay = (m.get("PositionRemuneration") or [{}])[0]
    lo, hi = pay.get("MinimumRange"), pay.get("MaximumRange")
    rate = (pay.get("RateIntervalCode") or "").lower()
    interval = "hour" if "hour" in rate else ("year" if (lo or hi) else None)
    duties = [d for d in (details.get("MajorDuties") or []) if d]
    parts = [
        details.get("JobSummary"),
        m.get("QualificationSummary"),
        ("Duties\n" + "\n".join(f"- {d}" for d in duties)) if duties else None,
        details.get("Education"),
        details.get("Requirements"),
        details.get("Evaluations"),
        _usajobs_clearance_line(details.get("SecurityClearance")),
        details.get("OtherInformation"),
    ]
    text = "\n\n".join(p for p in parts if p) or None
    country = (m.get("PositionLocation") or [{}])[0].get("CountryCode") or "US"
    return N.base(
        req_id=str(control_number or m.get("PositionID") or ""),
        title=m.get("PositionTitle"),
        url=m.get("PositionURI"),
        location_primary=m.get("PositionLocationDisplay") or (locs[0] if locs else None),
        locations=N.locations_json(locs),
        country=country,
        workplace_type=_usajobs_workplace(details, *locs, m.get("PositionLocationDisplay") or ""),
        employment_type=N.employment_type(
            (m.get("PositionSchedule") or [{}])[0].get("Name")
            or (m.get("PositionOfferingType") or [{}])[0].get("Name")),
        # `employer` is fixed for the whole board (see usajobs_jobs); the real hiring agency is
        # kept here so it isn't lost, not in `employer` -- see the registry note in the README.
        job_family=m.get("OrganizationName") or (m.get("JobCategory") or [{}])[0].get("Name"),
        job_level=_usajobs_grade(m, details),
        pay_min=int(float(lo)) if lo else None, pay_max=int(float(hi)) if hi else None,
        pay_currency="USD" if (lo or hi) else None,
        pay_interval=interval, pay_source="ats" if (lo or hi) else None,
        posted_at=N.parse_date(m.get("PublicationStartDate")),
        posting_end_at=N.parse_date(m.get("ApplicationCloseDate")),
        description_text=text,
        raw_json=N.raw(m),
    )


def usajobs_jobs(row, max_pages=None):
    """Pages every phrase in backend/profile.py's FUNCTION_PHRASES + SEPARATE_PASS_PHRASES
    through the USAJobs search API, de-duplicated by control number. Always returns a
    `Truncated` list -- see the module comment above; this is a keyword result set, never a
    full board, so it must never be close-passed on absence."""
    creds = _usajobs_creds()
    if creds is None:
        print("USAJobs: USAJOBS_API_KEY/USAJOBS_EMAIL not set -- skipping "
              "(set both in .env to enable; see .env.template)")
        return Truncated([])
    key, email = creds
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}
    phrases = P.FUNCTION_PHRASES + P.SEPARATE_PASS_PHRASES
    seen, out = {}, []
    with client() as c:
        for phrase in phrases:
            page, pages = 1, 0
            while True:
                params = {"Keyword": phrase, "ResultsPerPage": USAJOBS_PAGE, "Page": page}
                data = _request(c, "GET", USAJOBS_HOST, params=params, headers=headers).json()
                result = data.get("SearchResult") or {}
                items = result.get("SearchResultItems") or []
                for item in items:
                    m = item.get("MatchedObjectDescriptor") or {}
                    p = _usajobs_position(m, item.get("MatchedObjectId"))
                    if p["req_id"] and p["req_id"] not in seen:
                        seen[p["req_id"]] = True
                        out.append(p)
                pages += 1
                total = result.get("SearchResultCountAll") or 0
                if len(items) < USAJOBS_PAGE or page * USAJOBS_PAGE >= total:
                    break
                if max_pages and pages >= max_pages:
                    break
                page += 1
                time.sleep(PAGE_DELAY)
    return Truncated(out)


# ================================================================ dispatch

# ================================================================ Eightfold
# Two public API flavors exist, and a tenant answers one or the other:
#   apply/v2/jobs  — richer list (Liberty Mutual); 403 on some tenants (Microsoft)
#   pcsx/search    — thin list (Microsoft, Omnicell); wraps the payload in {"data": ...}
# Both page 10 at a time no matter what `num` says. The list's job_description is a
# truncated preview on apply/v2 and absent on pcsx, so the detail call
# (apply/v2/jobs/<id>, which works even where the apply/v2 list is refused) always
# supplies the JD. Registry: identifier_1 = host URL, identifier_2 = domain.
EIGHTFOLD_PAGE = 10


def _eightfold_base(row):
    host = row["identifier_1"].rstrip("/")
    domain = row["identifier_2"]
    if not host.startswith("http") or not domain:
        raise ValueError(f"{row['employer']}: Eightfold row needs host URL (identifier_1) and domain (identifier_2)")
    return host, domain


def _eightfold_workplace(value, *locs):
    v = (value or "").lower()
    if v.startswith("remote"):
        return "remote"
    return N.workplace_type(v or None, *locs)


def _eightfold_position(host, p):
    """One list record (either flavor) -> normalized posting. Never sets
    description_text: the list JD is a preview, and the detail stage fills it."""
    ef_id = p.get("id")
    locs = [l for l in (p.get("locations") or []) if l]
    if p.get("location") and p["location"] not in locs:
        locs.insert(0, p["location"])
    url = p.get("canonicalPositionUrl") or (host + p["positionUrl"] if p.get("positionUrl") else f"{host}/careers/job/{ef_id}")
    posted = p.get("postedTs") or p.get("t_update") or p.get("creationTs") or p.get("t_create")
    # Eightfold's own id is the unique key; a display_job_id is reused when one req is
    # posted in several locations (Microsoft: 21 of 2,240 on 2026-09-15). The display id
    # stays in raw_json.
    return N.base(
        req_id=str(ef_id),
        title=p.get("name") or p.get("posting_name"),
        url=url,
        location_primary=locs[0] if locs else None,
        locations=N.locations_json(locs),
        workplace_type=_eightfold_workplace(p.get("work_location_option") or p.get("workLocationOption"), *locs),
        job_family=p.get("department") or None,
        posted_at=N.parse_date(posted),
        raw_json=N.raw({k: v for k, v in p.items() if k != "job_description"}),
        _ef_id=ef_id,
    )


def _eightfold_pages(c, url, host, max_pages):
    """One ordered pass over a board. Returns (postings, count, truncated)."""
    out, start, total, pages = [], 0, None, 0
    while True:
        data = _request(c, "GET", f"{url}&start={start}&num={EIGHTFOLD_PAGE}").json()
        if "data" in data and "positions" not in data:  # pcsx wrapper
            data = data["data"] or {}
        positions = data.get("positions") or []
        if total is None:
            total = int(data.get("count") or 0)
        out.extend(_eightfold_position(host, p) for p in positions)
        pages += 1
        start += EIGHTFOLD_PAGE
        if not positions:
            # A tenant that omits `count` reports total=0 even with real postings (found on a
            # board that returned a full first page of 10 with no `count` field at all); a
            # non-empty page under total=0 is never a complete board, so it must come back
            # Truncated or the close-pass reads every other posting as taken down.
            return out, total, bool(out) and not total
        if total and start >= total:
            return out, total, False
        if max_pages and pages >= max_pages:
            return out, total, True
        time.sleep(PAGE_DELAY)


def _eightfold_places_jobs(row, places, max_pages=None, log=print):
    """PlacesPulled -- one Eightfold `query=` search per place (sprint plan §31.3), unioned on the
    Eightfold id and location-checked in code, same shape as _workday_places_jobs."""
    from . import bridge as B
    from urllib.parse import quote as _q

    host, domain = _eightfold_base(row)
    page_cap = max_pages or B.PLACE_PAGE_CAP
    seen, place_status = {}, {}
    with client() as c:
        base = None
        for cand in (f"{host}/api/apply/v2/jobs?domain={domain}", f"{host}/api/pcsx/search?domain={domain}"):
            resp = c.get(f"{cand}&start=0&num=1")
            if resp.status_code == 403:
                continue
            resp.raise_for_status()
            base = cand
            break
        if base is None:
            raise RuntimeError(f"{row['employer']}: both Eightfold list endpoints refused (403)")
        for place in places:
            query = place.get("search_text") or place["place"]
            try:
                got, _total, truncated = _eightfold_pages(c, f"{base}&query={_q(query)}", host, page_cap)
            except Exception as e:  # noqa: BLE001 — one bad place must not stop the others
                log(f"    place {place['place']!r}: FAILED — {type(e).__name__}: {str(e)[:90]}")
                place_status[place["place"]] = False
                continue
            if truncated:
                log(f"    place {place['place']!r}: hit the {page_cap}-page cap -- not closing on this place")
            place_status[place["place"]] = not truncated
            city, parsed_state = B.parse_place(place["place"])
            state = place.get("state") or parsed_state
            allow_no_state = bool(place.get("allow_no_state"))
            dropped = multi_site = 0
            for p in got:
                loc = p.get("location_primary") or ""
                if B.is_multi_site_text(loc):
                    multi_site += 1
                    continue
                if not B.location_matches(loc, city, state, allow_no_state=allow_no_state):
                    dropped += 1
                    continue
                key = p.get("req_id") or p.get("url")
                entry = seen.get(key)
                if entry is None:
                    entry = p
                    entry["_bridge_places"] = []
                    seen[key] = entry
                if place["place"] not in entry["_bridge_places"]:
                    entry["_bridge_places"].append(place["place"])
            if dropped or multi_site:
                log(f"    place {place['place']!r}: dropped {dropped} result(s) whose location text "
                    f"did not actually name this place, {multi_site} multi-site result(s) skipped")
    return PlacesPulled(list(seen.values()), place_status)


def eightfold_jobs(row, max_pages=None, scope=None):
    if scope and scope.get("strategy") == "places":
        return _eightfold_places_jobs(row, scope.get("places") or [], max_pages=max_pages)
    host, domain = _eightfold_base(row)
    with client() as c:
        base = None
        for cand in (f"{host}/api/apply/v2/jobs?domain={domain}", f"{host}/api/pcsx/search?domain={domain}"):
            resp = c.get(f"{cand}&start=0&num=1")
            if resp.status_code == 403:
                continue
            resp.raise_for_status()
            base = cand
            break
        if base is None:
            raise RuntimeError(f"{row['employer']}: both Eightfold list endpoints refused (403)")

        # Pages shift while a long pull runs: a posting the employer refreshes jumps to
        # the top of the timestamp order and everything under it moves one slot, so a
        # 224-page pull (Microsoft) both repeats and skips postings. A second pass in the
        # other order fills most gaps; if the union is still short of the board's own
        # count, the pull is reported truncated so nothing gets closed on its account.
        seen = {}
        truncated = False
        for order in ("timestamp", "relevance"):
            got, total, cut = _eightfold_pages(c, f"{base}&sort_by={order}", host, max_pages)
            truncated = truncated or cut
            for p in got:
                seen.setdefault(p["req_id"], p)
            if cut or len(seen) >= total:
                break
    # The board's own count also moves during a long pull (Microsoft: 2,240 -> 2,237 in
    # 200 s), so an exact match is rare on a big board. A union within 0.5% (at least 3)
    # of the count is treated as complete; a missed posting that was already in the DB
    # is closed and then reopened on the next run, which is self-healing.
    out = list(seen.values())
    if truncated or len(out) < total - max(3, int(total * 0.005)):
        return Truncated(out)
    return out


def _eightfold_detail_fields(d, posting):
    locs = [l for l in (d.get("locations") or []) if l]
    if d.get("location") and d["location"] not in locs:
        locs.insert(0, d["location"])
    text = N.html_to_text(d.get("job_description"))
    pay = N.pay_from_text(text)
    return dict(
        description_text=text,
        location_primary=locs[0] if locs else posting.get("location_primary"),
        locations=N.locations_json(locs) if locs else posting.get("locations"),
        workplace_type=_eightfold_workplace(d.get("work_location_option"), *locs) or posting.get("workplace_type"),
        job_family=d.get("department") or posting.get("job_family"),
        posted_at=N.parse_date(d.get("t_update") or d.get("t_create")) or posting.get("posted_at"),
        pay_min=pay[0] if pay else None, pay_max=pay[1] if pay else None,
        pay_interval=pay[2] if pay else None, pay_source="text" if pay else None,
    )


def eightfold_detail(row, posting):
    host, domain = _eightfold_base(row)
    ef_id = posting.get("_ef_id")
    if ef_id is None:
        m = re.search(r"/job/(\d+)", posting.get("url") or "")
        if not m:
            raise ValueError("posting has no Eightfold id to fetch detail with")
        ef_id = m.group(1)
    with client() as c:
        d = _request(c, "GET", f"{host}/api/apply/v2/jobs/{ef_id}?domain={domain}").json()
    if not d.get("id"):
        raise Gone(f"empty detail for {ef_id}")
    return _eightfold_detail_fields(d, posting)


# ================================================================ Paylocity
# No public JSON endpoint that works for every company: the documented v2 feed
# (recruiting/v2/api/feed/jobs/<guid>) returns an empty list when the company hasn't
# enabled it. The all-jobs page embeds the full listing, descriptions included, as a
# `window.pageData = {...};` script block, but its Description is a ~110-char teaser,
# so the detail stage reads the job-preview-details block of each job's Details page. Registry: identifier_1 = company GUID (from the jobs/All/<guid>/
# URL a job's Details page links to), identifier_2 = the cosmetic slug after it.
_PAYLOCITY_PAGEDATA = re.compile(r"window\.pageData\s*=\s*(\{.*?\});\s*\n", re.S)


def paylocity_jobs(row, max_pages=None):
    guid, slug = row["identifier_1"], row["identifier_2"] or "jobs"
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", guid or ""):
        raise ValueError(f"{row['employer']}: Paylocity row needs the company GUID in identifier_1")
    url = f"https://recruiting.paylocity.com/recruiting/jobs/All/{guid}/{slug}"
    with client() as c:
        resp = _request(c, "GET", url)
    if "JobNotFound" in str(resp.url):
        raise RuntimeError(f"{row['employer']}: Paylocity company {guid} not found (redirected to JobNotFound)")
    m = _PAYLOCITY_PAGEDATA.search(resp.text)
    if not m:
        raise RuntimeError(f"{row['employer']}: no pageData block on {url}")
    return _paylocity_positions(json.loads(m.group(1)).get("Jobs") or [])


def _paylocity_positions(jobs):
    out = []
    for j in jobs:
        if j.get("IsInternal"):
            continue
        loc = j.get("JobLocation") or {}
        name = j.get("LocationName") or loc.get("Name")
        metro = loc.get("Metro")
        out.append(N.base(
            req_id=str(j.get("JobId")),
            title=j.get("JobTitle"),
            url=f"https://recruiting.paylocity.com/recruiting/jobs/Details/{j.get('JobId')}",
            location_primary=metro or name,
            locations=N.locations_json([name, metro]),
            country=loc.get("Country") or None,
            workplace_type=N.workplace_type(j.get("IsRemote") or None, name, metro),
            job_family=j.get("HiringDepartment") or None,
            posted_at=N.parse_date(j.get("PublishedDate")),
            raw_json=N.raw({k: v for k, v in j.items() if k != "Description"}),
        ))
    return out


def paylocity_detail(row, posting):
    """The all-jobs block carries only a ~110-character teaser; the full description is
    the job-preview-details block on the job's Details page."""
    url = posting.get("url") or f"https://recruiting.paylocity.com/recruiting/jobs/Details/{posting.get('req_id')}"
    with client() as c:
        resp = _request(c, "GET", url)
    if "JobNotFound" in str(resp.url):
        raise Gone(f"JobNotFound {url}")
    return _paylocity_detail_fields(resp.text, posting)


def _paylocity_detail_fields(html, posting):
    from bs4 import BeautifulSoup
    div = BeautifulSoup(html, "lxml").select_one("div.job-preview-details")
    if div is None:
        raise RuntimeError("no job-preview-details block on Paylocity Details page")
    text = div.get_text("\n", strip=True)
    job_type = None
    m = re.search(r"^Job Type\n([^\n]+)", text, re.M)
    if m:
        job_type = m.group(1)
    # Drop the header rows (Apply / Job Type / <type>) and the "Description" label.
    text = re.sub(r"^(?:Apply\n)?(?:Job Type\n[^\n]+\n)?(?:Description\n)?", "", text)
    pay = N.pay_from_text(text)
    return dict(
        description_text=text,
        employment_type=N.employment_type(job_type) or posting.get("employment_type"),
        pay_min=pay[0] if pay else None, pay_max=pay[1] if pay else None,
        pay_interval=pay[2] if pay else None, pay_source="text" if pay else None,
    )

_LIST = {
    "workday": workday_jobs,
    "oracle_orc": oracle_orc_jobs,
    "greenhouse": greenhouse_jobs,
    "lever": lever_jobs,
    "ashby": ashby_jobs,
    "workable": workable_jobs,
    "bamboohr": bamboohr_jobs,
    "smartrecruiters": smartrecruiters_jobs,
    "eightfold": eightfold_jobs,
    "paylocity": paylocity_jobs,
    "usajobs": usajobs_jobs,
}
_DETAIL = {
    "workday": workday_detail,
    "oracle_orc": oracle_orc_detail,
    "workable": workable_detail,
    "bamboohr": bamboohr_detail,
    "smartrecruiters": smartrecruiters_detail,
    "eightfold": eightfold_detail,
    "paylocity": paylocity_detail,
}
IMPLEMENTED_PLATFORMS = frozenset(_LIST)
DETAIL_PLATFORMS = frozenset(_DETAIL)

# Platforms with a real places-strategy implementation (sprint plan §31.3). Orchestrator audit
# 2026-09-21, letter A: `list_jobs` must never silently do an ordinary whole-board pull for a
# `scope={"strategy": "places", ...}` call on a platform NOT in this set -- see the guard below.
PLACES_PLATFORMS = frozenset({"workday", "eightfold"})


def list_jobs(row, max_pages=None, scope=None):
    """Whole-board pull. Raises on hard failure so the caller never close-passes a
    board it didn't actually read. Returns a `Truncated` list if max_pages tripped.

    `scope` is the board's `board_scope` row; adapters that understand it pull to its plan.

    A `scope` whose strategy is `places` (the bridge track, sprint plan §31.3) is never forwarded
    to a platform function that has no `scope` parameter at all -- that used to fall through
    silently into an ordinary, unfiltered whole-board call (orchestrator audit 2026-09-21, letter
    A: `greenhouse_jobs(row, max_pages=None)` has no `scope` param, so the old
    `"scope" in inspect.signature(fn).parameters` guard let a places-scoped pull on such a
    platform quietly become a whole-board pull). A places-strategy scope now raises outright on
    any platform outside PLACES_PLATFORMS, before any request is made."""
    fn = _LIST.get(row["platform"])
    if fn is None:
        raise ValueError(f"no adapter for platform {row['platform']!r}")
    if scope is not None and scope.get("strategy") == "places":
        if row["platform"] not in PLACES_PLATFORMS:
            raise ValueError(
                f"{row['employer']}: platform {row['platform']!r} has no places-scoped adapter "
                f"(supported: {sorted(PLACES_PLATFORMS)}) -- refusing to fall back to a whole-board pull")
        return fn(row, max_pages=max_pages, scope=scope)
    if scope is not None and "scope" in inspect.signature(fn).parameters:
        return fn(row, max_pages=max_pages, scope=scope)
    return fn(row, max_pages=max_pages)


def fetch_detail(row, posting):
    """Per-posting detail for platforms whose list call is thin. Raises Gone when the
    posting has been taken down (which is itself useful: a free liveness check)."""
    fn = _DETAIL.get(row["platform"])
    if fn is None:
        raise ValueError(f"no detail adapter for platform {row['platform']!r}")
    return fn(row, posting)
