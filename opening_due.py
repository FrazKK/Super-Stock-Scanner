"""Quick check (standard library only): is an opening-surge check due for any market right now?"""
import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

OPEN = {"uk": ("Europe/London", 8, 0), "america": ("America/New_York", 9, 30), "canada": ("America/New_York", 9, 30)}
root = Path(__file__).resolve().parent
try:
    state = json.loads((root / "docs" / "data" / "opening_state.json").read_text())
except Exception:
    state = {}
now = dt.datetime.now(dt.timezone.utc)
due = []
for m, (tz, hh, mm) in OPEN.items():
    local = now.astimezone(ZoneInfo(tz))
    if local.weekday() >= 5:
        continue
    mins = (local - local.replace(hour=hh, minute=mm, second=0, microsecond=0)).total_seconds() / 60
    if 25 <= mins <= 80 and state.get(m) != local.date().isoformat():
        due.append(m)
forced = os.getenv("MARKETS", "").strip()
result = "true" if (due or forced) else "false"
print(f"due markets: {due or 'none'}; forced: {forced or 'no'}; run={result}")
if os.getenv("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
        fh.write(f"due={result}\n")
