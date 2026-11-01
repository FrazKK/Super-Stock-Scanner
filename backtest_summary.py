"""Add up every saved replay (docs/data/backtest-*.json) and send one phone alert with the totals."""
import json
import os
from pathlib import Path

import screener as S

root = Path(__file__).resolve().parent / "docs" / "data"
rows = {}
for f in sorted(root.glob("backtest-2*.json")):
    try:
        d = json.loads(f.read_text())
    except Exception:
        continue
    if not str(d.get("date", "")).startswith("20"):
        continue                                   # e.g. a mistyped year like 2626
    mode = (d.get("strategy") or "older rotation run") + ("" if d.get("require_news", True) else ", no news") \
        + ("" if d.get("require_trend", True) else ", no trend rule") + (", 5-min bars" if d.get("bars") == "5m" else "")
    rows.setdefault(mode, {})
    trades = [t for t in d.get("trades", []) if t.get("pl_gbp") is not None]
    rows[mode][d["date"]] = {"total": round(d.get("total_gbp") or 0, 2), "trades": len(trades),
                             "wins": sum(t["pl_gbp"] > 0 for t in trades),
                             "list": [x["ticker"] for x in d.get("list", [])],
                             "added": len(d.get("added_during_day", []))}


def sumup(r):
    return {"days": len(r), "days_traded": sum(1 for x in r.values() if x["trades"]),
            "trades": sum(x["trades"] for x in r.values()), "wins": sum(x["wins"] for x in r.values()),
            "total_gbp": round(sum(x["total"] for x in r.values()), 2),
            "best_day": max(r.items(), key=lambda kv: kv[1]["total"], default=(None, None))[0],
            "worst_day": min(r.items(), key=lambda kv: kv[1]["total"], default=(None, None))[0]}


out = {"by_day": rows, "totals": {m: sumup(r) for m, r in rows.items() if r}}
(root / "backtest-summary.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
print(json.dumps(out["totals"], indent=1))
ran = [d.strip() for d in os.getenv("RAN", "").split(",") if d.strip()]
lines = []
for m, t in out["totals"].items():
    lines.append(f"{m}: {t['days']} days, {t['trades']} trades ({t['wins']} won), total "
                 f"{'+' if t['total_gbp'] >= 0 else '-'}£{abs(t['total_gbp']):,.0f}")
S.push("Replays finished", "\n".join(lines) or "No replay results found.", 3, "rewind")
