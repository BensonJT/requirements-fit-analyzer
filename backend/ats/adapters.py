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
import re
import time
from urllib.parse import quote

import httpx

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


def workday_jobs(row, max_pages=None):
    tenant, wd, site = row["identifier_1"], row["identifier_2"], row["identifier_3"]
    if not site:
        raise ValueError(f"{row['employer']}: Workday row has no site slug (identifier_3)")
    host = _workday_host(tenant, wd)
    url = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    public = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
    out, offset, limit, total, pages = [], 0, 20, None, 0  # CXS rejects limit > 20
    with client() as c:
        while True:
            body = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
            data = _request(c, "POST", url, json=body).json()
            postings = data.get("jobPostings") or []
            if total is None:
                # Only the first page reports a trustworthy total; Wells Fargo's tenant
                # returns total=0 on every later page (found 2026-09-14), so never
                # re-read it.
                total = data.get("total") or 0
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
                break
            if max_pages and pages >= max_pages:
                return Truncated(out)
            time.sleep(PAGE_DELAY)
    return out


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
    parts = [d.get("ExternalDescriptionStr"), d.get("ExternalResponsibilitiesStr"), d.get("ExternalQualificationsStr")]
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
        text = j.get("descriptionPlain") or N.html_to_text(j.get("description"))
        if j.get("additionalPlain"):
            text = f"{text}\n\n{j['additionalPlain']}" if text else j["additionalPlain"]
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


# ================================================================ dispatch
_LIST = {
    "workday": workday_jobs,
    "oracle_orc": oracle_orc_jobs,
    "greenhouse": greenhouse_jobs,
    "lever": lever_jobs,
    "ashby": ashby_jobs,
    "workable": workable_jobs,
    "bamboohr": bamboohr_jobs,
    "smartrecruiters": smartrecruiters_jobs,
}
_DETAIL = {
    "workday": workday_detail,
    "oracle_orc": oracle_orc_detail,
    "workable": workable_detail,
    "bamboohr": bamboohr_detail,
    "smartrecruiters": smartrecruiters_detail,
}
IMPLEMENTED_PLATFORMS = frozenset(_LIST)
DETAIL_PLATFORMS = frozenset(_DETAIL)


def list_jobs(row, max_pages=None):
    """Whole-board pull. Raises on hard failure so the caller never close-passes a
    board it didn't actually read. Returns a `Truncated` list if max_pages tripped."""
    fn = _LIST.get(row["platform"])
    if fn is None:
        raise ValueError(f"no adapter for platform {row['platform']!r}")
    return fn(row, max_pages=max_pages)


def fetch_detail(row, posting):
    """Per-posting detail for platforms whose list call is thin. Raises Gone when the
    posting has been taken down (which is itself useful: a free liveness check)."""
    fn = _DETAIL.get(row["platform"])
    if fn is None:
        raise ValueError(f"no detail adapter for platform {row['platform']!r}")
    return fn(row, posting)
