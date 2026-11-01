import json, sys, re
from collections import Counter
rs=[json.loads(l) for f in sys.argv[1:] for l in open(f)]
PREF=re.compile(r"\b(preferred|a plus|nice to have|bonus|desired|desirable|ideally|is an asset|advantageous|highly valued|not required|good to have)\b",re.I)
pat=Counter(); ex={}
for r in rs:
    for l in r["req_sample"]:
        m=PREF.search(l)
        if not m: continue
        w=m.group(1).lower(); tail=l[m.end():].strip()
        if w=="preferred" and re.search(r"\brequired\b",l,re.I): k="mixed: X required ... Y preferred (one line)"
        elif w in("ideally",): k="ideally ..."
        elif w=="not required": k="... not required"
        elif re.match(r"^\W*(preferred|desired|nice to have|bonus|plus)\b",l,re.I): k="leading 'Preferred:'/'Bonus:' label"
        elif re.search(r"\b(or|and)\b[^.]{0,80}\bpreferred\b",l,re.I) and len(tail)>3: k="mid-line 'X preferred' then more text"
        elif len(tail)<=3: k="trailing status (should already be caught)"
        else: k=f"other: {w}"
        pat[k]+=1; ex.setdefault(k,l[:150])
tot=sum(pat.values()); print("Required lines with a preferred cue:",tot,"in",sum(1 for r in rs if any(PREF.search(l) for l in r['req_sample'])),"postings")
for k,n in pat.most_common(): print(f"{n:5d}  {k:52s} e.g. {ex[k]}")
# any-of lists
ANY=re.compile(r"\b(one of the following|any of the following|at least one of|one or more of the following|any one of)\b",re.I)
n_any=sum(1 for r in rs if any(ANY.search(h['raw']) for h in r['heads']) or any(ANY.search(l) for l in r['req_sample']))
print("\npostings with an any-of list ('one of the following' etc.):",n_any)
# bonus/benefit headings read as preferred
bon=Counter(h['h'] for r in rs for h in r['heads'] if h['kind']=='preferred' and re.search(r'bonus|incentive|award|eligib',h['h']) and not re.search(r'bonus points|bonus if|is a bonus|^bonus$|bonus skills|bonus qualif',h['h']))
print("benefit headings opened as Preferred:",sum(bon.values()),bon.most_common(8))
