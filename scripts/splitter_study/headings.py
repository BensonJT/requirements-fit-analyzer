import json, sys
from collections import defaultdict
rs=[json.loads(l) for f in sys.argv[1:] for l in open(f)]
agg=defaultdict(lambda: {"posts":set(),"emps":set(),"kind":None,"q":0,"d":0,"p":0,"z":0,"c":0,"k":0,"raw":None})
for r in rs:
    for h in r["heads"]:
        if h.get("n",0)==0: continue
        a=agg[(h["h"],h["kind"])]; a["posts"].add(r["pid"]); a["emps"].add(r["employer"]); a["raw"]=a["raw"] or h["raw"]
        a["q"]+=h["qual"]; a["d"]+=h["duty"]; a["p"]+=h["pref"]; a["z"]+=h["noise"]; a["c"]+=h["cert"]; a["k"]+=1
def show(title, filt, key, top=40):
    print(f"\n### {title}")
    rows=[(k,a) for k,a in agg.items() if filt(k,a)]
    rows.sort(key=key)
    for (h,kind),a in rows[:top]:
        k=a["k"]; print(f"{len(a['posts']):5d}p {len(a['emps']):3d}e  {kind:14s} q{a['q']/k:.2f} d{a['d']/k:.2f} p{a['p']/k:.2f} z{a['z']/k:.2f} c{a['c']/k:.2f} | {h[:70]}")
MIN=int(os.environ.get("MIN","3")) if (os:=__import__("os")) else 3
# unrecognized heading candidates whose content is qualification-like -> missing REQUIRED/PREFERRED vocab
show("UNRECOGNIZED, qualification-like content", lambda k,a:k[1]=="cand" and len(a["emps"])>=2 and a["q"]/a["k"]>=0.5 and a["d"]/a["k"]<0.35, lambda x:-len(x[1]["posts"]))
show("UNRECOGNIZED, duty-like content", lambda k,a:k[1]=="cand" and len(a["emps"])>=2 and a["d"]/a["k"]>=0.4, lambda x:-len(x[1]["posts"]))
show("UNRECOGNIZED, preferred-ish content", lambda k,a:k[1]=="cand" and len(a["emps"])>=1 and (a["p"]/a["k"]>=0.3 or a["c"]/a["k"]>=0.4), lambda x:-len(x[1]["posts"]),25)
show("UNRECOGNIZED, noise content", lambda k,a:k[1]=="cand" and len(a["emps"])>=2 and a["z"]/a["k"]>=0.3, lambda x:-len(x[1]["posts"]),25)
show("REQUIRED heading but duty-heavy content", lambda k,a:k[1]=="required" and len(a["posts"])>=3 and a["d"]/a["k"]>=0.35, lambda x:-len(x[1]["posts"]),30)
show("REQUIRED heading but preferred/cert-heavy", lambda k,a:k[1]=="required" and len(a["posts"])>=2 and (a["p"]/a["k"]>=0.3 or a["c"]/a["k"]>=0.35), lambda x:-len(x[1]["posts"]),25)
show("RESPONSIBILITY heading but qualification-heavy", lambda k,a:k[1]=="responsibility" and len(a["posts"])>=3 and a["q"]/a["k"]>=0.6 and a["d"]/a["k"]<0.25, lambda x:-len(x[1]["posts"]),25)
show("PREFERRED heading but looks required (low pref cue, high qual)", lambda k,a:k[1]=="preferred" and len(a["posts"])>=5, lambda x:-len(x[1]["posts"]),25)
