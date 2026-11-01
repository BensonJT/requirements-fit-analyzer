import json, sys, statistics as st
from collections import defaultdict, Counter
for fn in sys.argv[1:]:
    rs=[json.loads(l) for l in open(fn)]
    n=len(rs)
    def pct(f): return f"{100*sum(1 for r in rs if f(r))/n:.1f}%"
    print(f"\n===== {fn}: {n} postings")
    print("req>30:", pct(lambda r:r['n_req']>30), "| req==0:", pct(lambda r:r['n_req']==0),
          "| resp==0 & req>0:", pct(lambda r:r['n_resp']==0 and r['n_req']>0),
          "| req duty-share>=40% (>=5 lines):", pct(lambda r:r['n_req']>=5 and r['req_prof'].get('duty',0)>=0.4),
          "| req has noise line:", pct(lambda r:r['req_prof'].get('noise',0)>0),
          "| req has pref-cue line:", pct(lambda r:r['req_prof'].get('pref',0)>0),
          "| req lines led by Preferred/Bonus:", pct(lambda r:r['pref_lead']>0),
          "| no recognized req/pref heading:", pct(lambda r: not any(h['kind'] in('required','preferred') for h in r['heads'])))
    print("median req lines:", st.median(r['n_req'] for r in rs), " p90:", sorted(r['n_req'] for r in rs)[int(n*.9)])
    byplat=defaultdict(list)
    for r in rs: byplat[r['platform']].append(r)
    print("by platform (n, req>30, req==0, duty-heavy req):")
    for p,v in sorted(byplat.items(), key=lambda x:-len(x[1]))[:10]:
        m=len(v); print(f"  {p:20s} {m:5d}  {100*sum(r['n_req']>30 for r in v)/m:5.1f}%  {100*sum(r['n_req']==0 for r in v)/m:5.1f}%  {100*sum(r['n_req']>=5 and r['req_prof'].get('duty',0)>=0.4 for r in v)/m:5.1f}%")
