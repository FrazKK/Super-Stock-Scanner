"""
Practice-account trading on Trading 212. It only ever uses the PRACTICE (demo) account.

Strategy (PRACTICE_STRATEGY, default "catalyst"), agreed 1 Nov 2026:
  catalyst - Fraz's rules: positive news in the last 24-48 hours, relative volume 1.5x+, green and breaking above
             the opening-range high. No uptrend rule. One stock at a time (up to 5 a day), each held until its
             sell rule fires. No new buys after 11:00 New York, nothing under $1. Exits as breakout below.
  breakout - one trade a day. Watch the 5 US stocks "in play" (positive news, the most unusual volume, and
             normally moving 4%+ a day). Buy the first to break above its opening-range high (first 30 minutes).
             Stop at the opening-range low (never more than 5% away); at +1x the risk the stop moves to break-even
             plus fees; from +2x it trails 1x the risk under the high. Sold 10 minutes before the close.
             PRACTICE_MARKET_FILTER=yes also requires the US market (SPY) to be above its opening price.
  drift    - one trade a day. At 10:00 New York, buy the best US stock that had positive news in the last 5 days,
             closed up yesterday, normally moves 4%+ a day and is above today's opening price (ranked by
             yesterday's volume vs normal). 5% stop, then the same break-even and trailing stop. Sold before the close.
  rotation - the earlier rules below.

Rotation rules: US day trading, one stock at a time (agreed 31 Oct 2026)
  List   - The US opening-surge list: stocks with positive news that traded at least a third of a normal
           day's volume in the first 30 minutes, trending up. Only stocks Trading 212 offers.
           Every 15 minutes it also looks for NEW movers: a third of a normal day's volume in the last 30 minutes,
           rising, trending up, with positive news (set PRACTICE_REQUIRE_NEWS=no to drop the news requirement;
           stocks with bad news, placings or takeover news are always left out).
  Buy    - First buy of the day: the stock on that list with the highest first-30-minute volume
           (compared with its normal daily volume).
  Sell   - When the price is 5% below the buy price (a stop-loss order also sits at Trading 212), or
         - after holding at least 45 minutes, when it hasn't made a new high for 30 minutes AND another stock
           on the list is buying clearly harder over the last 30 minutes (at least 1.5x as much buying, and at
           least a third of a normal day's volume bought). It then buys that stock straight away.
         - Everything is sold 10 minutes before the close. Nothing is held overnight.
  Size   - GBP 1,000 per trade, whole shares. After a stop-out it buys the list's strongest current buyer.
           No new buys in the last 30 minutes, at most 4 buys a day, nothing already up 20%+ on the day, a stock
           that was stopped out is never bought again that day, and one sold for any other reason can't be
           bought back for 45 minutes. No company size limit.

The older rules (opening-surge and 2+ methods picks held for days by each method's sell rule) are still
here as --opening / --preclose / --sync, but the workflows no longer run them.

Commands:
  python practice.py --check      test the connection and send a phone alert with the account value
  python practice.py --daytrade   watch and trade the US session (runs until 10 minutes before the close)

Needs repository secrets T212_KEY and T212_SECRET created in the Trading 212 PRACTICE account.
"""
import argparse
import base64
import datetime as dt
import json
import math
import os
import re
import time
from pathlib import Path

import screener as S

BASE = "https://demo.trading212.com/api/v0"      # practice money only; there is deliberately no live address here
TRADE_GBP = 1000.0
MAX_OPEN = 10
SKIP_EXTENDED = True         # don't buy opening picks already up 20%+ on the day
SURGE_MAX_RISK = 0.08        # opening-surge stop: the day's low, but never more than 8% below the price
SURGE_EMA = 10               # opening-surge exit: a close below the 10-day EMA (from the day after buying)
WEEKLY_SAFETY = 0.03         # weekly-MA methods: the stop order sits 3% under the moving average
CONSENSUS_MAX_AGE = 7        # days: only buy "2+ methods" picks from a scan this recent

# Day trading (US)
DT_STOP = 0.05               # sell 5% below the buy price
DT_FLAT_MIN = 30             # "slowing down": no new high for this many minutes
DT_RISING_MIN = 15           # "rising": price higher than this many minutes ago
DT_MIN_HOLD = 45             # minutes a stock is held before it can be swapped (the 5% stop always applies)
DT_OVERTAKE = 1.5            # to swap, the new stock's buying must be at least 1.5x the current one's...
DT_OVERTAKE_MIN = 1 / 3      # ...and at least a third of a normal day's volume bought in the last 30 minutes
DT_MAX_UP = 0.20             # don't buy a stock already up more than 20% on the day

# Which day-trading strategy to run: "breakout" (default), "catalyst", "drift" or "rotation"
DT_STRATEGY = os.getenv("PRACTICE_STRATEGY", "catalyst").strip().lower()
# Breakout (opening-range breakout on "stocks in play"):
DT_OR_MIN = 30               # opening range = the first 30 minutes' high and low
DT_TOP_N = 5                 # only the 5 stocks with the most unusual volume count as "in play"
DT_BO_MIN_VOLX = 0.0         # minimum volume vs a normal day (first 30 min, or last 30 min for later movers)
DT_BUY_UNTIL = None          # minutes after the open after which no new buys are made (None = until DT_LAST_BUY)
DT_MIN_PRICE = 0.0           # skip stocks priced below this (US dollars)
DT_MIN_RANGE = 0.04          # the stock must normally move at least 4% a day (14-day average high-low range)
DT_BO_BUYS = 1               # buys per day
DT_BO_MAX_RISK = 0.05        # stop at the opening-range low, but never more than 5% below the buy price
DT_BE_AT_R = 1.0             # once up 1x the risk, move the stop to break-even (plus fees)
DT_TRAIL_AT_R = 2.0          # once up 2x the risk, trail the stop...
DT_TRAIL_R = 1.0             # ...1x the risk below the highest price since buying
DT_MARKET_FILTER = os.getenv("PRACTICE_MARKET_FILTER", "no").lower() in ("yes", "true", "1")
# Claude news check (optional): before each buy, Claude reads the stock's headlines and decides whether there's a
# genuine, price-moving company catalyst. Needs the ANTHROPIC_API_KEY secret; without it the keyword check is used.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
CLAUDE_MIN_STRENGTH = 3      # 1-5: how strong the catalyst must be to buy
# Drift (news in the last few days, closed up yesterday, still rising this morning):
DT_DRIFT_NEWS_DAYS = 5       # positive news within the last 5 days
DT_DRIFT_AT = 30             # decide 30 minutes after the open (buy if above today's opening price)
DT_DRIFT_CHECKS = 40         # how many candidates to check for news (best first)
                             # only buy when the US market (SPY) is above where it opened today
DT_WINDOW = 30               # minutes of buying volume compared between stocks
DT_POLL = 120                # seconds between checks
DT_LAST_BUY = 30             # no new buys in the last 30 minutes before the close
DT_EXIT = 10                 # sell 10 minutes before the close
DT_MAX_BUYS = 4              # buys per day, to stop it churning
DT_COOLDOWN = 45             # minutes before a stock that was sold can be bought again
DT_RUN_LIMIT = 5.6 * 3600    # GitHub stops a job at 6 hours: stop just before, the backup timer restarts it
DT_RESCAN = 15               # minutes between searches for new movers during the day
DT_RECHECK = 60              # minutes before a stock that didn't qualify is looked at again
DT_REQUIRE_NEWS = os.getenv("PRACTICE_REQUIRE_NEWS", "yes").lower() not in ("no", "false", "0")
                             # True: only stocks with positive news. False: heavy buying + uptrend is enough
                             # (stocks with bad news, placings or takeover news are always left out)
ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
STATE = DOCS / "data" / "practice.json"
CURRENCIES = {"america": {"USD"}, "uk": {"GBX", "GBP"}, "canada": {"CAD"}}
NAME_NOISE = {"plc", "inc", "corp", "corporation", "ltd", "limited", "holdings", "group", "company", "the", "and",
              "class", "co", "sa", "nv", "ag", "common", "stock", "shares", "ordinary"}


# ---------------------------------------------------------------------------
# Trading 212 API
# ---------------------------------------------------------------------------

class T212Error(Exception):
    def __init__(self, code, text):
        super().__init__(f"{code}: {text}")
        self.code, self.text = code, text


GAPS = {"summary": 5.2, "orders": 5.2, "order": 1.1, "positions": 1.1, "instruments": 51, "market": 1.3,
        "stop": 2.1, "cancel": 1.3, "history": 1.3}


class T212:
    def __init__(self, key, secret, session=None):
        import requests

        self.s = session or requests.Session()
        tok = base64.b64encode(f"{key.strip()}:{secret.strip()}".encode()).decode()
        self.s.headers.update({"Authorization": f"Basic {tok}", "Content-Type": "application/json"})
        self.last = {}

    def _call(self, kind, method, path, body=None, retry=True):
        if not BASE.startswith("https://demo."):
            raise RuntimeError("practice.py only trades on the practice account")
        for attempt in range(4):
            wait = self.last.get(kind, 0) + GAPS[kind] - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last[kind] = time.time()
            try:
                r = self.s.request(method, BASE + path, json=body, timeout=30)
            except Exception as e:
                # Orders aren't safe to resend (a lost reply may still have placed one), so only reads retry.
                if not retry or attempt == 3:
                    raise T212Error(0, str(e))
                time.sleep(3)
                continue
            if r.status_code == 429:        # rejected for rate limit, so safe to try again after the reset
                try:
                    reset = float(r.headers.get("x-ratelimit-reset") or 0)
                except ValueError:
                    reset = 0
                time.sleep(min(max(reset - time.time(), 2), 65))
                continue
            if r.status_code in (408, 500, 502, 503, 504) and retry and attempt < 3:
                time.sleep(3)
                continue
            if r.status_code >= 400:
                raise T212Error(r.status_code, r.text[:300])
            return r.json() if r.content and r.content.strip() else None
        raise T212Error(429, "rate limited")

    def summary(self):
        return self._call("summary", "GET", "/equity/account/summary") or {}

    def positions(self):
        return self._call("positions", "GET", "/equity/positions") or []

    def orders(self):
        return self._call("orders", "GET", "/equity/orders") or []

    def instruments(self):
        return self._call("instruments", "GET", "/equity/metadata/instruments") or []

    def market(self, ticker, qty):
        return self._call("market", "POST", "/equity/orders/market", {"ticker": ticker, "quantity": qty}, retry=False)

    def stop(self, ticker, qty, price):
        return self._call("stop", "POST", "/equity/orders/stop",
                          {"ticker": ticker, "quantity": qty, "stopPrice": price, "timeValidity": "GOOD_TILL_CANCEL"},
                          retry=False)

    def cancel(self, oid):
        return self._call("cancel", "DELETE", f"/equity/orders/{oid}")

    def history(self, ticker):
        return (self._call("history", "GET", f"/equity/history/orders?ticker={ticker}&limit=20") or {}).get("items") or []


def pos_ticker(p):
    return (p.get("instrument") or {}).get("ticker") or p.get("ticker")


def wallet(p, key):
    w = p.get("walletImpact") or {}
    return S.num(w.get(key))


# ---------------------------------------------------------------------------
# State, logging, helpers
# ---------------------------------------------------------------------------

def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def load_state():
    st = S.load_json(STATE, None) or {}
    st.setdefault("started", now_utc().date().isoformat())
    for k, v in (("open", []), ("closed", []), ("log", []), ("done", {}), ("account", {})):
        st.setdefault(k, v)
    st["rules"] = {"trade_gbp": TRADE_GBP, "max_open": MAX_OPEN}
    return st


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    st["updated"] = now_utc().isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, indent=1, default=S.jdefault), encoding="utf-8")


def log(st, text):
    print(f"  {text}")
    st["log"].append({"time": now_utc().isoformat(timespec="minutes"), "text": text})
    st["log"] = st["log"][-80:]


def fx_rates():
    """Units of each currency per pound."""
    out = {"GBP": 1.0, "GBX": 100.0}
    try:
        import yfinance as yf

        for c, sym in (("USD", "GBPUSD=X"), ("CAD", "GBPCAD=X")):
            h = yf.Ticker(sym).history(period="5d")
            if len(h):
                out[c] = float(h["Close"].iloc[-1])
    except Exception as e:
        print(f"  fx rates failed: {e}")
    out.setdefault("USD", 1.30)
    out.setdefault("CAD", 1.80)
    return out


def to_gbp(price, ccy, market, fx):
    ccy = (ccy or {"uk": "GBX", "america": "USD", "canada": "CAD"}[market]).upper()
    if ccy == "GBP" and market == "uk" and price and price > 50:
        ccy = "GBX"          # TradingView sometimes labels pence prices as GBP
    rate = fx.get(ccy)
    return price / rate if price and rate else None


def unit_factor(t212_price, local_price):
    """Trading 212 and our data can quote the same UK share in pence or pounds."""
    if not t212_price or not local_price:
        return 1.0
    f = t212_price / local_price
    return min((1.0, 0.01, 100.0), key=lambda u: abs(math.log(f / u)))


def gbp_txt(x):
    return f"{'+' if x >= 0 else '-'}£{abs(x):,.0f}"


def qty_txt(q):
    q = S.num(q) or 0
    return f"{q:,.0f}" if float(q).is_integer() else f"{q:,.2f}"


def round_price(x):
    return round(x, 4) if x < 1 else round(x, 2)


def market_open(market, when):
    mins, _ = S.minutes_since_open(market, when)
    left, _ = S.minutes_to_close(market, when)
    return mins is not None and mins >= 0 and left is not None and left > 5


def find_instrument(insts, market, ticker, name=""):
    t = str(ticker).upper().rstrip(".")
    variants = {t, t.replace(".", ""), t.replace(".", "/"), t.replace("-", "."), t.replace(".", "-")}
    cands = [i for i in insts if str(i.get("shortName") or "").upper() in variants
             and i.get("currencyCode") in CURRENCIES[market] and i.get("type", "STOCK") == "STOCK"]
    if market == "america":
        cands = [i for i in cands if str(i.get("ticker", "")).endswith("_US_EQ")] or cands
    words = [w for w in re.findall(r"[a-z0-9]{3,}", (name or "").lower()) if w not in NAME_NOISE][:1]
    if cands and words:
        named = [i for i in cands if words[0] in str(i.get("name") or "").lower()]
        if not named and len(cands) > 1:
            return None
        cands = named or cands
    return cands[0] if cands else None


# ---------------------------------------------------------------------------
# Stops and sell rules
# ---------------------------------------------------------------------------

def method_level(meth, stop, d, entry):
    """Where this method says the trade has failed; the stop-loss order sits at the highest of these."""
    if meth == "surge":
        return stop or entry * (1 - SURGE_MAX_RISK)
    if meth in ("luk", "kell"):
        return stop or entry * (1 - S.MIN_STOP)
    if meth in ("minervini", "episodic"):
        return stop or entry * (1 - S.MIN_STOP)
    if d is None or len(d) < 60:
        return None
    n = 10 if meth == "stine" else 30
    ma = S.to_weekly(d)["Close"].rolling(n).mean().iloc[-1]
    return float(ma) * (1 - WEEKLY_SAFETY) if ma == ma else None


def target_stop(pos, d):
    entry = pos.get("entry_local") or pos["ref_price"]
    levels = [method_level(m["method"], m.get("stop"), d, entry) for m in pos["methods"]]
    levels = [x for x in levels if x]
    return max(levels) if levels else entry * 0.85


def surge_check(pos, d, today, has_today):
    stop = pos["methods"][0].get("stop") or pos["entry_local"] * (1 - SURGE_MAX_RISK)
    if d is None or len(d) < 15:
        return "HOLD", "No price data, so holding (the stop-loss order still protects it)."
    if not has_today:
        return "HOLD", "No trading data for today yet."
    c = d["Close"]
    last = float(c.iloc[-1])
    e = float(S.ema(c, SURGE_EMA).iloc[-1])
    if last <= stop:
        return "SELL", f"At {S.fmt_num(last)}, at or below the stop ({S.fmt_num(stop)})."
    if today > dt.date.fromisoformat(pos["bought"]) and last < e:
        return "SELL", f"At {S.fmt_num(last)}, below the {SURGE_EMA}-day EMA ({S.fmt_num(e)}) an hour before the close."
    return "HOLD", f"{(last / e - 1) * 100:.0f}% above the {SURGE_EMA}-day EMA."


def evaluate(pos, d, when):
    """SELL or HOLD with the reason: the first method to say sell wins."""
    _, local = S.minutes_to_close(pos["market"], when)
    today = local.date()
    has_today = d is not None and len(d) and d.index[-1].date() == today
    notes = []
    for m in pos["methods"]:
        meth = m["method"]
        if meth == "surge":
            act, why = surge_check(pos, d, today, has_today)
            name = "Opening surge"
        else:
            h = {"file": pos["id"], "ticker": pos["ticker"], "market": pos["market"], "name": pos["name"],
                 "method": meth, "price": pos.get("entry_local") or pos["ref_price"], "bought": pos["bought"],
                 "exchange": pos["exchange"], "ysym": pos["ysym"], "stop": m.get("stop")}
            st = S.check_holding(h, d, today)
            act, _, why = S.preclose_advice(st, meth, local.weekday() == 4, has_today)
            name = S.STRATEGIES.get(meth, {}).get("short", meth)
        if act == "SELL":
            return "SELL", f"{name}: {why}"
        notes.append(f"{name}: {why}")
    return "HOLD", " · ".join(notes)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def place_stop(api, st, pos, p, level_local):
    cur = S.num(p.get("currentPrice"))
    unit = pos.get("unit", 1.0)
    price = level_local * unit
    if cur and price >= cur * 0.995:
        pos["stop_breached"] = True
        log(st, f"{pos['ticker']}: price is already at the stop level ({S.fmt_num(price)}), so it will be sold "
                f"at the pre-close check rather than placing a stop order.")
        return False
    qty = -abs(S.num(p.get("quantityAvailableForTrading")) or S.num(p.get("quantity")) or pos["qty"])
    try:
        o = api.stop(pos["t212"], qty, round_price(price))
    except T212Error as e:
        log(st, f"{pos['ticker']}: couldn't place the stop-loss order ({e.text[:120]}).")
        return False
    pos.update(stop_order_id=(o or {}).get("id"), stop_t212=round_price(price), stop_local=level_local,
               stop_breached=False)
    return True


def raise_stop(api, st, pos, p, active, new_level):
    old = pos.get("stop_local") or 0
    if new_level <= old * 1.005:
        return
    if pos.get("stop_order_id") in active:
        try:
            api.cancel(pos["stop_order_id"])
            time.sleep(1.5)
        except T212Error as e:
            if e.code != 404:
                log(st, f"{pos['ticker']}: couldn't move the stop ({e.text[:100]}).")
                return
    if place_stop(api, st, pos, p, new_level):
        log(st, f"{pos['ticker']}: stop raised from {S.fmt_num(old)} to {S.fmt_num(new_level)}.")
    elif old:
        place_stop(api, st, pos, p, old)


def record_closed(st, pos, exit_local, proceeds, reason, when):
    cost = pos.get("cost_gbp")
    pl = (proceeds - cost) if (proceeds is not None and cost) else None
    st["closed"].append({
        "ticker": pos["ticker"], "name": pos["name"], "market": pos["market"], "source": pos["source"],
        "methods": [m["method"] for m in pos["methods"]], "bought": pos["bought"], "sold": when.date().isoformat(),
        "days": (when.date() - dt.date.fromisoformat(pos["bought"])).days,
        "entry": pos.get("entry_local"), "exit": exit_local, "cost_gbp": cost,
        "proceeds_gbp": proceeds, "pl_gbp": pl, "pl_pct": (pl / cost) if (pl is not None and cost) else None,
        "reason": reason, "bought_at": pos.get("bought_at"), "sold_at": when.isoformat(timespec="minutes")})
    st["open"] = [x for x in st["open"] if x["id"] != pos["id"]]


def last_sell_fill(api, pos):
    """The most recent sell fill for this ticker since it was bought."""
    try:
        items = api.history(pos["t212"])
    except T212Error:
        return None
    for it in items:
        o, f = it.get("order") or {}, it.get("fill") or {}
        if o.get("side") == "SELL" and o.get("status") == "FILLED" and str(f.get("filledAt", "")) >= pos["bought"]:
            return o, f
    return None


def reconcile(api, st, when):
    """Match the saved positions with Trading 212: fills, stop-outs and missing stops."""
    positions = {pos_ticker(p): p for p in api.positions()}
    active = {o.get("id"): o for o in api.orders()}
    events = []
    for pos in list(st["open"]):
        p = positions.get(pos["t212"])
        if pos["status"] == "pending":
            if not p:
                if pos.get("order_id") in active:
                    continue
                filled = False
                try:
                    filled = any((it.get("order") or {}).get("id") == pos.get("order_id")
                                 and (it.get("order") or {}).get("status") == "FILLED" for it in api.history(pos["t212"]))
                except T212Error:
                    pass
                if not filled:
                    st["open"] = [x for x in st["open"] if x["id"] != pos["id"]]
                    log(st, f"{pos['ticker']}: the buy order didn't go through, so it was dropped.")
                    events.append(f"{pos['ticker']}: buy didn't go through")
                continue
            entry_t = S.num(p.get("averagePricePaid"))
            unit = unit_factor(entry_t, pos["ref_price"])
            pos.update(status="open", qty=S.num(p.get("quantity")), entry_t212=entry_t, unit=unit,
                       entry_local=(entry_t / unit) if entry_t else pos["ref_price"],
                       cost_gbp=wallet(p, "totalCost"))
            # the stop was worked out from the signal price: keep the same distance below the actual fill
            pos["stop_local"] = None
            log(st, f"Bought {qty_txt(pos['qty'])} {pos['ticker']} at {S.fmt_num(pos['entry_local'])} "
                    f"(cost £{(pos['cost_gbp'] or 0):,.0f}).")
        if not p:
            hit = last_sell_fill(api, pos)
            o, f = hit if hit else ({}, {})
            proceeds = S.num((f.get("walletImpact") or {}).get("netValue"))
            exit_t = S.num(f.get("price"))
            exit_local = exit_t / pos.get("unit", 1.0) if exit_t else None
            why = ("Stop-loss order filled" if o.get("id") and o.get("id") == pos.get("stop_order_id")
                   else "Sold outside the bot" if o else "No longer in the account")
            record_closed(st, pos, exit_local, proceeds, why, when)
            pl = st["closed"][-1]["pl_gbp"]
            log(st, f"{pos['ticker']}: {why.lower()}" + (f", P&L {gbp_txt(pl)}." if pl is not None else "."))
            events.append(f"{pos['ticker']}: {why.lower()}" + (f" ({gbp_txt(pl)})" if pl is not None else ""))
            continue
        pos.update(qty=S.num(p.get("quantity")), price_t212=S.num(p.get("currentPrice")),
                   value_gbp=wallet(p, "currentValue"), pl_gbp=wallet(p, "unrealizedProfitLoss"))
        if pos.get("stop_order_id") not in active and not pos.get("stop_breached"):
            level = pos.get("stop_local") or (pos["initial_stop"] / pos["ref_price"] * pos["entry_local"])
            place_stop(api, st, pos, p, level)
    return positions, active, events


def update_account(api, st):
    try:
        s = api.summary()
    except T212Error as e:
        print(f"  account summary failed: {e}")
        return
    acct = {"currency": s.get("currency"), "value": S.num(s.get("totalValue")),
            "cash": S.num((s.get("cash") or {}).get("availableToTrade")),
            "realised": S.num((s.get("investments") or {}).get("realizedProfitLoss")),
            "unrealised": S.num((s.get("investments") or {}).get("unrealizedProfitLoss")),
            "time": now_utc().isoformat(timespec="minutes")}
    st["account"] = acct
    if not st.get("start_value") and acct["value"]:
        st["start_value"] = acct["value"]


# ---------------------------------------------------------------------------
# Buying
# ---------------------------------------------------------------------------

def opening_candidates(m, data):
    out = []
    for x in data.get("picks") or []:
        if not x.get("buyers") or (SKIP_EXTENDED and x.get("extended")):
            continue
        price = S.num(x.get("price"))
        if not price:
            continue
        low = S.num(x.get("low"))
        stop = max(low, price * (1 - SURGE_MAX_RISK)) if low and low < price else price * (1 - SURGE_MAX_RISK)
        bp = x.get("buy_pct30") if x.get("buy_pct30") is not None else x.get("buy_pct")
        top = next((n["title"] for n in x.get("news") or [] if n.get("tone") == "pos"), "")
        out.append({"source": "opening", "ticker": x["ticker"], "name": x["name"], "market": m,
                    "exchange": x["exchange"], "ysym": x["ysym"], "price": price, "currency": x.get("currency"),
                    "methods": [{"method": "surge", "stop": stop}],
                    "why": f"Opening surge {S.pct(x['change'])}, {x['vol_x']:.1f}x normal volume in 30 min, "
                           f"est. {bp:.0%} buying. {top[:100]}".strip()})
    return out


def consensus_candidates(m, latest):
    mdata = (latest.get("markets") or {}).get(m) or {}
    lists = mdata.get("lists") or {}
    out = []
    for e in mdata.get("multi") or []:
        price = S.num(e.get("entry_close")) or S.num(e.get("price"))
        if not price:
            continue
        methods = []
        for mm in e.get("methods") or []:
            k = mm["strategy"]
            card = next((x for x in lists.get(k) or [] if x["ticker"] == e["ticker"]), {})
            methods.append({"method": k, "stop": S.num(card.get("stop"))})
        names = ", ".join(S.STRATEGIES.get(x["method"], {}).get("short", x["method"]) for x in methods)
        out.append({"source": "consensus", "ticker": e["ticker"], "name": e["name"], "market": m,
                    "exchange": e["exchange"], "ysym": e["ysym"], "price": price, "currency": e.get("currency"),
                    "methods": methods, "why": f"{len(methods)} methods agree: {names}."})
    return out


def buy_all(api, st, cands, positions, when):
    slots = MAX_OPEN - len(st["open"])
    if slots <= 0 or not cands:
        if cands:
            log(st, f"Full ({MAX_OPEN} positions), so skipped: {', '.join(c['ticker'] for c in cands)}.")
        return []
    print(f"  {len(cands)} candidate(s), {slots} slot(s) free")
    insts = api.instruments()
    fx = fx_rates()
    cash = S.num((api.summary().get("cash") or {}).get("availableToTrade")) or 0
    held = {(p["market"], p["ticker"]) for p in st["open"]}
    weekly = [c for c in cands if any(m["method"] not in ("surge", "luk", "kell", "minervini", "episodic")
                                      for m in c["methods"])]
    prices = S.live_prices([c["ysym"] for c in weekly]) if weekly else {}
    bought = []
    for c in cands:
        if len(st["open"]) >= MAX_OPEN:
            log(st, f"Full ({MAX_OPEN} positions), so skipped {c['ticker']}.")
            continue
        if (c["market"], c["ticker"]) in held:
            continue
        inst = find_instrument(insts, c["market"], c["ticker"], c["name"])
        if not inst:
            log(st, f"{c['ticker']}: not found on Trading 212, skipped.")
            continue
        if inst["ticker"] in positions:
            log(st, f"{c['ticker']}: already held in the practice account (not by the bot), skipped.")
            continue
        pg = to_gbp(c["price"], c.get("currency"), c["market"], fx)
        qty = math.floor(TRADE_GBP / pg) if pg else 0
        if qty < 1:
            log(st, f"{c['ticker']}: one share costs more than £{TRADE_GBP:,.0f}, skipped.")
            continue
        if S.num(inst.get("maxOpenQuantity")) and qty > inst["maxOpenQuantity"]:
            qty = int(inst["maxOpenQuantity"])
        if qty * pg > cash * 0.98:
            log(st, f"{c['ticker']}: not enough practice cash (£{cash:,.0f}), skipped.")
            continue
        pos = {"id": f"{c['market']}-{c['ticker']}-{when.date().isoformat()}", "status": "pending",
               "ticker": c["ticker"], "t212": inst["ticker"], "name": c["name"], "market": c["market"],
               "exchange": c["exchange"], "ysym": c["ysym"], "source": c["source"], "methods": c["methods"],
               "bought": when.date().isoformat(), "ref_price": c["price"], "qty": qty, "why": c["why"]}
        pos["initial_stop"] = target_stop(pos, prices.get(c["ysym"]))
        try:
            o = api.market(inst["ticker"], qty)
        except T212Error as e:
            log(st, f"{c['ticker']}: buy order refused ({e.text[:120]}).")
            continue
        pos["order_id"] = (o or {}).get("id")
        st["open"].append(pos)
        held.add((c["market"], c["ticker"]))
        cash -= qty * pg
        bought.append(pos)
        log(st, f"Buy order: {qty} {c['ticker']} (about £{qty * pg:,.0f}). {c['why']}")
    return bought


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def save_tickers(api, insts=None):
    """Publish which US and UK shares Trading 212 offers, for the morning news summaries to check against."""
    try:
        insts = insts if insts is not None else api.instruments()
    except T212Error as e:
        print(f"  instrument list failed: {e}")
        return
    out = {"updated": now_utc().isoformat(timespec="minutes"), "us": [], "uk": []}
    for i in insts:
        if i.get("type", "STOCK") != "STOCK" or not i.get("shortName"):
            continue
        if str(i.get("ticker", "")).endswith("_US_EQ"):
            out["us"].append(str(i["shortName"]).upper())
        elif i.get("currencyCode") in ("GBX", "GBP"):
            out["uk"].append(str(i["shortName"]).upper())
    out["us"], out["uk"] = sorted(set(out["us"])), sorted(set(out["uk"]))
    if out["us"]:
        f = DOCS / "data" / "t212_tickers.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
        print(f"  Trading 212 list saved: {len(out['us'])} US, {len(out['uk'])} UK shares")


def cmd_opening(api, st, when):
    positions, active, events = reconcile(api, st, when)
    op = S.load_json(DOCS / "data" / "opening.json", {}) or {}
    latest = S.load_json(DOCS / "data" / "latest.json", {}) or {}
    bought = []
    for m in S.MARKETS:
        data = (op.get("markets") or {}).get(m)
        _, local = S.minutes_since_open(m, when)
        today = local.date().isoformat()
        if not data or data.get("date") != today or st["done"].get(f"opening-{m}") == today:
            continue
        if not market_open(m, when):
            print(f"  {m}: market not open now, no orders placed")
            continue
        cands = []
        scan = str(latest.get("date") or "")[:10]
        fresh = scan and (local.date() - dt.date.fromisoformat(scan)).days <= CONSENSUS_MAX_AGE
        if fresh and st["done"].get(f"consensus-{m}") != scan:
            cands += consensus_candidates(m, latest)
            st["done"][f"consensus-{m}"] = scan
        cands += opening_candidates(m, data)          # already in score order
        st["done"][f"opening-{m}"] = today
        if not cands:
            log(st, f"{S.MARKETS[m]['label']}: nothing met the buy rules today.")
            continue
        bought += buy_all(api, st, cands, positions, when)
    if bought:
        time.sleep(5)
        positions, active, ev2 = reconcile(api, st, when)
        events += ev2
    return bought, events


def cmd_preclose(api, st, when, forced):
    positions, active, events = reconcile(api, st, when)
    due = []
    for m in S.MARKETS:
        left, local = S.minutes_to_close(m, when)
        if forced:
            if m in forced:
                due.append(m)
        elif left is not None and 10 <= left <= 80 and st["done"].get(f"preclose-{m}") != local.date().isoformat():
            due.append(m)
    ps = [p for p in st["open"] if p["status"] == "open" and p["market"] in due]
    for m in due:
        st["done"][f"preclose-{m}"] = S.minutes_to_close(m, when)[1].date().isoformat()
    if not ps:
        return [], events
    prices = S.live_prices([p["ysym"] for p in ps])
    sold = []
    for pos in ps:
        p = positions.get(pos["t212"])
        if not p:
            continue
        d = prices.get(pos["ysym"])
        act, why = evaluate(pos, d, when)
        if act == "SELL" or pos.get("stop_breached"):
            if not market_open(pos["market"], when):
                log(st, f"{pos['ticker']}: the rule says sell ({why}), but the market is closed.")
                continue
            if sell(api, st, pos, p, active, why, when):
                sold.append(st["closed"][-1])
        else:
            pos["last_check"] = why
            raise_stop(api, st, pos, p, active, target_stop(pos, d))
    return sold, events


def sell(api, st, pos, p, active, why, when):
    if pos.get("stop_order_id") in active:
        try:
            api.cancel(pos["stop_order_id"])
            time.sleep(2)
        except T212Error as e:
            if e.code != 404:
                log(st, f"{pos['ticker']}: couldn't cancel the stop before selling ({e.text[:100]}).")
                return False
    qty = S.num(p.get("quantity")) or pos["qty"]
    try:
        api.market(pos["t212"], -abs(qty))
    except T212Error as e:
        log(st, f"{pos['ticker']}: sell order refused ({e.text[:120]}). Putting the stop back.")
        place_stop(api, st, pos, p, pos.get("stop_local") or pos["initial_stop"])
        return False
    price_t = S.num(p.get("currentPrice"))
    value = wallet(p, "currentValue")
    record_closed(st, pos, price_t / pos.get("unit", 1.0) if price_t else None, value, why, when)
    pl = st["closed"][-1]["pl_gbp"]
    log(st, f"Sold {pos['ticker']}" + (f" (about {gbp_txt(pl)})" if pl is not None else "") + f". {why}")
    return True


def fmt_buy(pos):
    stop = pos.get("stop_local") or pos.get("initial_stop")
    return (f"{pos['ticker']}: {qty_txt(pos['qty'])} shares at ~{S.fmt_num(pos.get('entry_local') or pos['ref_price'])}"
            f"{', stop ' + S.fmt_num(stop) if stop else ''}. {pos['why']}")


# ---------------------------------------------------------------------------
# Day trading: one US stock at a time, rotating to the strongest buyer
# ---------------------------------------------------------------------------

def git_save(msg):
    """Commit the practice data mid-run so the page stays up to date (only on GitHub)."""
    if not os.getenv("GITHUB_ACTIONS"):
        return
    import subprocess

    try:
        S.rerender_page(DOCS)
    except Exception as e:
        print(f"  page refresh failed: {e}")
    for c in (["git", "add", "docs"], ["git", "commit", "-q", "-m", msg],
              ["git", "pull", "-q", "--rebase", "-X", "theirs"], ["git", "push", "-q"]):
        r = subprocess.run(c, cwd=ROOT, capture_output=True, text=True)
        if r.returncode and c[1] == "commit":
            return                                   # nothing new to save
        if r.returncode:
            print(f"  git {c[1]} failed: {(r.stderr or r.stdout)[:200]}")
            return


def live_avg_volume(ysyms):
    """Average daily volume over the last 10 complete days."""
    import pandas as pd
    import yfinance as yf

    out = {}
    try:
        data = yf.download(list(ysyms), period="1mo", interval="1d", group_by="ticker", auto_adjust=False,
                           progress=False, threads=True)
    except Exception as e:
        print(f"  average volume download failed: {e}")
        return out
    today = dt.date.today()
    for s in ysyms:
        try:
            d = data[s] if isinstance(data.columns, pd.MultiIndex) else data
            v = d["Volume"].dropna()
            v = v[[ix.date() < today for ix in v.index]]
            if len(v):
                out[s] = float(v.iloc[-10:].mean())
        except Exception:
            continue
    return out


def ny(when):
    from zoneinfo import ZoneInfo
    return when.astimezone(ZoneInfo("America/New_York"))


def dt_metrics(bars, avg, when):
    """Buying volume over the last 30 minutes (as a multiple of normal daily volume) and whether it's rising."""
    if bars is None or not len(bars):
        return None
    f = S.flow_split(bars, when - dt.timedelta(minutes=DT_WINDOW))
    buy_vol = f["volume"] * f["buy_pct"] if f["buy_pct"] is not None else 0.0
    idx = bars.index.tz_convert("UTC") if bars.index.tz is not None else bars.index.tz_localize("UTC")
    last = float(bars["Close"].iloc[-1])
    earlier = bars["Close"][idx <= when - dt.timedelta(minutes=DT_RISING_MIN)]
    p15 = float(earlier.iloc[-1]) if len(earlier) else None
    return {"buy_x": (buy_vol / avg) if avg else 0.0, "buy_vol": buy_vol, "last": last,
            "rising": p15 is None or last >= p15, "p15": p15}


CLAUDE_SYSTEM = (
    "You screen US small-cap stocks for a day-trading bot. You are given a stock's recent headlines, each marked as "
    "OFFICIAL (a company announcement: press-release wire, RNS, SEC, the company's own site), ANALYST (a broker "
    "upgrade or price-target change) or OTHER (commentary, previews, gossip, market reports). Decide whether there is a "
    "genuine, specific, price-moving company catalyst in an OFFICIAL headline from the last 48 hours: e.g. FDA or "
    "regulatory approval, clearly positive trial data, earnings or guidance well ahead of expectations, a large contract "
    "or order relative to the company's size, a major partnership with money attached, an oil/gas discovery or strong "
    "drill results, a resource upgrade, a product launch that matters to revenue. For oil and gas explorers, strong "
    "signs are: commercial discovery or declared commerciality; flow-test rates (bopd, boepd, mmscf/d) that are high "
    "for the company's size; metres of net pay; hydrocarbons encountered with a clear column or pay zone; a farm-in by "
    "a major partner or a carried interest; a campaign now fully funded; a strategic review or formal sale process "
    "(no offer yet). Weaker positives that need size or context: a well spudded, a high-impact well starting, a CPR "
    "or 2C/2P/prospective resource upgrade, a licence award. Traps that mean NO catalyst (or negative): 'oil shows' "
    "or 'gas shows' or traces only, a 'tight formation' or reservoir needing stimulation, 'working with all "
    "stakeholders' (usually funding trouble), dry hole, plugged and abandoned, non- or sub-commercial, water-bearing, "
    "results below pre-drill expectations. For miners: high-grade intercepts (g/t), maiden or upgraded JORC/NI 43-101 "
    "resources, positive PFS/DFS, first gold pour. Biotech: approval, priority review, met primary endpoint with "
    "statistical significance are strong; Fast Track/orphan designations are moderate; 'well tolerated', 'met safety "
    "endpoint', 'trend toward', submitting an IND/NDA or setting a PDUFA date are NOT catalysts; CRL, clinical hold, "
    "missed endpoints and 'strategic reprioritization' (layoffs) are negative. Tech/defence/consumer: a named large "
    "customer, a task order or contract with a firm value, design wins, volume production, ARR or same-store sales "
    "growth are strong. Non-binding LOIs, MOUs, teaming agreements, 'up to $' or IDIQ ceiling values, 'exploring' "
    "deals, AI-buzzword product launches and crypto-treasury plans are NOT catalysts. Going-concern doubts, Nasdaq "
    "deficiency notices, guidance cuts and recalls are negative. Routine items (conference "
    "attendance, presentations, earnings dates, AGMs, appointments, minor updates, loans/refinancing) are NOT "
    "catalysts. Takeover targets, share offerings, placings, warnings and misses rule a stock out. ANALYST upgrades "
    "from established brokers add strength only when there is already an official catalyst. Use OTHER headlines only "
    "as sentiment: clearly negative sentiment lowers strength. Reply with JSON only: "
    '{"catalyst": true or false, "strength": 1-5, "reason": "one short sentence"}')


def claude_check(st, day, c, when):
    """Ask Claude whether the stock has a real company catalyst. Returns (ok, reason). Falls back to the keyword
    check (ok) when there's no API key or the call fails, so trading never stops because of it."""
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    cache = day.setdefault("claude", {})
    if c["ticker"] in cache:
        v = cache[c["ticker"]]
        return v["ok"], v["reason"]
    if not key:
        return True, "keyword check only (no Claude key)"
    news = c.get("news") or []
    if not news:
        return True, "no headlines to check"
    lines = []
    for n in news[:10]:
        kind = "OFFICIAL" if n.get("official") else "ANALYST" if n.get("tone") == "analyst" else "OTHER"
        lines.append(f"- [{kind}] {n.get('date', '')} {n.get('source', '')}: {n.get('title', '')}")
    prompt = (f"Stock: {c.get('name')} ({c['ticker']}), US-listed. Today is {ny(when):%A %d %B %Y}, "
              f"{ny(when):%H:%M} New York. It is up on the day and breaking out on heavy volume.\n"
              f"Headlines:\n" + "\n".join(lines))
    import requests

    v, raw = None, ""
    for attempt, max_tokens in enumerate((600, 1200)):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"},
                              json={"model": CLAUDE_MODEL, "max_tokens": max_tokens,
                                    "system": CLAUDE_SYSTEM,
                                    "messages": [{"role": "user", "content": prompt + "\n\nAnswer with the JSON object only, "
                                                  "no other text."}]})
        except Exception as e:
            print(f"  Claude check error: {e}")
            time.sleep(3)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            print(f"  Claude busy ({r.status_code}), retrying")
            time.sleep(5)
            continue
        if r.status_code != 200:
            why = f"Claude unavailable ({r.status_code}: {r.text[:120]}), keyword check used"
            log(st, f"{c['ticker']}: {why}")
            return True, why
        raw = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        v = parse_verdict(raw)
        if v is not None:
            break
        print(f"  Claude reply unreadable (attempt {attempt + 1}): {raw[:200]!r}")
    if v is None:
        # couldn't get a readable answer: don't throw the stock out, fall back to the keyword check
        why = f"Claude reply unreadable or busy ({raw[:80]!r}), keyword check used"
        log(st, f"{c['ticker']}: {why}")
        return True, why
    ok = bool(v.get("catalyst")) and int(v.get("strength") or 0) >= CLAUDE_MIN_STRENGTH
    reason = f"Claude: {'catalyst' if v.get('catalyst') else 'no catalyst'} (strength {v.get('strength')}/5): " \
             f"{str(v.get('reason') or '')[:160]}"
    cache[c["ticker"]] = {"ok": ok, "reason": reason}
    log(st, f"{c['ticker']}: {reason}")
    return ok, reason


def parse_verdict(text):
    """Pull {"catalyst":..., "strength":..., "reason":...} out of Claude's reply (allowing code fences or extra text)."""
    text = (text or "").replace("```json", "").replace("```", "")
    for m in re.finditer(r"\{[^{}]*\}", text, re.S):
        try:
            v = json.loads(m.group(0))
        except ValueError:
            continue
        if "catalyst" in v:
            try:
                v["strength"] = int(v.get("strength") or 0)
            except (TypeError, ValueError):
                v["strength"] = 0
            return v
    m = re.search(r'"catalyst"\s*:\s*(true|false)', text, re.I)
    if m:                                   # reply was cut off after the verdict: use what's there
        st = re.search(r'"strength"\s*:\s*(\d)', text)
        return {"catalyst": m.group(1).lower() == "true", "strength": int(st.group(1)) if st else 0,
                "reason": "(reply cut short)"}
    return None


def has_analyst(c):
    return any(n.get("tone") == "analyst" for n in c.get("news") or [])


def dt_holding(st):
    return next((p for p in st["open"] if p.get("source") == "daytrade"), None)


def dt_buy(api, st, day, c, price, why, fx, when, positions, stop=None):
    pg = to_gbp(price, "USD", "america", fx)
    qty = math.floor(TRADE_GBP / pg) if pg else 0
    if qty < 1:
        log(st, f"{c['ticker']}: one share costs more than £{TRADE_GBP:,.0f}, skipped.")
        day["skip"][c["ticker"]] = when.isoformat()
        return None
    if c["t212"] in positions:
        log(st, f"{c['ticker']}: already held in the practice account (not by the bot), skipped.")
        day["skip"][c["ticker"]] = when.isoformat()
        return None
    try:
        o = api.market(c["t212"], qty)
    except T212Error as e:
        log(st, f"{c['ticker']}: buy order refused ({e.text[:120]}).")
        day["skip"][c["ticker"]] = when.isoformat()
        return None
    day["buys"] += 1
    day.setdefault("bought_list", []).append(c["ticker"])
    pos = {"id": f"america-{c['ticker']}-{when.date().isoformat()}-{day['buys']}", "status": "pending",
           "ticker": c["ticker"], "t212": c["t212"], "name": c["name"], "market": "america",
           "exchange": c["exchange"], "ysym": c["ysym"], "source": "daytrade", "methods": [{"method": "daytrade"}],
           "bought": when.date().isoformat(), "bought_at": when.isoformat(timespec="minutes"), "ref_price": price,
           "qty": qty, "why": why, "initial_stop": stop or price * (1 - DT_STOP), "risk": price - (stop or price * (1 - DT_STOP)),
           "high": price, "high_time": when.isoformat()}
    pos["order_id"] = (o or {}).get("id")
    st["open"].append(pos)
    log(st, f"Day trade buy order: {qty} {c['ticker']} at about ${price:,.2f}. {why}")
    for _ in range(4):                     # wait for the fill, then set the 5% stop from the actual price
        time.sleep(3)
        reconcile(api, st, when)
        if pos["status"] == "open":
            break
    if pos["status"] == "open":
        pos["high"] = pos["entry_local"]
        S.push(f"Day trade: bought {c['ticker']}",
               f"{qty_txt(pos['qty'])} {c['ticker']} at ${pos['entry_local']:,.2f} (about £{pos.get('cost_gbp') or 0:,.0f}). "
               f"Stop ${pos['entry_local'] * (1 - DT_STOP):,.2f}. {why}", 3, "money_with_wings")
    return pos


def dt_sell(api, st, day, pos, positions, active, why, when):
    p = positions.get(pos["t212"])
    if not p:
        return False
    if not sell(api, st, pos, p, active, why, when):
        return False
    c = st["closed"][-1]
    day["sold"][pos["ticker"]] = when.isoformat()
    pl = c.get("pl_gbp")
    S.push(f"Day trade: sold {pos['ticker']}" + (f" ({gbp_txt(pl)})" if pl is not None else ""),
           f"{why}" + (f" Result {gbp_txt(pl)} ({S.pct(c['pl_pct'])})." if pl is not None else ""), 3, "moneybag")
    time.sleep(3)
    return True


def surge_check(bars, avg, when, prev_close=None):
    """A fresh surge: at least a third of a normal day's volume in the last 30 minutes, price rising."""
    if bars is None or not len(bars) or not avg:
        return None
    f = S.flow_split(bars, when - dt.timedelta(minutes=DT_WINDOW))
    m = dt_metrics(bars, avg, when)
    vx = f["volume"] / avg
    last = float(bars["Close"].iloc[-1])
    if vx < S.OPEN_VOL_MULT or not m["rising"] or f["volume"] * last < S.OPEN_MIN_VALUE["america"]:
        return None
    if prev_close and last <= prev_close:
        return None
    return {"vol_x": round(vx, 2), "buy_pct": f["buy_pct"], "last": last}


def news_ok(news, require):
    tones = {n.get("tone") for n in news}
    if "neg" in tones or "deal" in tones:
        return False
    return "pos" in tones or not require


def live_discover(when, known):
    """Look for stocks surging now (live data): TradingView shortlist, then Yahoo 1-minute bars and news."""
    cfg = S.MARKETS["america"]
    df = S.live_opening_tv("america")
    df = df[df["exchange"].astype(str).str.upper().isin(cfg["exchanges"])]
    df = df[~df.apply(S.is_excluded, axis=1)]
    rows = []
    for r in df.to_dict("records"):
        tick = str(r["name"])
        close, s20, s50 = (S.num(r.get(k)) for k in ("close", "SMA20", "SMA50"))
        if tick in known or not close or close < cfg["universe_price_min"]:
            continue
        if not S.OPEN_REQUIRE_TREND or (s20 and s50 and close > s20 and close > s50 and s20 >= 0.97 * s50):
            rows.append(r)
    rows.sort(key=lambda r: (S.num(r.get("volume")) or 0) / max(S.num(r.get("average_volume_10d_calc")) or 1, 1),
              reverse=True)
    rows = rows[:60]
    for r in rows:
        r["ysym"] = S.yahoo_symbol(r["exchange"], r["name"])
    bars = S.live_intraday([r["ysym"] for r in rows]) if rows else {}
    since = when - dt.timedelta(days=S.OPEN_NEWS_DAYS + (2 if ny(when).weekday() <= 1 else 0))
    found, rejected = [], []
    for r in rows:
        tick = str(r["name"])
        sc = surge_check(bars.get(r["ysym"]), S.num(r.get("average_volume_10d_calc")), when)
        if not sc:
            continue
        news = S.live_news("america", tick, r["ysym"], since, str(r.get("description") or ""))
        if not news_ok(news, DT_REQUIRE_NEWS):
            rejected.append(tick)
            continue
        top = next((n["title"] for n in news if n.get("tone") == "pos"), "")
        chg = S.num(r.get("change"))
        found.append({"ticker": tick, "name": str(r.get("description") or tick), "exchange": str(r["exchange"]).upper(),
                      "ysym": r["ysym"], "vol_x": sc["vol_x"], "avg": S.num(r.get("average_volume_10d_calc")), "news": news,
                      "prev": (S.num(r.get("close")) / (1 + chg / 100)) if chg is not None and S.num(r.get("close")) else None,
                      "why": f"{sc['vol_x']:.1f}x normal volume in the last 30 min" + (f"; news: {top[:80]}" if top else
                                                                                         "; no news found")})
    return found, rejected


def live_daily_stats(ysyms):
    """Average daily volume (10 days), previous close and normal daily range (14 days) for each symbol."""
    import pandas as pd
    import yfinance as yf

    out = {}
    try:
        data = yf.download(list(ysyms), period="2mo", interval="1d", group_by="ticker", auto_adjust=False,
                           progress=False, threads=True)
    except Exception as e:
        print(f"  daily stats download failed: {e}")
        return out
    today = dt.date.today()
    for s in ysyms:
        try:
            d = data[s] if isinstance(data.columns, pd.MultiIndex) else data
            d = d.dropna(subset=["Close"])
            d = d[[ix.date() < today for ix in d.index]]
            if len(d) >= 10:
                rng = ((d["High"] - d["Low"]) / d["Close"]).iloc[-14:].mean()
                out[s] = {"avg": float(d["Volume"].iloc[-10:].mean()), "prev": float(d["Close"].iloc[-1]),
                          "prev2": float(d["Close"].iloc[-2]), "yvol": float(d["Volume"].iloc[-1]),
                          "range": float(rng)}
        except Exception:
            continue
    return out


def opening_range(bars, when):
    """High and low of the first DT_OR_MIN minutes of today's session."""
    if bars is None or not len(bars):
        return None
    idx = bars.index.tz_convert(ny(when).tzinfo) if bars.index.tz is not None else bars.index.tz_localize("UTC").tz_convert(ny(when).tzinfo)
    o = ny(when).replace(hour=9, minute=30, second=0, microsecond=0)
    first = bars[(idx >= o) & (idx < o + dt.timedelta(minutes=DT_OR_MIN))]
    if first.empty:
        return None
    return float(first["High"].max()), float(first["Low"].min())


def market_ok(fetch_bars, when):
    """True when SPY is trading above today's opening price (used only if DT_MARKET_FILTER is on)."""
    if not DT_MARKET_FILTER:
        return True
    b = (fetch_bars(["SPY"]) or {}).get("SPY")
    if b is None or not len(b):
        return True
    o = ny(when).replace(hour=9, minute=30, second=0, microsecond=0)
    idx = b.index.tz_convert(o.tzinfo) if b.index.tz is not None else b.index.tz_localize("UTC").tz_convert(o.tzinfo)
    today = b[idx >= o]
    return not len(today) or float(today["Close"].iloc[-1]) >= float(today["Open"].iloc[0])


def day_setup(st, when):
    today = ny(when).date().isoformat()
    day = st.get("day") or {}
    if day.get("date") != today:
        day = {"date": today, "buys": 0, "sold": {}, "skip": {}, "finished": False}
    day.setdefault("skip", {})
    st["day"] = day
    return day


def manage_hold(api, st, day, hold, positions, active, when):
    """Stop, break-even and trailing stop for a one-trade-a-day position. True if it was sold."""
    p = positions.get(hold["t212"]) or {}
    price = S.num(p.get("currentPrice"))
    if not price:
        return False
    if price > hold.get("high", 0):
        hold["high"], hold["high_time"] = price, when.isoformat()
    entry = hold.get("entry_local") or hold["ref_price"]
    risk = hold.get("risk") or entry * DT_STOP
    stop_now = hold.get("stop_local") or hold["initial_stop"]
    new_stop = stop_now
    if hold["high"] >= entry + DT_BE_AT_R * risk:
        new_stop = max(new_stop, entry * 1.006)             # break-even plus the round-trip fees
    if hold["high"] >= entry + DT_TRAIL_AT_R * risk:
        new_stop = max(new_stop, hold["high"] - DT_TRAIL_R * risk)
    hold["last_check"] = f"${price:,.2f} ({S.pct(price / entry - 1)}), high ${hold['high']:,.2f}, stop ${stop_now:,.2f}"
    if price <= stop_now:
        return dt_sell(api, st, day, hold, positions, active, f"Hit the stop (${stop_now:,.2f}) at ${price:,.2f}.", when)
    if new_stop > stop_now * 1.002:
        raise_stop(api, st, hold, p, active, new_stop)
    return False


def cmd_breakout(api, st, fetch_bars, fetch_stats, clock, pause, discover):
    """One trade a day: the first "in play" stock (news + the most unusual volume + a stock that normally moves
    a lot) to break above its opening-range high. Stop at the opening-range low (max 5%), moved to break-even at
    +1R and trailed 1R under the high from +2R. Sold 10 minutes before the close."""
    started = time.time()
    when = clock()
    day = day_setup(st, when)
    if day.get("finished"):
        print("Day trading already finished for today.")
        return "done"
    op = S.load_json(DOCS / "data" / "opening.json", {}) or {}
    data = (op.get("markets") or {}).get("america") or {}
    if data.get("date") != day["date"]:
        # the opening check didn't run (GitHub's timer is unreliable): carry on with the 15-minute search alone
        print("No US opening list for today; relying on the search for movers every 15 minutes.")
        data = {}
    insts = api.instruments()
    save_tickers(api, insts)
    cands = []
    for x in data.get("picks") or []:
        if not news_ok(x.get("news") or [], True):
            continue
        inst = find_instrument(insts, "america", x["ticker"], x["name"])
        if inst:
            cands.append({**{k: x.get(k) for k in ("ticker", "name", "exchange", "ysym", "vol_x", "news")},
                          "t212": inst["ticker"]})
    stats = fetch_stats([c["ysym"] for c in cands]) if cands else {}
    fx = fx_rates()
    checked, last_scan, last_save = {}, None, 0.0
    day["list"] = [c["ticker"] for c in cands]
    print(f"  breakout watch list: {', '.join(day['list']) or 'empty so far'}")
    while True:
        when = clock()
        left, _ = S.minutes_to_close("america", when)
        mins, _ = S.minutes_since_open("america", when)
        if left is None or mins is None or mins < 0:
            save_state(st)
            return "wait"
        if time.time() - started > DT_RUN_LIMIT:
            save_state(st)
            git_save("Day trade (handing over)")
            return "resume"
        positions, active, events = reconcile(api, st, when)
        for e in events:
            S.push("Day trade update", e, 3, "memo")
        hold = dt_holding(st)
        if left <= DT_EXIT:
            if hold and hold["status"] == "open":
                dt_sell(api, st, day, hold, positions, active, "End of day: sold 10 minutes before the close.", when)
            break
        can_buy = (not hold and day["buys"] < DT_BO_BUYS and left > DT_LAST_BUY
                   and (DT_BUY_UNTIL is None or mins <= DT_BUY_UNTIL))
        if can_buy and (last_scan is None or (when - last_scan).total_seconds() >= DT_RESCAN * 60):
            last_scan = when
            known = {c["ticker"] for c in cands} | {t for t, x in checked.items()
                                                     if (when - dt.datetime.fromisoformat(x)).total_seconds() < DT_RECHECK * 60}
            try:
                found, rejected = discover(when, known)
            except Exception as e:
                print(f"  search for new movers failed: {e}")
                found, rejected = [], []
            for t in rejected:
                checked[t] = when.isoformat()
            for x in found:
                checked[x["ticker"]] = when.isoformat()
                inst = find_instrument(insts, "america", x["ticker"], x["name"])
                if inst:
                    cands.append({**{k: x.get(k) for k in ("ticker", "name", "exchange", "ysym", "vol_x", "news")},
                                  "t212": inst["ticker"]})
                    stats.update(fetch_stats([x["ysym"]]))
                    day["list"].append(x["ticker"])
                    log(st, f"Added {x['ticker']} to today's list at {ny(when):%H:%M} New York: {x.get('why', '')}")
        traded = False
        if hold and hold["status"] == "open":
            if manage_hold(api, st, day, hold, positions, active, when):
                hold, traded = None, True
        elif can_buy and mins >= DT_OR_MIN and market_ok(fetch_bars, when):
            ok = [c for c in cands if (stats.get(c["ysym"]) or {}).get("range", 0) >= DT_MIN_RANGE
                  and (c.get("vol_x") or 0) >= DT_BO_MIN_VOLX]
            ok = [c for c in ok if c["ticker"] not in day.get("bought_list", [])
                  and (day.get("claude", {}).get(c["ticker"]) or {}).get("ok", True)]
            # broker upgrades on top of official news move a stock up the queue; then the most unusual volume
            ok = sorted(ok, key=lambda c: (has_analyst(c), c.get("vol_x") or 0), reverse=True)[:DT_TOP_N]
            bars = fetch_bars([c["ysym"] for c in ok]) if ok else {}
            for c in ok:
                if c["ticker"] in day.get("bought_list", []):
                    continue                           # each stock at most once a day
                b = bars.get(c["ysym"])
                rng = opening_range(b, when)
                prev = (stats.get(c["ysym"]) or {}).get("prev")
                if not rng or b is None or not len(b):
                    continue
                last = float(b["Close"].iloc[-1])
                if last <= rng[0] or last < DT_MIN_PRICE or (prev and last / prev - 1 > DT_MAX_UP):
                    continue
                stop = max(rng[1], last * (1 - DT_BO_MAX_RISK))
                if stop >= last * 0.995:
                    stop = last * 0.99
                verdict, vwhy = claude_check(st, day, c, when)
                if not verdict:
                    continue
                top = next((n for n in c.get("news") or [] if n.get("tone") == "pos"), None)
                why = (f"Broke above its opening-range high (${rng[0]:,.2f}) at ${last:,.2f}. In play: "
                       f"{c.get('vol_x') or 0:.1f}x normal volume, normally moves "
                       f"{(stats.get(c['ysym']) or {}).get('range', 0) * 100:.0f}% a day. Stop ${stop:,.2f}."
                       + (f" News ({top.get('source')}): {top['title'][:90]}." if top else "")
                       + (" Analyst upgrade too." if has_analyst(c) else "")
                       + (f" {vwhy}" if vwhy.startswith("Claude") else ""))
                if dt_buy(api, st, day, c, last, why, fx, when, positions, stop=stop):
                    traded = True
                    break
        if traded or time.time() - last_save > 600:
            save_state(st)
            git_save(f"Day trade {when.strftime('%H:%M')}")
            last_save = time.time()
        pause(DT_POLL)
    day["finished"] = True
    done = [c for c in st["closed"] if c.get("source") == "daytrade" and c.get("sold") == when.date().isoformat()]
    tot = sum(c["pl_gbp"] for c in done if c.get("pl_gbp") is not None)
    S.push("Day trade summary", (f"{len(done)} trade(s) today: " + ", ".join(
        f"{c['ticker']} {gbp_txt(c['pl_gbp'])}" for c in done if c.get("pl_gbp") is not None) + f". Total {gbp_txt(tot)}.")
           if done else "No trade today: nothing in play broke out.", 3, "bar_chart")
    log(st, f"Day trading finished: {len(done)} trade(s), total {gbp_txt(tot)}.")
    return "done"


def live_find_drift(when):
    """Live: US stocks above today's open that closed up yesterday, normally move 4%+ a day and had positive news
    in the last 5 days, strongest interest (yesterday's volume vs normal) first."""
    from tradingview_screener import Query, col

    cfg = S.MARKETS["america"]
    q = (Query().set_markets("america")
         .select("name", "description", "exchange", "close", "open", "change", "volume", "average_volume_10d_calc",
                 "type", "typespecs", "sector", "industry", "currency")
         .where(col("type") == "stock", col("close") > col("open"), col("close") >= 1,
                col("average_volume_10d_calc") > 50000)
         .order_by("change", ascending=False).limit(600))
    _, df = q.get_scanner_data()
    for c in ("typespecs", "sector", "industry", "currency"):
        if c not in df.columns:
            df[c] = None
    df = df[df["exchange"].astype(str).str.upper().isin(cfg["exchanges"])]
    df = df[~df.apply(S.is_excluded, axis=1)]
    df = df[df["average_volume_10d_calc"] * df["close"] >= cfg["min_avg_value"]]
    rows = df.to_dict("records")
    for r in rows:
        r["ysym"] = S.yahoo_symbol(r["exchange"], r["name"])
    stats = live_daily_stats([r["ysym"] for r in rows])
    return drift_rank(rows, stats, when, lambda r: float(r["close"]), lambda r: float(r["open"]))


def drift_rank(rows, stats, when, price_of, open_of, news_fn=None, news_day=None):
    """Shared by live and replay: apply the drift rules and return up to 5 candidates, best first."""
    keep = []
    for r in rows:
        stt = stats.get(r["ysym"]) or {}
        if not stt or stt.get("range", 0) < DT_MIN_RANGE or not stt.get("prev2"):
            continue
        if stt["prev"] <= stt["prev2"]:                         # must have closed up yesterday
            continue
        price, opn = price_of(r), open_of(r)
        if not price or not opn or price <= opn or price / stt["prev"] - 1 > DT_MAX_UP:
            continue
        r["_score"] = stt.get("yvol", 0) / max(stt.get("avg") or 1, 1)
        r["_stats"] = stt
        keep.append(r)
    keep.sort(key=lambda r: r["_score"], reverse=True)
    since = when - dt.timedelta(days=DT_DRIFT_NEWS_DAYS + 2)
    news_fn = news_fn or (lambda r: S.live_news("america", str(r["name"]), r["ysym"], since, str(r.get("description") or "")))
    out = []
    for r in keep[:DT_DRIFT_CHECKS]:
        news = news_fn(r)
        if news_day:
            news = [n for n in news if n["date"] <= news_day]
        if not news_ok(news, True):
            continue
        top = next((n["title"] for n in news if n.get("tone") == "pos"), "")
        out.append({"ticker": str(r["name"]), "name": str(r.get("description") or r["name"]),
                    "exchange": str(r["exchange"]).upper(), "ysym": r["ysym"], "price": price_of(r), "news": news,
                    "why": f"News in the last {DT_DRIFT_NEWS_DAYS} days ({top[:80]}); closed up yesterday "
                           f"({S.pct(r['_stats']['prev'] / r['_stats']['prev2'] - 1)}) on "
                           f"{r['_score']:.1f}x normal volume; above today's open at 10:00 New York."})
        if len(out) >= 5:
            break
    return out


def cmd_drift(api, st, fetch_bars, clock, pause, find_drift):
    """One trade a day: at 10:00 New York, buy the best stock with positive news in the last 5 days that closed up
    yesterday and is above today's opening price. 5% stop, break-even/trailing stop, sold before the close."""
    started = time.time()
    when = clock()
    day = day_setup(st, when)
    if day.get("finished"):
        print("Day trading already finished for today.")
        return "done"
    insts = api.instruments()
    save_tickers(api, insts)
    fx = fx_rates()
    last_save = 0.0
    while True:
        when = clock()
        left, _ = S.minutes_to_close("america", when)
        mins, _ = S.minutes_since_open("america", when)
        if left is None or mins is None or mins < 0:
            save_state(st)
            return "wait"
        if time.time() - started > DT_RUN_LIMIT:
            save_state(st)
            git_save("Day trade (handing over)")
            return "resume"
        positions, active, events = reconcile(api, st, when)
        for e in events:
            S.push("Day trade update", e, 3, "memo")
        hold = dt_holding(st)
        if left <= DT_EXIT:
            if hold and hold["status"] == "open":
                dt_sell(api, st, day, hold, positions, active, "End of day: sold 10 minutes before the close.", when)
            break
        traded = False
        if hold and hold["status"] == "open":
            if manage_hold(api, st, day, hold, positions, active, when):
                hold, traded = None, True
        elif not hold and not day.get("drift_checked") and mins >= DT_DRIFT_AT and left > DT_LAST_BUY:
            day["drift_checked"] = True
            try:
                cands = find_drift(when)
            except Exception as e:
                print(f"  drift search failed: {e}")
                cands = []
            day["list"] = [c["ticker"] for c in cands]
            if not cands:
                log(st, "Drift: nothing had news in the last 5 days, closed up yesterday and is above today's open.")
            for c in cands:
                inst = find_instrument(insts, "america", c["ticker"], c["name"])
                if not inst:
                    continue
                c["t212"] = inst["ticker"]
                verdict, vwhy = claude_check(st, day, c, when)
                if not verdict:
                    continue
                stop = c["price"] * (1 - DT_STOP)
                if dt_buy(api, st, day, c, c["price"], c["why"], fx, when, positions, stop=stop):
                    traded = True
                    break
        if traded or time.time() - last_save > 600:
            save_state(st)
            git_save(f"Day trade {when.strftime('%H:%M')}")
            last_save = time.time()
        pause(DT_POLL)
    day["finished"] = True
    done = [c for c in st["closed"] if c.get("source") == "daytrade" and c.get("sold") == when.date().isoformat()]
    tot = sum(c["pl_gbp"] for c in done if c.get("pl_gbp") is not None)
    S.push("Day trade summary", (f"{done[0]['ticker']}: {gbp_txt(tot)} today." if done else
                                 "No trade today: nothing met the drift rules."), 3, "bar_chart")
    log(st, f"Day trading finished: {len(done)} trade(s), total {gbp_txt(tot)}.")
    return "done"


def apply_catalyst():
    """Fraz's catalyst rules (1 Nov 2026). Up to 5 trades a day, one at a time: each is held until its sell rule
    fires, then it looks for the next stock that still matches (never the same stock twice in a day).
    Rules: positive news in the last 24-48 hours, relative volume above 1.5x
    (about 0.22x a normal day's volume in the first 30 minutes, given the morning is the busiest part of the day),
    green on the day and breaking above short-term resistance (the opening-range high). No uptrend or
    daily-range requirement. Trades exactly like breakout: one trade a day, stop at the opening-range low
    (max 5%), break-even then trailing stop, sold before the close."""
    global DT_STRATEGY, DT_MIN_RANGE, DT_BO_BUYS, DT_BUY_UNTIL, DT_MIN_PRICE
    S.OPEN_VOL_MULT = 0.22
    S.OPEN_NEWS_DAYS = 2
    S.OPEN_REQUIRE_TREND = False
    DT_MIN_RANGE = 0.0
    DT_BO_BUYS = 5             # one at a time: hold until the sell rule fires, then look for the next that matches
    DT_BUY_UNTIL = 90          # no new buys after 11:00 New York (later buys lost in both months tested)
    DT_MIN_PRICE = 1.0         # skip sub-$1 stocks
    DT_STRATEGY = "breakout"


def cmd_daytrade(api, st, fetch_bars=None, fetch_avg=None, clock=None, pause=None, discover=None, fetch_stats=None,
                 find_drift=None):
    if DT_STRATEGY == "catalyst":
        apply_catalyst()                        # sets its rules, then runs the breakout engine
    if DT_STRATEGY == "drift":
        return cmd_drift(api, st, fetch_bars or S.live_intraday, clock or now_utc, pause or time.sleep,
                         find_drift or live_find_drift)
    if DT_STRATEGY == "breakout":
        return cmd_breakout(api, st, fetch_bars or S.live_intraday, fetch_stats or live_daily_stats,
                            clock or now_utc, pause or time.sleep, discover or live_discover)
    fetch_bars = fetch_bars or S.live_intraday
    fetch_avg = fetch_avg or live_avg_volume
    clock = clock or now_utc
    pause = pause or time.sleep
    started = time.time()
    when = clock()
    today = ny(when).date().isoformat()
    day = st.get("day") or {}
    if day.get("date") != today:
        day = {"date": today, "buys": 0, "sold": {}, "skip": {}, "finished": False}
    day.setdefault("skip", {})
    st["day"] = day
    if day.get("finished"):
        print("Day trading already finished for today.")
        return "done"
    op = S.load_json(DOCS / "data" / "opening.json", {}) or {}
    data = (op.get("markets") or {}).get("america") or {}
    if data.get("date") != today:
        print("No US opening list for today yet.")
        return "wait"
    discover = discover or live_discover
    picks = [x for x in data.get("picks") or [] if news_ok(x.get("news") or [], True)]
    if not DT_REQUIRE_NEWS:
        picks += [x for x in data.get("quiet") or [] if news_ok(x.get("news") or [], False)]
    insts = api.instruments()
    save_tickers(api, insts)
    cands = []
    for x in picks:
        inst = find_instrument(insts, "america", x["ticker"], x["name"])
        if inst:
            prev = (x["price"] / (1 + x["change"])) if x.get("price") and x.get("change") is not None else None
            cands.append({**{k: x.get(k) for k in ("ticker", "name", "exchange", "ysym", "vol_x")}, "t212": inst["ticker"],
                          "prev": prev})
    missing = [x["ticker"] for x in picks if x["ticker"] not in {c["ticker"] for c in cands}]
    if missing:
        print(f"  not on Trading 212: {', '.join(missing)}")
    if not cands:
        print("  nothing on the opening list yet; will keep looking for movers during the day")
    avg = fetch_avg([c["ysym"] for c in cands]) if cands else {}
    checked = {}
    last_scan = None
    fx = fx_rates()
    day["list"] = [c["ticker"] for c in cands]
    print(f"  day trading {len(cands)} stocks: {', '.join(day['list'])}")
    last_save = 0.0
    while True:
        when = clock()
        left, _ = S.minutes_to_close("america", when)
        mins, _ = S.minutes_since_open("america", when)
        if left is None or mins is None or mins < 0:
            save_state(st)
            return "wait"                           # weekend or before the open
        if time.time() - started > DT_RUN_LIMIT:
            save_state(st)
            git_save("Day trade (handing over)")
            return "resume"
        positions, active, events = reconcile(api, st, when)
        for e in events:
            S.push("Day trade update", e, 3, "memo")
            tick = e.split(":")[0]
            if "stop-loss" in e:
                day["sold"][tick] = when.isoformat()
                day.setdefault("stopped", []).append(tick)
        hold = dt_holding(st)
        if left <= DT_EXIT:
            if hold and hold["status"] == "open":
                dt_sell(api, st, day, hold, positions, active, "End of day: sold 10 minutes before the close.", when)
            break
        if left > DT_LAST_BUY and (last_scan is None or (when - last_scan).total_seconds() >= DT_RESCAN * 60):
            last_scan = when
            known = {c["ticker"] for c in cands} | {t for t, x in checked.items()
                                                     if (when - dt.datetime.fromisoformat(x)).total_seconds() < DT_RECHECK * 60}
            try:
                found, rejected = discover(when, known)
            except Exception as e:
                print(f"  search for new movers failed: {e}")
                found, rejected = [], []
            for t in rejected:
                checked[t] = when.isoformat()
            for x in found:
                inst = find_instrument(insts, "america", x["ticker"], x["name"])
                checked[x["ticker"]] = when.isoformat()
                if not inst:
                    continue
                cands.append({**{k: x.get(k) for k in ("ticker", "name", "exchange", "ysym", "vol_x", "prev")},
                              "t212": inst["ticker"]})
                if x.get("avg"):
                    avg[x["ysym"]] = x["avg"]
                else:
                    avg.update(fetch_avg([x["ysym"]]))
                day["list"].append(x["ticker"])
                log(st, f"Added {x['ticker']} to today's list at {ny(when):%H:%M} New York: {x.get('why', '')}")
        bars = fetch_bars(list({c["ysym"] for c in cands} | ({hold["ysym"]} if hold else set())))
        met = {c["ticker"]: dt_metrics(bars.get(c["ysym"]), avg.get(c["ysym"]), when) for c in cands}

        def too_far_up(c):
            m = met.get(c["ticker"])
            return bool(m and c.get("prev") and m["last"] / c["prev"] - 1 > DT_MAX_UP)

        def eligible(c):
            m = met.get(c["ticker"])
            if not m or not m["rising"] or too_far_up(c) or c["ticker"] in day.get("stopped", []):
                return False
            for book in (day["sold"], day["skip"]):
                t = book.get(c["ticker"])
                if t and (when - dt.datetime.fromisoformat(t)).total_seconds() < DT_COOLDOWN * 60:
                    return False
            return not hold or c["ticker"] != hold["ticker"]

        ranked = sorted((c for c in cands if eligible(c)), key=lambda c: met[c["ticker"]]["buy_x"], reverse=True)
        traded = False
        if hold and hold["status"] == "open":
            p = positions.get(hold["t212"]) or {}
            price = S.num(p.get("currentPrice")) or (met.get(hold["ticker"]) or {}).get("last")
            if price and price > hold.get("high", 0):
                hold["high"], hold["high_time"] = price, when.isoformat()
            entry = hold.get("entry_local") or hold["ref_price"]
            flat = (when - dt.datetime.fromisoformat(hold["high_time"])).total_seconds() / 60
            mine = (met.get(hold["ticker"]) or {}).get("buy_x", 0.0)
            hold["last_check"] = (f"${price:,.2f} ({S.pct(price / entry - 1)}), high ${hold['high']:,.2f}, "
                                  f"no new high for {flat:.0f} min, buying {mine:.2f}x normal in 30 min")
            held = (when - dt.datetime.fromisoformat(hold.get("bought_at") or hold["high_time"])).total_seconds() / 60
            best_x = met[ranked[0]["ticker"]]["buy_x"] if ranked else 0.0
            if price and price <= entry * (1 - DT_STOP):
                if dt_sell(api, st, day, hold, positions, active,
                           f"Fell 5% below the buy price (${price:,.2f} vs ${entry:,.2f}).", when):
                    day.setdefault("stopped", []).append(hold["ticker"])
                    hold, traded = None, True
            elif (held >= DT_MIN_HOLD and flat >= DT_FLAT_MIN and ranked and day["buys"] < DT_MAX_BUYS
                  and best_x >= DT_OVERTAKE_MIN and best_x >= DT_OVERTAKE * mine):
                best = ranked[0]
                why = (f"No new high for {flat:.0f} min, and {best['ticker']} overtook it on buying volume "
                       f"({met[best['ticker']]['buy_x']:.2f}x vs {mine:.2f}x normal in 30 min).")
                if dt_sell(api, st, day, hold, positions, active, why, when):
                    hold, traded = None, True
                    positions, active, _ = reconcile(api, st, when)
        if not hold and left > DT_LAST_BUY and day["buys"] < DT_MAX_BUYS:
            if day["buys"] == 0:
                pool = [c for c in cands if eligible(c) or (met.get(c["ticker"]) and c["ticker"] not in day["skip"]
                                                            and not too_far_up(c))]
                pick = max(pool, key=lambda c: c.get("vol_x") or 0, default=None)
                why = (f"Highest first-30-minute volume on today's list ({pick['vol_x']:.1f}x a normal day)."
                       if pick else "")
            else:
                pick = ranked[0] if ranked else None
                why = (f"Strongest buying on the list: {met[pick['ticker']]['buy_x']:.2f}x normal volume bought "
                       f"in the last 30 min, and rising." if pick else "")
            if pick and met.get(pick["ticker"]):
                if dt_buy(api, st, day, pick, met[pick["ticker"]]["last"], why, fx, when, positions):
                    traded = True
        if traded or time.time() - last_save > 600:
            save_state(st)
            git_save(f"Day trade {when.strftime('%H:%M')}")
            last_save = time.time()
        pause(DT_POLL)
    day["finished"] = True
    done = [c for c in st["closed"] if c.get("source") == "daytrade" and c.get("sold") == when.date().isoformat()]
    tot = sum(c["pl_gbp"] for c in done if c.get("pl_gbp") is not None)
    if done:
        S.push("Day trade summary", f"{len(done)} trade(s) today: " + ", ".join(
            f"{c['ticker']} {gbp_txt(c['pl_gbp'])}" for c in done if c.get("pl_gbp") is not None)
               + f". Total {gbp_txt(tot)}.", 3, "bar_chart")
    log(st, f"Day trading finished: {len(done)} trade(s), total {gbp_txt(tot)}.")
    return "done"


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--opening", action="store_true")
    g.add_argument("--preclose", action="store_true")
    g.add_argument("--sync", action="store_true")
    g.add_argument("--daytrade", action="store_true")
    ap.add_argument("--markets", default="")
    a = ap.parse_args()
    key, secret = os.getenv("T212_KEY", ""), os.getenv("T212_SECRET", "")
    if not key or not secret:
        print("Practice trading isn't set up (no T212_KEY / T212_SECRET secrets), so nothing to do.")
        return
    api = T212(key, secret)
    st = load_state()
    when = now_utc()
    try:
        if a.check:
            update_account(api, st)
            acct = st["account"]
            n = len(api.positions())
            S.push("Practice account connected",
                   f"Trading 212 practice account: value {acct.get('currency') or ''} {acct.get('value') or 0:,.2f}, "
                   f"cash {acct.get('cash') or 0:,.2f}, {n} position(s). The bot will trade it by the agreed rules.",
                   3, "white_check_mark")
            log(st, "Connection check OK.")
            save_tickers(api)
            if os.getenv("ANTHROPIC_API_KEY", "").strip():
                ok, why = claude_check(st, {}, {"ticker": "TEST", "name": "Example Therapeutics", "news": [
                    {"title": "Example Therapeutics Announces FDA Approval of Its Lead Drug", "official": True,
                     "tone": "pos", "date": when.date().isoformat(), "source": "Business Wire"}]}, when)
                works = why.startswith("Claude:")
                S.push("Claude news check " + ("working" if works else "NOT working"),
                       why if works else f"{why}. Check the ANTHROPIC_API_KEY secret and that the account has credit.",
                       3, "white_check_mark" if works else "warning")
            else:
                print("No ANTHROPIC_API_KEY secret: Claude news check off (keyword check used).")
        elif a.opening:
            bought, events = cmd_opening(api, st, when)
            update_account(api, st)
            if bought:
                lines = [fmt_buy(p) for p in bought]
                S.push(f"Practice: bought {', '.join(p['ticker'] for p in bought)}", "\n\n".join(lines),
                       3, "money_with_wings")
            if events:
                S.push("Practice account update", "\n".join(events), 3, "memo")
        elif a.preclose:
            forced = [m for m in re.split(r"[,\s]+", a.markets.lower()) if m in S.MARKETS] if a.markets.strip() else []
            sold, events = cmd_preclose(api, st, when, forced)
            update_account(api, st)
            if sold:
                lines = [f"{c['ticker']}: " + (f"{gbp_txt(c['pl_gbp'])} ({S.pct(c['pl_pct'])}). " if c["pl_gbp"] is not None
                                                else "") + c["reason"] for c in sold]
                S.push(f"Practice: sold {', '.join(c['ticker'] for c in sold)}", "\n\n".join(lines), 3, "moneybag")
            if events:
                S.push("Practice account update", "\n".join(events), 3, "memo")
        elif a.daytrade:
            res = cmd_daytrade(api, st)
            update_account(api, st)
            if res == "resume":
                Path(ROOT / ".daytrade_resume").write_text("1")
        elif a.sync:
            _, _, events = reconcile(api, st, when)
            update_account(api, st)
            save_tickers(api)
            if events:
                S.push("Practice account update", "\n".join(events), 3, "memo")
    except T212Error as e:
        hint = (" Check the T212_KEY / T212_SECRET secrets were made in the PRACTICE account."
                if e.code in (401, 403) else "")
        log(st, f"Trading 212 error {e.code}: {e.text[:150]}.{hint}")
        S.push("Practice bot problem", f"Trading 212 said: {e.code} {e.text[:150]}.{hint}", 3, "warning")
    save_state(st)
    try:
        S.rerender_page(DOCS)
    except Exception as e:
        print(f"  page refresh failed: {e}")


if __name__ == "__main__":
    main()
