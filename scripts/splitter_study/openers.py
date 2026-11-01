import json, sys, re
from collections import Counter, defaultdict
rs=[json.loads(l) for f in sys.argv[1:] for l in open(f)]
OPEN={"job description":r"^job description$|^description$|^position overview$|^role overview$|^overview$|^about the (?:role|position|job|opportunity)$|^the position$",
 "duties (missing)":r"^work you.ll do$|essential job functions|^job functions$|what you.ll work on|how you.ll make a difference|successful candidate will|^primary duties|^major duties|^key duties|^core duties|what you.ll own|^the work$|^your day$|^your mission$|^mission$|^the job$|^main tasks$|^tasks$|^what the role involves",
 "required (missing)":r"^minimum requirement$|^qualification$|^qualification requirements$|^mandatory|^essential criteria$|^essential qualifications?$|^key qualifications$|^required clearance$|^languages?$|^licens|^certifications?$|^licen[cs]es? (?:and|&|/) certifications?$|^clearance$",
 "preferred (missing)":r"stand out|we.ll prioritize|^preferences$|^extra credit|^even better|^a bonus if|^it would be great if|^would be great|^ideally|^plus$|^pluses$|^optional|^differentiators$|^not required but",
 "team/company (should drop)":r"^the team$|^our team$|^team overview$|^company overview$|^company profile$|^what we believe$|^our culture$|^how we work$|^department description$|^about this team$|^meet the team$|^unleash your potential$|^employee referral|^parental leave$|^paid time off$|^work shift$|^sponsorship$|^the details$|^pay$|^keywords$|^li-[a-z0-9]+$|^\d{4,6}\s+\d{4,6}\s+usd$"}
prevsec=defaultdict(Counter); cnt=Counter(); posts=defaultdict(set); lines=Counter(); ex={}
for r in rs:
    sec="intro"
    for h in r["heads"]:
        if h["kind"] in("required","preferred","responsibility","drop"): sec=h["kind"]; continue
        for name,p in OPEN.items():
            if re.search(p,h["h"]):
                cnt[name]+=1; posts[name].add(r["pid"]); prevsec[name][sec]+=1; lines[name]+=h["n"]; ex.setdefault((name,h["h"]),0); ex[(name,h["h"])]+=1
for name in OPEN:
    top=sorted(((n,h) for (k,h),n in ex.items() if k==name), reverse=True)[:8]
    print(f"{name:28s} {len(posts[name]):5d} postings, ~{lines[name]} lines; sits under: {dict(prevsec[name].most_common())}")
    print("      ", "; ".join(f"{h}({n})" for n,h in top))
