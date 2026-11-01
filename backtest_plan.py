"""Turn the replay form's dates into a list of jobs for GitHub to run side by side (standard library only).

Accepts one date, a list ("2026-10-27, 2026-10-28") or a range ("2026-10-05 to 2026-10-30"), in either
2026-10-29 or 29-10-2026 style. Weekends are skipped, and so are days older than Yahoo's ~30-day limit
for 1-minute prices. Blank means the last 10 weekdays.
"""
import datetime as dt
import json
import os
import re
from zoneinfo import ZoneInfo

today = dt.datetime.now(ZoneInfo("America/New_York")).date()
oldest = today - dt.timedelta(days=29)


def parse(t):
    t = t.strip().replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y"):
        try:
            return dt.datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    raise SystemExit(f"Couldn't read the date '{t}'. Use e.g. 2026-10-29 or 29-10-2026.")


text = os.getenv("DATES", "").strip()
days = []
if not text:
    d = today - dt.timedelta(days=1)
    while len(days) < 10:
        if d.weekday() < 5:
            days.append(d)
        d -= dt.timedelta(days=1)
else:
    for part in re.split(r"[,;\n]+", text):
        part = part.strip()
        if not part:
            continue
        rng = re.split(r"\s+(?:to|-)\s+|\s*\.\.\s*", part)
        if len(rng) == 2:
            a, b = sorted((parse(rng[0]), parse(rng[1])))
            d = a
            while d <= b:
                days.append(d)
                d += dt.timedelta(days=1)
        else:
            days.append(parse(part))
days = sorted({d for d in days if d.weekday() < 5 and oldest <= d < today})
skipped = text and not days
news = os.getenv("NEWS", "required")
modes = ["required", "not required"] if news == "both" else [news]
jobs = [{"day": d.isoformat(), "news": m} for d in days for m in modes]
print(f"{len(days)} weekday(s): {', '.join(d.isoformat() for d in days)}; news: {', '.join(modes)}; {len(jobs)} job(s)")
if skipped:
    print(f"No usable weekdays. Only the last ~30 days can be replayed (from {oldest}).")
if os.getenv("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
        fh.write(f"matrix={json.dumps({'include': jobs})}\n")
        fh.write(f"count={len(jobs)}\n")
