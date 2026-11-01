"""
Replay one past US trading day with the day-trading rules in practice.py, using Yahoo's 1-minute bars.

  python backtest.py --date 2026-10-29

It rebuilds that day's opening-surge list with the current rule (a third of a normal day's volume in the first 30
minutes, trending up, up on the day, positive news in the 3 days before), then runs the exact same trading
code as the live bot against a pretend account fed by the minute bars, including its search for new movers
every 15 minutes. Add --no-news to let stocks without news qualify (saved as backtest-<date>-nonews.json). The result is saved to
docs/data/backtest-<date>.json and sent as a phone alert. Nothing touches Trading 212.

Approximations: today's list of US companies is used (not that day's), news comes from feeds that may have
dropped older items, fills are at the last 1-minute close plus 0.1% spread and Trading 212's 0.15% currency fee each way,
and stop-loss orders fill at the stop price (or the next price if it gapped through).
"""
import argparse
import datetime as dt
import itertools
import json
import os
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import practice as P
import screener as S

NY = ZoneInfo("America/New_York")
SPREAD = 0.001                 # 0.1% each way (half the bid/offer spread on a liquid small cap)
FX_FEE = 0.0015                # Trading 212's currency conversion fee, charged on buys and sells
BAR = {"interval": "1m", "minutes": 1}   # 1-minute bars (last ~30 days) or 5-minute bars (last ~60 days)
ROOT = Path(__file__).resolve().parent


def last_weekday(before):
    d = before - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


SPY_BARS = None


def result_suffix(a):
    """File name ending for a replay: strategy, then -nonews and -5m when used."""
    return f"-{a.strategy}" + ("" if not a.no_news else "-nonews") + ("-5m" if a.bars == "5m" else "")


def parse_day(text):
    """Accepts 2026-10-29 as well as UK style 29-10-2026, 29/10/2026 or 29/10/26."""
    t = text.strip().replace("/", "-").replace(".", "-")
    try:
        return dt.date.fromisoformat(t)
    except ValueError:
        for fmt in ("%d-%m-%Y", "%d-%m-%y"):
            try:
                return dt.datetime.strptime(t, fmt).date()
            except ValueError:
                pass
    raise SystemExit(f"Couldn't read the date '{text}'. Use e.g. 29-10-2026 or 2026-10-29.")


def download(syms, **kw):
    import yfinance as yf

    out = {}
    syms = list(dict.fromkeys(syms))
    step = 100 if kw.get("interval") == "1d" else 50
    for i in range(0, len(syms), step):
        chunk = syms[i:i + step]
        try:
            raw = yf.download(chunk, group_by="ticker", auto_adjust=False, progress=False, threads=True, **kw)
        except Exception as e:
            print(f"  download failed: {e}")
            continue
        if raw is None or raw.empty:
            continue
        for s in chunk:
            try:
                d = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
                if len(d):
                    out[s] = d
            except Exception:
                continue
        time.sleep(1)
    print(f"  {kw.get('interval')} bars for {len(out)} of {len(syms)}")
    return out


def build_list(day):
    global SPY_BARS
    """Rebuild that day's US opening-surge list (as the 10:15 check would have seen it, with data to 10:00)."""
    uni = S.prepare_universe(S.live_tv("america", any_size=S.OPEN_ANY_SIZE), "america")
    meta = {r["ysym"]: r for r in uni.to_dict("records")}
    daily = download(list(meta), start=(day - dt.timedelta(days=150)).isoformat(),
                     end=(day + dt.timedelta(days=1)).isoformat(), interval="1d")
    pre, info = [], {}
    for ys, d in daily.items():
        idx = [ix.date() for ix in d.index]
        prior = d[[x < day for x in idx]]
        today = d[[x == day for x in idx]]
        if len(prior) < 50 or today.empty:
            continue
        avg = float(prior["Volume"].iloc[-10:].mean())
        prev = float(prior["Close"].iloc[-1])
        if avg <= 0 or float(today["Volume"].iloc[0]) < S.OPEN_VOL_MULT * avg or float(today["High"].iloc[0]) <= prev:
            continue
        rng = float(((prior["High"] - prior["Low"]) / prior["Close"]).iloc[-14:].mean())
        info[ys] = {"avg": avg, "prev": prev, "closes": prior["Close"].astype(float).tolist(), "range": rng}
        pre.append(ys)
    print(f"  {len(pre)} stocks had enough volume and traded above the previous close that day")
    mins = download(pre + ["SPY"], start=day.isoformat(), end=(day + dt.timedelta(days=1)).isoformat(),
                    interval=BAR["interval"], prepost=False)
    open_t = dt.datetime.combine(day, dt.time(9, 30), NY)
    t30 = open_t + dt.timedelta(minutes=30)
    for ys in list(mins):
        b = mins[ys].copy()
        b.index = b.index.tz_convert(NY) if b.index.tz is not None else b.index.tz_localize("UTC").tz_convert(NY)
        mins[ys] = b[[x.date() == day for x in b.index]]
    SPY_BARS = mins.pop("SPY", None)
    cands = []
    for ys, b in mins.items():
        first = b[(b.index >= open_t) & (b.index < t30)]
        if first.empty:
            continue
        i = info[ys]
        vol30 = float(first["Volume"].sum())
        price = float(first["Close"].iloc[-1])
        vx = vol30 / i["avg"]
        sma20 = (sum(i["closes"][-19:]) + price) / 20
        sma50 = (sum(i["closes"][-49:]) + price) / 50
        trend = price > sma20 and price > sma50 and sma20 >= 0.97 * sma50
        if vx < S.OPEN_VOL_MULT or not trend or price <= i["prev"] or vol30 * price < S.OPEN_MIN_VALUE["america"]:
            continue
        flow = S.flow_split(first, open_t.astimezone(dt.timezone.utc), t30.astimezone(dt.timezone.utc))
        hi, lo, op = float(first["High"].max()), float(first["Low"].min()), float(first["Open"].iloc[0])
        m = meta[ys]
        hi52 = S.num(m.get("price_52_week_high"))
        cands.append({"ticker": m["ticker"], "name": str(m.get("description") or m["ticker"]),
                      "exchange": str(m["exchange"]).upper(), "ysym": ys, "price": price, "change": price / i["prev"] - 1,
                      "vol_x": round(vx, 2), "buy_pct30": flow["buy_pct"], "avg": i["avg"],
                      "holding": price >= op and (hi == lo or (price - lo) / (hi - lo) >= 0.5),
                      "near_high": bool(hi52 and price >= 0.95 * hi52), "extended": price / i["prev"] - 1 > S.OPEN_EXTENDED})
    cands.sort(key=lambda x: x["vol_x"], reverse=True)
    cands = cands[:150]
    print(f"  {len(cands)} met the volume and trend rules; checking news")
    since = dt.datetime.combine(day - dt.timedelta(days=S.OPEN_NEWS_DAYS + (2 if day.weekday() <= 1 else 0)),
                                dt.time(0), dt.timezone.utc)
    picks, quiet = [], []
    for c in cands:
        news = [n for n in S.live_news("america", c["ticker"], c["ysym"], since, c["name"]) if n["date"] <= day.isoformat()]
        c["news"] = news
        tones = {n["tone"] for n in news}
        if "neg" in tones or "deal" in tones:
            continue
        if "pos" in tones:
            bp = c["buy_pct30"]
            c["score"] = round(2 * c["vol_x"] + c["holding"] + c["near_high"] + 0.5 * sum(n["tone"] == "pos" for n in news)
                               - c["extended"] + (4 * (bp - 0.5) if bp is not None else 0), 2)
            picks.append(c)
        else:
            quiet.append(c)
        if len(picks) >= 25:
            break
    picks.sort(key=lambda x: x["score"], reverse=True)
    return picks[:15], quiet, mins, info, meta, since


class ReplayAccount:
    """A pretend Trading 212 account priced from the day's 1-minute bars."""
    ids = itertools.count(1)

    def __init__(self, bars, clock, cash=5000.0, fx=1.30):
        self.bars, self.clock, self.cash, self.fx = bars, clock, cash, fx
        self.pos, self.ord, self.hist = {}, {}, []
        self.sym = {}

    def _bars_to_now(self, t):
        b = self.bars[self.sym[t]]
        return b[b.index <= self.clock() - dt.timedelta(minutes=BAR["minutes"])]

    def px(self, t):
        b = self._bars_to_now(t)
        return float(b["Close"].iloc[-1]) if len(b) else float(self.bars[self.sym[t]]["Open"].iloc[0])

    def summary(self):
        return {"currency": "GBP", "totalValue": self.cash, "cash": {"availableToTrade": self.cash}, "investments": {}}

    def positions(self):
        self._check_stops()
        for t, p in self.pos.items():
            p["currentPrice"] = self.px(t)
            v = p["currentPrice"] * p["quantity"] / self.fx
            p["walletImpact"].update(currentValue=v, unrealizedProfitLoss=v - p["walletImpact"]["totalCost"])
        return list(self.pos.values())

    def orders(self):
        return [o for o in self.ord.values() if o["status"] == "NEW"]

    def instruments(self):
        return [{"ticker": f"{t}_US_EQ", "shortName": t, "name": n, "currencyCode": "USD", "type": "STOCK"}
                for t, n in self.names.items()]

    def _fill(self, t, q, price, oid):
        # dealing costs: buy a little above / sell a little below the last price (spread), plus
        # Trading 212's 0.15% currency conversion fee on each US trade
        price = price * (1 + SPREAD) if q > 0 else price * (1 - SPREAD)
        gbp = price * abs(q) / self.fx * ((1 + FX_FEE) if q > 0 else (1 - FX_FEE))
        if q > 0:
            self.cash -= gbp
            self.pos[t] = {"instrument": {"ticker": t}, "quantity": q, "quantityAvailableForTrading": q,
                           "currentPrice": price, "averagePricePaid": price,
                           "walletImpact": {"totalCost": gbp, "currentValue": gbp, "unrealizedProfitLoss": 0}}
        else:
            self.cash += gbp
            self.pos.pop(t, None)
        self.hist.insert(0, {"order": {"id": oid, "status": "FILLED", "side": "BUY" if q > 0 else "SELL", "ticker": t},
                             "fill": {"price": price, "filledAt": self.clock().isoformat(),
                                      "walletImpact": {"netValue": gbp}}})

    def market(self, t, q):
        oid = next(self.ids)
        self._fill(t, q, self.px(t), oid)
        return {"id": oid, "status": "FILLED"}

    def stop(self, t, q, price):
        oid = next(self.ids)
        self.ord[oid] = {"id": oid, "ticker": t, "quantity": q, "stopPrice": price, "status": "NEW",
                         "placed": self.clock()}
        return self.ord[oid]

    def cancel(self, oid):
        if oid in self.ord:
            self.ord[oid]["status"] = "CANCELLED"

    def history(self, t):
        return [h for h in self.hist if h["order"]["ticker"] == t]

    def _check_stops(self):
        for o in list(self.ord.values()):
            if o["status"] != "NEW" or o["ticker"] not in self.pos:
                continue
            b = self._bars_to_now(o["ticker"])
            b = b[b.index >= o["placed"]]
            hit = b[b["Low"] <= o["stopPrice"]]
            if len(hit):
                bar = hit.iloc[0]
                price = min(o["stopPrice"], float(bar["Open"]))
                o["status"] = "FILLED"
                self._fill(o["ticker"], o["quantity"], price, o["id"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--no-news", action="store_true", help="also trade heavy buying + uptrend without news")
    ap.add_argument("--strategy", default="breakout", choices=["breakout", "breakout-market", "rotation"])
    ap.add_argument("--bars", default="1m", choices=["1m", "5m"])
    a = ap.parse_args()
    P.DT_REQUIRE_NEWS = not a.no_news
    P.DT_STRATEGY = "rotation" if a.strategy == "rotation" else "breakout"
    P.DT_MARKET_FILTER = a.strategy == "breakout-market"
    BAR.update(interval=a.bars, minutes=5 if a.bars == "5m" else 1)
    day = parse_day(a.date) if a.date.strip() else last_weekday(dt.datetime.now(NY).date())
    print(f"Replaying {day} (US)")
    picks, quiet, mins, info, meta, since = build_list(day)
    t212 = set((S.load_json(ROOT / "docs" / "data" / "t212_tickers.json", {}) or {}).get("us") or [])
    print(f"  list: {[(p['ticker'], p['vol_x']) for p in picks]}")

    tmp = Path(tempfile.mkdtemp())
    (tmp / "data").mkdir()
    P.DOCS, P.STATE = tmp, tmp / "data" / "practice.json"
    (tmp / "data" / "opening.json").write_text(json.dumps(
        {"markets": {"america": {"date": day.isoformat(), "picks": picks, "quiet": quiet}}}, default=S.jdefault))
    clock_t = {"now": dt.datetime.combine(day, dt.time(10, 16), NY).astimezone(dt.timezone.utc)}
    clock = lambda: clock_t["now"]
    fxr = 1.30
    try:
        g = download(["GBPUSD=X"], start=(day - dt.timedelta(days=5)).isoformat(),
                     end=(day + dt.timedelta(days=1)).isoformat(), interval="1d").get("GBPUSD=X")
        fxr = float(g["Close"].iloc[-1]) if g is not None and len(g) else fxr
    except Exception:
        pass
    acct = ReplayAccount(mins, clock, fx=fxr)
    acct.names = {meta[ys]["ticker"]: str(meta[ys].get("description") or meta[ys]["ticker"]) for ys in mins
                  if not t212 or meta[ys]["ticker"] in t212}
    acct.sym = {f"{meta[ys]['ticker']}_US_EQ": ys for ys in mins}
    news_cache = {}

    def discover(when, known):
        """Replay version of the bot's search for new movers, from the day's 1-minute bars."""
        cut = when - dt.timedelta(minutes=BAR["minutes"])
        found, rejected = [], []
        for ys, b in mins.items():
            tick = meta[ys]["ticker"]
            if tick in known:
                continue
            i = info[ys]
            sc = P.surge_check(b[b.index <= cut], i["avg"], when, i["prev"])
            if not sc:
                continue
            last = sc["last"]
            sma20 = (sum(i["closes"][-19:]) + last) / 20
            sma50 = (sum(i["closes"][-49:]) + last) / 50
            if not (last > sma20 and last > sma50 and sma20 >= 0.97 * sma50):
                continue
            name = str(meta[ys].get("description") or tick)
            if tick not in news_cache:
                news_cache[tick] = [n for n in S.live_news("america", tick, ys, since, name) if n["date"] <= day.isoformat()]
            news = news_cache[tick]
            if not P.news_ok(news, P.DT_REQUIRE_NEWS):
                rejected.append(tick)
                continue
            top = next((n["title"] for n in news if n.get("tone") == "pos"), "")
            found.append({"ticker": tick, "name": name, "exchange": str(meta[ys]["exchange"]).upper(), "ysym": ys,
                          "vol_x": sc["vol_x"], "avg": i["avg"], "prev": i["prev"],
                          "why": f"{sc['vol_x']:.1f}x normal volume in the last 30 min"
                                 + (f"; news: {top[:80]}" if top else "; no news found")})
        return found, rejected
    alerts = []
    real_push = S.push
    S.push = lambda title, msg, *x: alerts.append(f"{clock().astimezone(NY).strftime('%H:%M')} {title}: {msg}")
    P.git_save = lambda msg: None
    P.fx_rates = lambda: {"GBP": 1.0, "GBX": 100.0, "USD": fxr, "CAD": 1.8}
    P.time.sleep = lambda s: None

    def pause(sec):
        clock_t["now"] += dt.timedelta(seconds=sec)

    def fetch_bars(ysyms):
        cut = clock() - dt.timedelta(minutes=BAR["minutes"])
        out = {y: mins[y][mins[y].index <= cut] for y in ysyms if y in mins}
        if "SPY" in ysyms and SPY_BARS is not None:
            out["SPY"] = SPY_BARS[SPY_BARS.index <= cut]
        return out

    st = P.load_state()
    res = P.cmd_daytrade(acct, st, fetch_bars=fetch_bars,
                         fetch_avg=lambda ys: {y: info[y]["avg"] for y in ys if y in info},
                         clock=clock, pause=pause, discover=discover,
                         fetch_stats=lambda ys: {y: {k: info[y][k] for k in ("avg", "prev", "range")} for y in ys if y in info})
    S.push = real_push
    trades = [c for c in st["closed"]]
    total = sum(c["pl_gbp"] for c in trades if c.get("pl_gbp") is not None)
    out = {"date": day.isoformat(), "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
           "fx_gbpusd": fxr, "result": res, "require_news": P.DT_REQUIRE_NEWS, "strategy": a.strategy,
           "bars": a.bars,
           "added_during_day": [x["text"] for x in st["log"] if x["text"].startswith("Added ")],
           "list": [{k: p.get(k) for k in ("ticker", "name", "vol_x", "buy_pct30", "change", "score")}
                    | {"news": [n["title"] for n in p["news"] if n["tone"] == "pos"][:2],
                       "on_t212": (not t212) or p["ticker"] in t212} for p in picks],
           "quiet": [{"ticker": q["ticker"], "vol_x": q["vol_x"]} for q in quiet[:15]],
           "trades": [{k: c.get(k) for k in ("ticker", "bought_at", "sold_at", "entry", "exit", "cost_gbp", "pl_gbp",
                                            "pl_pct", "reason")} for c in trades],
           "total_gbp": total, "alerts": alerts, "log": [x["text"] for x in st["log"]]}
    f = ROOT / "docs" / "data" / f"backtest-{day.isoformat()}{result_suffix(a)}.json"
    f.write_text(json.dumps(out, indent=1, default=S.jdefault), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("list", "trades", "total_gbp")}, indent=1, default=S.jdefault))
    lines = [f"{c['ticker']}: {P.gbp_txt(c['pl_gbp'])} ({c['reason'][:80]})" for c in trades if c.get("pl_gbp") is not None]
    if os.getenv("REPLAY_BATCH"):
        return                                  # batch runs send one summary alert at the end instead
    S.push(f"Replay of {day:%a %d %b} ({a.strategy}{'' if P.DT_REQUIRE_NEWS else ', no news'}): {P.gbp_txt(total)}",
           ("\n".join(lines) if lines else "No trades: nothing on the list qualified.")
           + f"\nList: {', '.join(p['ticker'] for p in picks) or 'empty'}", 3, "rewind")


if __name__ == "__main__":
    main()
