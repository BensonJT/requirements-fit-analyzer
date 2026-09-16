#!/usr/bin/env bash
# Where did the two-lens re-grade get to? The *.result.json files are the only source of truth:
# a batch a terminal died partway through looks exactly like one nobody started, and is simply re-graded.
cd "$(dirname "$0")"
ls db/batches_2lens/*.result.json 2>/dev/null |
  sed 's|.*batch_0*\([0-9]*\)\.result\.json|\1|' | sort -n > /tmp/.done_batches
awk -v total=118 '
  FNR==NR { done[$1]=1; n++; next }
' /tmp/.done_batches /dev/null
python3 - <<'PY'
done = {int(l) for l in open('/tmp/.done_batches') if l.strip()}
missing = [b for b in range(1, 119) if b not in done]
up = max((b for b in range(1, 119) if all(x in done for x in range(1, b + 1))), default=0)
dn = min((b for b in range(118, 0, -1) if all(x in done for x in range(b, 119))), default=119)
print(f"done {len(done)} / 118 batches  (~{len(done) * 25} of 2946 postings)")
print(f"Terminal A has finished everything up to batch {up}")
print(f"Terminal B has finished everything down to batch {dn if dn <= 118 else '-'}")
print(f"remaining {len(missing)} batches ~ {len(missing) * 78 / 1000:.1f}M tokens")
print("not yet graded:", ", ".join(map(str, missing)) if missing else "none - run judge import")
PY
