"""Title prefilter for the detail-fetch budget. NOT the screening engine.

Workday and Oracle boards hold ~60k postings between them and their list calls carry
no JD, so we can't fetch every JD every day. This regex decides which postings get a
detail request FIRST. Anything it misses still sits in `postings` with its title,
location and dates, and can be detail-fetched later (`--detail-all`, or a wider
pattern). Over-inclusion is cheap; tune freely.
"""
DETAIL_TITLE_PATTERN = (
    r"process|operational excellence|operations excellence|opex|continuous improvement|"
    r"business excellence|performance excellence|six sigma|lean|black belt|kaizen|"
    r"transformation|change management|change enablement|organizational change|"
    r"business analy|operations analy|operations manager|operations lead|"
    r"program manager|program director|portfolio|pmo|"
    r"data (engineer|analy|scien)|analytics|business intelligence|insights|reporting|"
    r"service delivery|service management|quality (manager|lead|director|assurance)|"
    r"strategy|planning|governance|workforce|productivity|efficiency|automation|"
    r"director|principal|senior manager|sr\.? manager"
)
