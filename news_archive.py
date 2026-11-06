"""
Keep a day-by-day archive of the news collector's releases, so replays of older days can read the same official
news the live bot sees (news.json itself only keeps the last 4 days).

  python news_archive.py --news out/news.json --dir archive

Writes <dir>/YYYY-MM-DD.json (New York date) for every complete day still inside news.json that isn't archived
yet, and deletes days older than KEEP_DAYS. Only finished days are written, so each day is saved once.
"""
import argparse
import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
KEEP_DAYS = 62          # 5-minute replays reach back ~60 days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--news", default="out/news.json")
    ap.add_argument("--dir", default="archive")
    a = ap.parse_args()
    d = json.load(open(a.news))
    items = d.get("items") or []
    if not items:
        print("no items")
        return
    os.makedirs(a.dir, exist_ok=True)
    today = dt.datetime.now(NY).date()
    oldest_item = min(dt.datetime.fromisoformat(i["time"].replace("Z", "+00:00")) for i in items)
    first_full = oldest_item.astimezone(NY).date() + dt.timedelta(days=1)   # the oldest day may be cut off
    by_day = {}
    for i in items:
        day = dt.datetime.fromisoformat(i["time"].replace("Z", "+00:00")).astimezone(NY).date()
        by_day.setdefault(day, []).append(i)
    wrote = 0
    for day, its in sorted(by_day.items()):
        f = os.path.join(a.dir, f"{day.isoformat()}.json")
        if day < first_full or day >= today or os.path.exists(f):
            continue
        with open(f, "w") as fh:
            json.dump({"date": day.isoformat(), "count": len(its), "items": its}, fh, indent=0)
        wrote += 1
        print(f"archived {day}: {len(its)} releases")
    cutoff = today - dt.timedelta(days=KEEP_DAYS)
    for name in os.listdir(a.dir):
        if name.endswith(".json") and name[:10] < cutoff.isoformat():
            os.remove(os.path.join(a.dir, name))
            print(f"removed {name}")
    print(f"{wrote} new day(s) archived")


if __name__ == "__main__":
    main()
