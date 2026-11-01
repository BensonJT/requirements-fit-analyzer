import sys, os, re
sys.path.insert(0, os.environ.get("JOBSEARCH_ROOT") or os.path.expanduser("~/jobsearch"))
import duckdb
from backend.finder import requirements as R, judge2
from backend.finder.labels import strip_boilerplate
con=duckdb.connect(os.path.expanduser("~/jobsearch/db/jobsearch.duckdb"), read_only=True)
maxl=int(os.environ.get("MAXL","70"))
for pid in sys.argv[1:]:
    e,t,jd=con.execute("select employer,title,description_text from postings where posting_id=?",[pid]).fetchone()
    lines=R.rejoin_lines(strip_boilerplate(R._unwrap_paragraphs(jd)))
    secs={}
    for u in R.split_requirements(jd): secs.setdefault((u.source or u.text)[:60], u.section)
    print(f"\n######## {pid} {e} | {t}")
    for l in lines[:maxl]:
        if not l: continue
        k=R._heading_kind(l); sub=R._subheading(l)
        tag = f"[H:{k}]" if k else ("[sub]" if sub else "      ")
        sec = next((s for key,s in secs.items() if l.startswith(key[:40]) or key[:40] in l), "")
        print(f"{tag:18s}{sec[:5]:6s}{l[:130]}")
