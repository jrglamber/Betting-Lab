"""Read-only NCAAF price-quality audit. Usage: python tools/ncaaf_price_audit.py research.zip"""
import csv
import io
import json
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timezone

def dt(s):
    try: return datetime.fromisoformat(str(s).replace("Z","+00:00"))
    except (ValueError, TypeError): return None

def number(v):
    try: return float(v)
    except (ValueError, TypeError): return None

def summarize(rows):
    n=len(rows)
    settled=[r for r in rows if r.get("status")=="SETTLED"]
    def mean(field):
        vals=[number(r.get(field)) for r in rows]
        vals=[x for x in vals if x is not None]
        return round(sum(vals)/len(vals),3) if vals else None
    def median(field):
        vals=sorted(x for r in rows if (x:=number(r.get(field))) is not None)
        return vals[len(vals)//2] if vals else None
    return dict(count=n,settled=len(settled),wins=sum(r.get("result")=="WIN" for r in settled),
                net_units=round(sum(number(r.get("net_pnl_units")) or 0 for r in settled),3),
                roi_pct=round(100*sum(number(r.get("net_pnl_units")) or 0 for r in settled)/len(settled),2) if settled else None,
                same_book_clv_mean=mean("clv_pct"),consensus_clv_mean=mean("clv_vs_consensus_pct"),
                entry_consensus_age_median=median("consensus_age_minutes"),
                closing_minutes_before_start_median=median("closing_minutes_before_start"),
                stale_closes=sum(r.get("clv_quality")=="STALE" for r in rows),
                high_quality_closes=sum(r.get("clv_quality") in ("A","B") for r in rows))

def audit(path):
    with zipfile.ZipFile(path) as z:
        with z.open("tables/multisport_execution_bets.csv") as f:
            rows=[r for r in csv.DictReader(io.TextIOWrapper(f,encoding="utf-8-sig"))
                  if r.get("sport_key")=="americanfootball_ncaaf"
                  and 3 <= (number(r.get("offered_odds")) or 0) < 5]
    unique={}
    for r in rows:
        key=r.get("execution_key") or r.get("id")
        if key in unique: raise ValueError("Duplicate execution key: "+str(key))
        unique[key]=r
    groups=defaultdict(list)
    for r in rows:
        created=dt(r.get("created_at"))
        groups["all"].append(r)
        groups["before_2026_10_19" if created and created.date().isoformat()<"2026-10-19" else "from_2026_10_19"].append(r)
        groups["book:"+str(r.get("bookmaker_key"))].append(r)
        groups["quality:"+str(r.get("clv_quality"))].append(r)
        if created: groups["week:"+created.date().isoformat()].append(r)
    return {k:summarize(v) for k,v in sorted(groups.items())}

if __name__=="__main__":
    if len(sys.argv)!=2: sys.exit(__doc__)
    print(json.dumps(audit(sys.argv[1]),indent=2))
