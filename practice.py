"""
Practice-account trading on Trading 212. It only ever uses the PRACTICE (demo) account.

Rules (agreed 31 Oct 2026):
  Buy   - Opening-surge picks with good news and "buyers in control" (60%+ estimated buying),
          skipping any already up 20%+ on the day (chasing risk).
        - "2+ methods agree" picks from the weekly scan, bought at the first opening check after the scan.
  Size  - A fixed GBP 1,000 per trade in whole shares, with at most 10 of the bot's positions open.
  Sell  - Each method's own sell rule, checked about an hour before the close. For a stock several methods
          agreed on, the first method to say sell wins. Opening-surge trades use an episodic-pivot style
          rule: stop at the day's low (never more than 8% away), then sell on a close below the 10-day EMA.
        - A stop-loss order is placed at Trading 212 straight after buying. It sits at the method's stop
          (or just under the weekly moving average for weekly methods) and is only ever raised, never lowered.

Commands:
  python practice.py --check      test the connection and send a phone alert with the account value
  python practice.py --opening    after the opening check: buy
  python practice.py --preclose   about an hour before the close: sell or raise stops
  python practice.py --sync       evening: record stop-outs, make sure every position has a stop, refresh page

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
        "reason": reason})
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


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--opening", action="store_true")
    g.add_argument("--preclose", action="store_true")
    g.add_argument("--sync", action="store_true")
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
        elif a.sync:
            _, _, events = reconcile(api, st, when)
            update_account(api, st)
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
