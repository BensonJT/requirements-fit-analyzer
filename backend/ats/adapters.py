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
import json
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
        if not positions or start >= total:
            return out, total, False
        if max_pages and pages >= max_pages:
            return out, total, True
        time.sleep(PAGE_DELAY)


def eightfold_jobs(row, max_pages=None):
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
