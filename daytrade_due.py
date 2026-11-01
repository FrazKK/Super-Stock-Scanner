"""Quick check (standard library only): should the US day trader be running now?"""
import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

root = Path(__file__).resolve().parent
now = dt.datetime.now(ZoneInfo("America/New_York"))
today = now.date().isoformat()


def load(name):
    try:
        return json.loads((root / "docs" / "data" / name).read_text())
    except Exception:
        return {}


opening = (load("opening.json").get("markets") or {}).get("america") or {}
day = load("practice.json").get("day") or {}
open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
in_hours = now.weekday() < 5 and open_t + dt.timedelta(minutes=30) <= now <= close_t - dt.timedelta(minutes=12)
listed = opening.get("date") == today
finished = day.get("date") == today and day.get("finished")
due = in_hours and not finished          # the opening list helps but isn't needed (the bot searches every 15 min)
print(f"in hours={in_hours}, today's US list={listed}, finished={finished}, run={due}")
if os.getenv("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
        fh.write(f"due={'true' if due else 'false'}\n")
