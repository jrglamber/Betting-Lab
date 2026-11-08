"""Offline CLV-first audit using an existing weekly export ZIP.

Usage: python scripts/audit_clv_basketball.py path/to/research-weekly.zip
Read-only. Does not access production or make betting recommendations.
"""
import csv
import io
import json
import sys
import zipfile
from pathlib import Path

# Permit running this file directly from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clv_basketball_replay import replay
from clv_hr2_shadow_research import analyze


def load(z, name):
    with z.open("tables/" + name + ".csv") as source:
        return list(csv.DictReader(io.TextIOWrapper(source, encoding="utf-8-sig")))


def main(path):
    with zipfile.ZipFile(path) as z:
        clv = load(z, "clv_first_samples")
        hr = load(z, "hit_rate_candidates")
    for row in clv:
        if row.get("pnl_units") in ("", None):
            row["pnl_units"] = None
    result = {
        "source": Path(path).name,
        "overlap": analyze(clv, [r for r in hr if r.get("experiment_key") == "HR2"]),
        "basketball_accept": replay(clv, "ACCEPT"),
        "basketball_observe": replay(clv, "OBSERVE"),
    }
    # Avoid dumping thousands of individual bet records to routine CI logs.
    for name in ("basketball_accept", "basketball_observe"):
        for outcome in result[name]["results"].values():
            outcome.pop("cards_detail", None)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/audit_clv_basketball.py export.zip")
    main(sys.argv[1])
