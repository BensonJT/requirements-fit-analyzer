import json, sys, re
from collections import Counter
rs=[json.loads(l) for f in sys.argv[1:] for l in open(f)]
A=Counter(); B=Counter(); exA={}; exB={}; pa=set(); pb=set()
for r in rs:
    prev=None
    for h in r["heads"]:
        k=h["kind"]
        if k=="cand": continue
        if k=="required" and prev=="preferred":
            A[h["h"]]+=1; pa.add(r["pid"]); exA.setdefault(h["h"],(r["employer"],r["pid"],h["sample"][:2],h.get("pref")))
        if k=="required" and prev=="responsibility" and h.get("duty",0)>=0.4 and h["n"]>=2:
            B[h["h"]]+=1; pb.add(r["pid"]); exB.setdefault(h["h"],(r["employer"],r["pid"],h["sample"][:2]))
        prev=k
print("A) Required heading right after Preferred:",len(pa),"postings")
for h,n in A.most_common(25): e=exA[h]; print(f"{n:4d} '{h}' {e[0]} {e[1]} | {e[2][:1]}")
print("\nB) Required heading after Responsibilities but its lines are duties:",len(pb),"postings")
for h,n in B.most_common(25): e=exB[h]; print(f"{n:4d} '{h}' {e[0]} {e[1]} | {e[2][:1]}")
