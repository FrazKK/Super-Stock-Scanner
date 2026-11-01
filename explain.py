"""
Why didn't the day trader pick these stocks? Checks each rule for given days and tickers.

  python explain.py "2026-10-29:STRL,BURU; 2026-10-30:RIVN"

For each stock it reports: in the bot's universe (market cap, liquidity), first-30-minute volume vs normal,
the strongest 30-minute burst later in the day (and whether it would have been picked up by the 15-minute
checks), trend, up on the day, news found and how it was read, and Trading 212 availability.
Saves docs/data/explain-<date>.json and sends a phone alert with a one-line verdict per stock.
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import practice as P
import screener as S
from backtest import download, parse_day

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent


def parse_items(text):
    out = []
    for part in re.split(r"[;\n]+", text):
        if ":" not in part:
            continue
        d, ticks = part.split(":", 1)
        day = parse_day(d)
        out += [(day, t.strip().upper()) for t in re.split(r"[,\s]+", ticks) if t.strip()]
    return out


def check(day, tick, tv, t212):
    cfg = S.MARKETS["america"]
    r = {"date": day.isoformat(), "ticker": tick, "fails": [], "notes": []}
    row = tv.get(tick)
    if row is None:
        r["fails"].append("not found in TradingView's US stock list")
        return r
    mcap, close = S.num(row.get("market_cap_basic")), S.num(row.get("close"))
    avgv = S.num(row.get("average_volume_10d_calc")) or 0
    r.update(name=str(row.get("description") or tick), exchange=str(row.get("exchange")), mcap=mcap)
    if mcap and not S.OPEN_ANY_SIZE and not (cfg["mcap"][0] <= mcap <= cfg["mcap"][1]):
        r["fails"].append(f"market cap ${mcap / 1e9:.1f}bn is outside the bot's ${cfg['mcap'][0] / 1e6:.0f}m–${cfg['mcap'][1] / 1e9:.1f}bn range")
    if str(row.get("exchange")).upper() not in cfg["exchanges"]:
        r["fails"].append(f"listed on {row.get('exchange')}, not a main US exchange")
    if S.is_excluded(row):
        r["fails"].append("excluded type (fund, SPAC, preferred, etc.)")
    if close and avgv * close < cfg["min_avg_value"]:
        r["fails"].append(f"too thinly traded (about ${avgv * close / 1e3:.0f}k a day)")
    if t212 and tick not in t212:
        r["fails"].append("not available on Trading 212")
    ys = S.yahoo_symbol(row.get("exchange"), tick)
    daily = download([ys], start=(day - dt.timedelta(days=150)).isoformat(),
                     end=(day + dt.timedelta(days=1)).isoformat(), interval="1d").get(ys)
    if daily is None or not len(daily):
        r["fails"].append("no daily price data from Yahoo")
        return r
    idx = [x.date() for x in daily.index]
    prior = daily[[x < day for x in idx]]
    today = daily[[x == day for x in idx]]
    if len(prior) < 50 or today.empty:
        r["fails"].append("not enough price history")
        return r
    avg = float(prior["Volume"].iloc[-10:].mean())
    prev = float(prior["Close"].iloc[-1])
    closes = prior["Close"].astype(float).tolist()
    r.update(prev_close=prev, day_open=float(today["Open"].iloc[0]), day_close=float(today["Close"].iloc[0]),
             day_high=float(today["High"].iloc[0]), day_change=float(today["Close"].iloc[0]) / prev - 1,
             day_volume_x=round(float(today["Volume"].iloc[0]) / avg, 2) if avg else None)
    mins = download([ys], start=day.isoformat(), end=(day + dt.timedelta(days=1)).isoformat(), interval="1m",
                    prepost=False).get(ys)
    if mins is None or not len(mins):
        r["fails"].append("no 1-minute data from Yahoo")
        return r
    b = mins.copy()
    b.index = b.index.tz_convert(NY) if b.index.tz is not None else b.index.tz_localize("UTC").tz_convert(NY)
    b = b[[x.date() == day for x in b.index]]
    open_t = dt.datetime.combine(day, dt.time(9, 30), NY)

    def trend_at(price):
        s20 = (sum(closes[-19:]) + price) / 20
        s50 = (sum(closes[-49:]) + price) / 50
        return price > s20 and price > s50 and s20 >= 0.97 * s50

    first = b[(b.index >= open_t) & (b.index < open_t + dt.timedelta(minutes=30))]
    p10 = float(first["Close"].iloc[-1]) if len(first) else None
    vx30 = float(first["Volume"].sum()) / avg if avg and len(first) else 0
    r.update(first30_volume_x=round(vx30, 2), price_10am=p10, up_at_10am=bool(p10 and p10 > prev),
             trend_at_10am=bool(p10 and trend_at(p10)))
    # every 15-minute check the bot would have made (10:15 to 15:30 New York)
    best, first_hit = None, None
    t = open_t + dt.timedelta(minutes=45)
    while t <= open_t + dt.timedelta(hours=6):
        tu = t.astimezone(dt.timezone.utc)
        upto = b[b.index <= t - dt.timedelta(minutes=1)]
        win = upto[upto.index >= t - dt.timedelta(minutes=30)]
        if len(win):
            vx = float(win["Volume"].sum()) / avg if avg else 0
            last = float(upto["Close"].iloc[-1])
            if best is None or vx > best[1]:
                best = (t.strftime("%H:%M"), round(vx, 2), last)
            sc = P.surge_check(upto, avg, tu, prev)
            if sc and trend_at(last) and first_hit is None:
                first_hit = t.strftime("%H:%M")
        t += dt.timedelta(minutes=15)
    r.update(best_30min_burst=best and {"time": best[0], "volume_x": best[1], "price": best[2]},
             would_pass_volume_trend_at=first_hit)
    since = dt.datetime.combine(day - dt.timedelta(days=S.OPEN_NEWS_DAYS + (2 if day.weekday() <= 1 else 0)),
                                dt.time(0), dt.timezone.utc)
    news = [n for n in S.live_news("america", tick, ys, since, r["name"]) if n["date"] <= day.isoformat()]
    tones = {n["tone"] for n in news}
    r["news"] = [{"title": n["title"][:140], "date": n["date"], "tone": n["tone"], "source": n["source"]} for n in news]
    ok_open = vx30 >= S.OPEN_VOL_MULT and r["up_at_10am"] and r["trend_at_10am"]
    if not ok_open:
        why = []
        if vx30 < S.OPEN_VOL_MULT:
            why.append(f"first-30-min volume {vx30:.2f}x normal (needs {S.OPEN_VOL_MULT:.2f}x)")
        if not r["up_at_10am"]:
            why.append("not up on the day at 10:00")
        if not r["trend_at_10am"]:
            why.append("not in an uptrend (price vs 20/50-day averages)")
        r["notes"].append("Not on the 10:15 list: " + "; ".join(why) + ".")
    if first_hit:
        r["notes"].append(f"Would have passed the volume + trend check at {first_hit} New York.")
    elif not ok_open:
        bb = r["best_30min_burst"]
        r["notes"].append(f"Never passed the volume + trend check during the day (strongest 30-min burst "
                          f"{bb['volume_x']:.2f}x normal at {bb['time']})." if bb else "No intraday data.")
    if "deal" in tones:
        r["fails"].append("takeover news, which is always excluded")
    if "neg" in tones:
        r["fails"].append("negative news (placing, warning, etc.), which is always excluded")
    if not news:
        r["notes"].append("No news found in the 3 days before (OK only with 'news not required').")
    elif "pos" not in tones:
        r["notes"].append("News found but none read as positive: " + " | ".join(n["title"][:70] for n in news[:2]))
    return r


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else os.getenv("ITEMS", "")
    items = parse_items(text)
    if not items:
        raise SystemExit("Nothing to check. Use e.g. 2026-10-29:STRL,BURU; 2026-10-30:RIVN")
    ticks = sorted({t for _, t in items})
    tv_df = S.live_tv("america", ticks)
    tv = {str(r["name"]): r for r in tv_df.to_dict("records")}
    t212 = set((S.load_json(ROOT / "docs" / "data" / "t212_tickers.json", {}) or {}).get("us") or [])
    out = []
    for day, tick in items:
        try:
            out.append(check(day, tick, tv, t212))
        except Exception as e:
            out.append({"date": day.isoformat(), "ticker": tick, "fails": [f"check failed: {e}"], "notes": []})
        print(json.dumps(out[-1], indent=1, default=S.jdefault))
    f = ROOT / "docs" / "data" / f"explain-{dt.date.today().isoformat()}.json"
    f.write_text(json.dumps(out, indent=1, default=S.jdefault), encoding="utf-8")
    lines = [f"{r['date'][5:]} {r['ticker']}: " + ("; ".join(r["fails"] + r["notes"])[:200] or "passes everything")
             for r in out]
    S.push("Why the bot didn't pick them", "\n".join(lines)[:3800], 3, "mag")


if __name__ == "__main__":
    main()
