"""Quick check (standard library only) so the pre-close workflow can skip setup when nothing is due."""
import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

CLOSE = {"uk": ("Europe/London", 16, 30), "america": ("America/New_York", 16, 0), "canada": ("America/New_York", 16, 0)}
root = Path(__file__).resolve().parent
markets = set()
for f in (root / "holdings").glob("*.json"):
    try:
        markets.add(str(json.loads(f.read_text()).get("market", "america")).lower())
    except Exception:
        pass
try:
    state = json.loads((root / "docs" / "data" / "preclose_state.json").read_text())
except Exception:
    state = {}
now = dt.datetime.now(dt.timezone.utc)
due = []
for m in sorted(markets & set(CLOSE)):
    tz, hh, mm = CLOSE[m]
    local = now.astimezone(ZoneInfo(tz))
    if local.weekday() >= 5:
        continue
    mins = (local.replace(hour=hh, minute=mm, second=0, microsecond=0) - local).total_seconds() / 60
    if 10 <= mins <= 80 and state.get(m) != local.date().isoformat():
        due.append(m)
manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
result = "true" if (due or (manual and markets)) else "false"
print(f"due markets: {due or 'none'}; run={result}")
if os.getenv("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
        fh.write(f"due={result}\n")
